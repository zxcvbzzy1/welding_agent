from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from im_backend.api.core import get_agent_catalog, get_current_user
from im_backend.application.services.messaging.agents import IMAgentService
from im_backend.infra.agent_flow_bridge.pathing import ensure_agent_flow_path

ensure_agent_flow_path()
from infra.tool.builtin import robot as robot_tool  # type: ignore  # noqa: E402
from infra.tool.builtin import system as bash_tool  # type: ignore  # noqa: E402


router = APIRouter()

_VALID_DANGER_POLICY = {"reject", "confirm"}
_VALID_AUTO_CONFIRM = {"ask", "approve", "reject"}


class ToolConfigRequest(BaseModel):
    danger_policy: str | None = None
    auto_confirm: str | None = None


def _find_tool(service: IMAgentService, tool_name: str) -> dict | None:
    return next(
        (t for t in service._bridge.list_tools() if t.get("name") == tool_name),
        None,
    )


@router.get("/tools/{tool_name}")
async def get_tool_detail(
    tool_name: str,
    current_user: dict = Depends(get_current_user),
    service: IMAgentService = Depends(get_agent_catalog),
):
    """工具详情（只读）：完整的 input_schema / metadata / events / 持久化 config。"""
    _ = current_user
    tool = _find_tool(service, tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail="工具不存在")
    return {"item": tool}


@router.patch("/tools/{tool_name}/config")
async def patch_tool_config(
    tool_name: str,
    request: ToolConfigRequest,
    current_user: dict = Depends(get_current_user),
    service: IMAgentService = Depends(get_agent_catalog),
):
    """更新工具的运行时参数配置。

    config 存到 tools 集合的独立 `config` 字段（不放 metadata，避免被启动时的工具登记覆盖），
    并对支持运行时配置的内置工具进程内即时生效（im_backend 与 agent_flow 同进程）。
    """
    _ = current_user
    if _find_tool(service, tool_name) is None:
        raise HTTPException(status_code=404, detail="工具不存在")

    incoming = {
        k: v
        for k, v in {
            "danger_policy": request.danger_policy,
            "auto_confirm": request.auto_confirm,
        }.items()
        if v is not None
    }
    if "danger_policy" in incoming and incoming["danger_policy"] not in _VALID_DANGER_POLICY:
        raise HTTPException(status_code=400, detail="danger_policy 仅支持 reject / confirm")
    if "auto_confirm" in incoming and incoming["auto_confirm"] not in _VALID_AUTO_CONFIRM:
        raise HTTPException(status_code=400, detail="auto_confirm 仅支持 ask / approve / reject")
    if not incoming:
        raise HTTPException(status_code=400, detail="未提供任何可更新的配置项")

    store = service._bridge.store
    record = store.find_one("tools", {"tool_id": tool_name}) or {}
    merged = {**(record.get("config") or {}), **incoming}
    store.update_one("tools", {"tool_id": tool_name}, {"config": merged}, upsert=True)

    # 支持运行时配置的内置工具进程内即时生效，无需重启。
    if tool_name == "bash":
        bash_tool.set_bash_settings(
            danger_policy=merged.get("danger_policy"),
            auto_confirm=merged.get("auto_confirm"),
        )
    if tool_name in {"robot_move_to", "robot_move_by"}:
        robot_tool.set_robot_settings(auto_confirm=merged.get("auto_confirm"))

    return {"item": {"name": tool_name, "config": merged}}
