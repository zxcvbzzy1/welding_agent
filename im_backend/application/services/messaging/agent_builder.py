"""对话式（引导）创建 Agent 的应用服务。

一个轻量的“创建助手”：把创建流程写进 system prompt，逐轮引导用户给出 agent 配置，
每轮都在回复末尾输出一个 JSON 草稿块。前端拿到草稿后预填到现有创建表单，由用户复核
确认后再走原有 createAgent 流程（本服务只产出草稿，不直接创建）。

复用 agent_flow 既有的廉价模型单例（infra.config.llm_client，OpenAI 兼容的
DeepSeek/GLM/MiniMax）——这是项目里统一的“简单任务”模型；本栈没有 Anthropic/Claude 客户端。
"""

from __future__ import annotations

import json
import re
from typing import Any

from im_backend.application.services.messaging.agents import IMAgentService
from im_backend.infra.agent_flow_bridge.pathing import ensure_agent_flow_path

ensure_agent_flow_path()

from infra.config import llm_client  # type: ignore  # noqa: E402


_AGENT_KINDS = ("native", "claude_code", "codex")
_AGENT_TYPES = ("executor", "planner")
_PERMISSION_PROFILES = ("human_confirm", "plan")

_TOOL_FIELD_LABELS = {
    "system": "系统",
    "search": "搜索",
    "memory": "记忆",
    "human": "人工协作",
    "camera": "相机",
    "write_agent": "编写 Agent",
    "other": "其它",
}


