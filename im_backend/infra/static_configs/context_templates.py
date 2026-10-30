"""新建 Agent 时使用的上下文模版。

每个新建的 native Agent 都会按这里的模版克隆出一份**独立**的上下文管理 / 记忆，
而不是复用别人选定的 context。模版内容对齐 ``static_agents.py`` 里 operator / planner
的 ContextEngine 配置，但这里只用纯 dict 描述 provider_config（与
``agent_flow`` 的 ``ContextService.create_context`` 入参一致），因此**不依赖也不引用**
``static_agents.py``，避免在创建流程里实例化种子 Agent。

provider_config 结构：``[{"provider_id": str, "enabled": bool, "params": dict}, ...]``
``ContextService._build_provider`` 会据此构造对应 provider，并配套一份
``DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])`` 记忆。
"""

from __future__ import annotations

import copy
from typing import Any

# 工具反馈：达到 100 条时归纳历史，并完整保留最新 10 条原文。
_TOOL_OUTPUT_STRATEGY = {"pipeline": [{"type": "compact_history", "threshold": 100, "keep_recent": 10}]}
# 对话历史沿用原有最近 35 条规则。
_HISTORY_STRATEGY = {"pipeline": [{"type": "full_history"}, {"type": "recency", "keep_last": 35}]}
# 新建 Agent 放开系统工具与人机问答。
DEFAULT_TOOL_FIELDS = ["system", "human"]


def _executor_template() -> list[dict[str, Any]]:
    return [
        {"provider_id": "state", "enabled": True, "params": {}},
        {
            "provider_id": "available_tools",
            "enabled": True,
            "params": {"available_fields": list(DEFAULT_TOOL_FIELDS), "available_tools": []},
        },
        {"provider_id": "user_prompt", "enabled": True, "params": {}},
        {"provider_id": "pinned_context", "enabled": True, "params": {}},
        # 技能召回：从 memory 的 "skill" 字段注入召回到的技能（与 static 执行者配置一致）。
        {
            "provider_id": "skill",
            "enabled": True,
            "params": {"memory_field": "skill", "strategy_config": {"pipeline": [{"type": "full_history"}]}},
        },
        {
            "provider_id": "history",
            "enabled": True,
            "params": {"memory_field": "agent_history", "strategy_config": _HISTORY_STRATEGY},
        },
        {"provider_id": "error", "enabled": True, "params": {}},
        {
            "provider_id": "tool_output",
            "enabled": True,
            "params": {"memory_field": "tool_respond", "strategy_config": _TOOL_OUTPUT_STRATEGY},
        },
    ]


def _planner_template() -> list[dict[str, Any]]:
    return [
        {"provider_id": "state", "enabled": True, "params": {}},
        {"provider_id": "available_executors", "enabled": True, "params": {}},
        {
            "provider_id": "available_tools",
            "enabled": True,
            "params": {"available_fields": [], "available_tools": ["ask_user"]},
        },
        {"provider_id": "executor_status", "enabled": True, "params": {}},
        {"provider_id": "pinned_context", "enabled": True, "params": {}},
        {"provider_id": "plan_observations", "enabled": True, "params": {}},
        {
            "provider_id": "history",
            "enabled": True,
            "params": {"memory_field": "agent_history", "strategy_config": _HISTORY_STRATEGY},
        },
        {"provider_id": "user_prompt", "enabled": True, "params": {}},
        {"provider_id": "error", "enabled": True, "params": {}},
        {
            "provider_id": "tool_output",
            "enabled": True,
            "params": {"memory_field": "tool_respond", "strategy_config": _TOOL_OUTPUT_STRATEGY},
        },
    ]


def agent_context_template(kind: str) -> list[dict[str, Any]]:
    """返回某类 Agent 的 provider_config 模版（深拷贝，调用方可安全修改）。"""
    if kind == "planner":
        return copy.deepcopy(_planner_template())
    if kind == "executor":
        return copy.deepcopy(_executor_template())
    raise ValueError(f"未知上下文类型: {kind}")
