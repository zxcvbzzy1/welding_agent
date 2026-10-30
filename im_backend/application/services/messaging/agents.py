from __future__ import annotations

import copy
from typing import Any

from im_backend.application.services.platform.cleanup import IMCleanupService
from im_backend.infra.agent_flow_bridge.bridge import AgentFlowBridge
from im_backend.infra.static_configs.context_templates import agent_context_template


class IMAgentService:
    PROTECTED_AGENT_IDS = {"default_planner", "default_executor"}

    def __init__(self, *, bridge: AgentFlowBridge, cleanup: IMCleanupService) -> None:
        self._bridge = bridge
        self._cleanup = cleanup

    def list_agents(self) -> list[dict[str, Any]]:
        return [record for record in self._bridge.list_agents() if self._is_native_agent(record)]

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "field": tool.get("field"),
            }
            for tool in self._bridge.list_tools()
        ]

    def list_visible_agents(self, user_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self._bridge.list_agents()
            if self._is_native_agent(record) and self.is_visible(record, user_id)
        ]

    def list_contexts(self) -> list[dict[str, Any]]:
        return [record for record in self._bridge.list_contexts() if record.get("kind") != "coding"]

    def list_visible_contexts(self, user_id: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self._bridge.list_contexts()
            if record.get("kind") != "coding" and self.is_visible(record, user_id)
        ]

    def create_agent(
        self,
        *,
        name: str,
        agent_type: str = "executor",
        context_id: str = "default_executor",
        role_prompt: str = "",
        metadata: dict[str, Any] | None = None,
        owner_user_id: str = "",
        tool_names: list[str] | None = None,
        tool_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("Agent 名称不能为空")
        if agent_type not in {"executor", "planner"}:
            raise ValueError("agent_type 必须是 executor 或 planner")
        requested_metadata = metadata or {}
        self._bridge.agents.validate_model_kind(agent_type, requested_metadata)
        agent_kind = requested_metadata.get("agent_kind") or "native"
        if agent_kind != "native":
            raise ValueError("当前仅支持 native Agent")

        # 每个 Agent 按模版克隆一份独立的上下文管理 / 记忆，不复用传入的 context_id；
        # executor 选了具体工具/字段时，在模版基础上限定 available_tools。
        effective_context_id = self._build_agent_context(
            name=name.strip(),
            agent_type=agent_type,
            tool_names=tool_names or [],
            tool_fields=tool_fields or [],
            owner_user_id=owner_user_id,
        )

        agent_metadata = {
            key: value for key, value in requested_metadata.items() if key != "agent_kind"
        }
        if owner_user_id:
            agent_metadata = {
                **agent_metadata,
                "owner_user_id": owner_user_id,
                "visibility": "private",
            }
        return self._bridge.create_agent(
            name=name.strip(),
            agent_type=agent_type,
            context_id=effective_context_id,
            role_prompt=role_prompt,
            metadata=agent_metadata,
        )

    def _build_agent_context(
        self,
        *,
        name: str,
        agent_type: str,
        tool_names: list[str],
        tool_fields: list[str],
        owner_user_id: str = "",
    ) -> str:
        """按上下文模版为新建 native Agent 克隆一份独立的上下文管理 / 记忆。

        模版来自 ``context_templates``（对齐 static_agents.py 的 provider 配置），
        executor 选了具体工具/字段时，在模版基础上把 available_tools 限定为所选项。
        """
        kind = "planner" if agent_type == "planner" else "executor"
        provider_config = agent_context_template(kind)
        if kind == "executor" and (tool_names or tool_fields):
            tool_params = {
                "available_fields": list(tool_fields or []),
                "available_tools": list(tool_names or []),
            }
            replaced = False
            for item in provider_config:
                if item.get("provider_id") == "available_tools":
                    item["params"] = tool_params
                    item["enabled"] = True
                    replaced = True
            if not replaced:
                provider_config.append({"provider_id": "available_tools", "enabled": True, "params": tool_params})
        record = self._bridge.contexts.create_context(
            kind=kind,
            name=f"{name} 上下文",
            provider_config=provider_config,
        )
        context_id = record["context_id"]
        if owner_user_id:
            self._bridge.store.update_one(
                "contexts",
                {"context_id": context_id},
                {"metadata": {"owner_user_id": owner_user_id, "visibility": "private", "kind_label": "agent_context"}},
            )
        return context_id

    def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        role_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
        tool_names: list[str] | None = None,
        tool_fields: list[str] | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        """编辑 Agent：name / role_prompt / 描述等基础字段 + executor 的可用工具。

        agent_type 不可通过编辑变更（会牵动上下文重新置备）。工具变更落到该 Agent
        独占的上下文的 available_tools provider 上。
        """
        if agent_id in self.PROTECTED_AGENT_IDS:
            raise ValueError("默认 Agent 不允许编辑")
        record = self._bridge.ensure_agent_exists(agent_id)
        if user_id and self.owner_user_id(record) != user_id:
            raise ValueError("只能编辑当前用户拥有的 Agent")

        existing_meta = record.get("metadata") or {}
        agent_kind = existing_meta.get("agent_kind") or "native"
        if agent_kind != "native":
            raise ValueError("当前仅支持 native Agent")
        agent_type = record.get("agent_type")
        self._bridge.agents.validate_model_kind(agent_type, {**existing_meta, **(metadata or {})})

        # 工具变更仅对 executor 生效。
        if (
            (tool_names is not None or tool_fields is not None)
            and agent_type == "executor"
        ):
            self._update_agent_tools(
                record.get("context_id", ""),
                tool_names or [],
                tool_fields or [],
            )

        merged_metadata: dict[str, Any] | None = None
        if metadata is not None:
            # store 对 metadata 整体覆盖：先并好旧值，再钉死不可经编辑变更的系统字段。
            merged_metadata = {**existing_meta, **metadata}
            merged_metadata.pop("agent_kind", None)
            if existing_meta.get("owner_user_id"):
                merged_metadata["owner_user_id"] = existing_meta["owner_user_id"]
            if existing_meta.get("visibility"):
                merged_metadata["visibility"] = existing_meta["visibility"]

        return self._bridge.update_agent(
            agent_id,
            name=name,
            role_prompt=role_prompt,
            metadata=merged_metadata,
        )

    def _update_agent_tools(
        self, context_id: str, tool_names: list[str], tool_fields: list[str]
    ) -> None:
        """把所选工具/字段写回该 Agent 独占上下文的 available_tools provider。"""
        if not context_id:
            return
        record = self._bridge.contexts.get_context(context_id)
        if record is None:
            return
        provider_config = copy.deepcopy(record.get("provider_config") or [])
        tool_params = {
            "available_fields": list(tool_fields or []),
            "available_tools": list(tool_names or []),
        }
        replaced = False
        for item in provider_config:
            if item.get("provider_id") == "available_tools":
                item["params"] = tool_params
                item["enabled"] = True
                replaced = True
        if not replaced:
            provider_config.append(
                {"provider_id": "available_tools", "enabled": True, "params": tool_params}
            )
        self._bridge.contexts.update_context_providers(context_id, provider_config)

    async def delete_agent(self, agent_id: str, *, user_id: str = "") -> dict[str, Any]:
        if agent_id in self.PROTECTED_AGENT_IDS:
            raise ValueError("默认 Agent 不允许删除")
        record = self._bridge.ensure_agent_exists(agent_id)
        if user_id and self.owner_user_id(record) != user_id:
            raise ValueError("只能删除当前用户拥有的 Agent")
        agent_flow_result = await self._bridge.delete_agent(agent_id)
        im_stats = await self._cleanup.delete_agent_im_refs(agent_id)
        return {
            "deleted": True,
            "agent_id": agent_id,
            "agent_flow": agent_flow_result,
            "im_stats": im_stats,
        }

    def ensure_agent_access(self, agent_id: str, user_id: str) -> dict[str, Any]:
        record = self._bridge.ensure_agent_exists(agent_id)
        if not self._is_native_agent(record) or not self.is_visible(record, user_id):
            raise KeyError(f"Agent 不存在: {agent_id}")
        return record

    def ensure_context_access(self, context_id: str, user_id: str) -> dict[str, Any]:
        record = self._bridge.get_context_record(context_id)
        if record is None or record.get("kind") == "coding" or not self.is_visible(record, user_id):
            raise KeyError(f"Context 不存在: {context_id}")
        return record

    @staticmethod
    def _is_native_agent(record: dict[str, Any]) -> bool:
        return (record.get("metadata") or {}).get("agent_kind", "native") == "native"

    def is_visible(self, record: dict[str, Any], user_id: str) -> bool:
        metadata = record.get("metadata") or {}
        if metadata.get("visibility") == "public":
            return True
        owner = self.owner_user_id(record)
        return not owner or owner == user_id

    def owner_user_id(self, record: dict[str, Any]) -> str:
        metadata = record.get("metadata") or {}
        return metadata.get("owner_user_id") or metadata.get("created_by") or ""