class AgentBuilderService:
    """运行单轮“创建助手”对话，产出助手回复 + agent 草稿。"""

    def __init__(self, *, agents: IMAgentService, llm: Any = None) -> None:
        self._agents = agents
        self._llm = llm or llm_client

    async def chat(self, *, messages: list[dict[str, Any]], draft: dict[str, Any] | None) -> dict[str, Any]:
        prior = self._sanitize_draft(draft or {})
        llm_messages = [{"role": "system", "content": self._build_system_prompt(prior)}]
        for item in messages:
            role = "assistant" if item.get("role") == "assistant" else "user"
            llm_messages.append({"role": role, "content": str(item.get("content", ""))})

        raw = await self._llm.chat(llm_messages)
        raw = raw or ""
        parsed = self._parse_json(raw)
        reply = self._strip_json_block(raw).strip()

        if parsed is None:
            # 解析失败：保留上一轮草稿，本轮不推进 ready，绝不抛错。
            return {"reply": reply or "（请补充一下信息，我没能整理出有效草稿）", "draft": prior, "ready": False}

        ready = bool(parsed.get("ready"))
        merged = self._merge_draft(prior, parsed)
        return {"reply": reply, "draft": merged, "ready": ready}

    # ── 内部 ──────────────────────────────────────────────────────
    def _build_system_prompt(self, draft: dict[str, Any]) -> str:
        tools = self._agents.list_tools()
        tool_lines = [
            f"- {tool.get('name', '')}（field={tool.get('field') or 'other'}）：{tool.get('description', '')}"
            for tool in tools
            if tool.get("name")
        ]
        tool_catalog = "\n".join(tool_lines) if tool_lines else "（暂无可用工具）"
        field_labels = "、".join(f"{key}({label})" for key, label in _TOOL_FIELD_LABELS.items())
        return (
            "你是 AgentHub 的“创建助手”。通过多轮对话引导用户配置一个新的 Agent，并在每一轮回复的"
            "末尾输出一个 JSON 草稿块。你只负责整理草稿，不要自称已经创建；真正的创建由用户在表单里确认后完成。\n\n"
            "## 需要收集的字段\n"
            "- name：Agent 名称（必填）。\n"
            f"- agent_kind：运行类型，取值 {list(_AGENT_KINDS)}。native=平台原生 Agent；claude_code / codex=外部 coding CLI。\n"
            f"- agent_type：{list(_AGENT_TYPES)}。仅 native 可为 planner；claude_code / codex 只能是 executor。\n"
            "- description：能力描述（擅长方向 / 可承担任务）。\n"
            "- role_prompt：角色 system prompt，仅 native 有意义；非 native 留空。\n"
            "- workdir：工作目录，留空表示用后端默认。\n"
            f"- permission_profile：仅 coding（claude_code/codex）有意义，取值 {list(_PERMISSION_PROFILES)}。\n"
            "- tool_names / tool_fields：仅 native 且 agent_type=executor 时有意义。tool_fields 是工具大类，"
            f"可选：{field_labels}；tool_names 是下面目录里的具体工具名。其它情况一律留空。\n"
            "- tags：1~2 个能力标签短词(中文)，用于在侧栏展示，例如 前端、测试、数据分析。\n\n"
            "## 可用工具目录\n"
            f"{tool_catalog}\n\n"
            "## 对话要求\n"
            "1. 用简洁中文一次只问 1-2 个关键问题，按 name -> agent_kind -> agent_type -> 能力/工具 的顺序推进。\n"
            "2. 严格遵守上面的取值约束（例如用户想要 planner 但选了 codex，要纠正为 native 或改回 executor），"
            "否则最终表单提交会被后端拒绝。\n"
            "3. 每轮回复都必须在末尾输出一个 ```json 代码块，包含当前最佳猜测的完整草稿（字段可部分为空），"
            "并带一个布尔 ready：当 name 与 agent_kind 已确定（native 还需 agent_type 确定）时置为 true。\n"
            "4. JSON 之外的正文是给用户看的引导语，不要把 JSON 内容重复进正文。\n\n"
            "## 当前草稿（供你参考，可继续修改）\n"
            f"```json\n{json.dumps(draft, ensure_ascii=False)}\n```\n\n"
            "草稿 JSON 的字段：name, agent_kind, agent_type, description, role_prompt, workdir, "
            "permission_profile, tool_names(list), tool_fields(list), tags(list), ready(bool)。"
        )

    @staticmethod
    def _strip_json_block(raw: str) -> str:
        return re.sub(r"```(?:json)?\s*.*?```", "", raw, flags=re.DOTALL)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
        else:
            # 没有围栏时，退而尝试截取首个顶层 JSON 对象。
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                return None
            text = text[start : end + 1]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _merge_draft(self, prior: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
        merged = {**prior}
        for key in ("name", "description", "role_prompt", "workdir"):
            if isinstance(parsed.get(key), str):
                merged[key] = parsed[key]
        if parsed.get("agent_kind") in _AGENT_KINDS:
            merged["agent_kind"] = parsed["agent_kind"]
        if parsed.get("agent_type") in _AGENT_TYPES:
            merged["agent_type"] = parsed["agent_type"]
        if parsed.get("permission_profile") in _PERMISSION_PROFILES:
            merged["permission_profile"] = parsed["permission_profile"]
        for key in ("tool_names", "tool_fields"):
            value = parsed.get(key)
            if isinstance(value, list):
                merged[key] = [str(item) for item in value if isinstance(item, (str, int))]
        if isinstance(parsed.get("tags"), list):
            merged["tags"] = [str(x) for x in parsed["tags"] if str(x).strip()][:2]
        return self._sanitize_draft(merged)

    @staticmethod
    def _sanitize_draft(draft: dict[str, Any]) -> dict[str, Any]:
        kind = draft.get("agent_kind") if draft.get("agent_kind") in _AGENT_KINDS else "native"
        agent_type = draft.get("agent_type") if draft.get("agent_type") in _AGENT_TYPES else "executor"
        # 非 native 只能是 executor；非 native 的 role_prompt 无意义。
        if kind != "native":
            agent_type = "executor"
        profile = draft.get("permission_profile")
        if profile not in _PERMISSION_PROFILES:
            profile = "human_confirm"
        tool_names = draft.get("tool_names") if isinstance(draft.get("tool_names"), list) else []
        tool_fields = draft.get("tool_fields") if isinstance(draft.get("tool_fields"), list) else []
        # 工具选择仅 native+executor 有意义。
        if not (kind == "native" and agent_type == "executor"):
            tool_names, tool_fields = [], []
        tags = draft.get("tags") if isinstance(draft.get("tags"), list) else []
        return {
            "name": str(draft.get("name") or ""),
            "agent_kind": kind,
            "agent_type": agent_type,
            "description": str(draft.get("description") or ""),
            "role_prompt": str(draft.get("role_prompt") or "") if kind == "native" else "",
            "workdir": str(draft.get("workdir") or ""),
            "permission_profile": profile,
            "tool_names": [str(item) for item in tool_names],
            "tool_fields": [str(item) for item in tool_fields],
            "tags": [str(x).strip() for x in tags if str(x).strip()][:2],
        }
