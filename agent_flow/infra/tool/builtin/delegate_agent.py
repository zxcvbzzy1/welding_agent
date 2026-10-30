"""Delegate a task or load visual input through ordinary tool events."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
import uuid

from domain.agent.react import React
from domain.agent.vlm import Vlm
from domain.agent_base import AgentBase
from domain.context.providers import AvailableToolsProvider
from domain.run_context import current_agent
from domain.tool import Tool, Tool_respond
from infra.config import agent_dict, bus, factory, llm_client, vlm_client
from infra.context.runtime import bind_context_runtime
from infra.event_bind import On_bind
from infra.LLM.images import IMAGE_SCHEMA, ImageRuntime
from infra.runtime.control import ensure_execution_active


DELEGATE_AGENT = Tool(
    name="delegate_agent", field="system",
    description="委托临时智能体完成独立任务。react 执行普通任务；vlm 按 task 要求解析图片并返回文字。子智能体不继承聊天历史，请完整提供任务和图片引用。",
    input_schema={"type": "object", "properties": {
        "agent_type": {"type": "string", "enum": ["react", "vlm"]},
        "task": {"type": "string", "minLength": 1}, "images": IMAGE_SCHEMA,
    }, "required": ["agent_type", "task"]},
)
LOAD_IMAGES = Tool(
    name="load_images", field=None,
    description="VLM 按需查看历史或新生成图片。upload 的 value 是聊天文件 ID；path 是当前工作目录内路径；url 是公网图片地址。加载后原图在下一轮推理可见。",
    input_schema={"type": "object", "properties": {"images": IMAGE_SCHEMA}, "required": ["images"]},
)


LLMWrapper = Callable[[Any, AgentBase, AgentBase], Any]
ContextPublisher = Callable[[str, dict], Awaitable[None]]


def available_tool_names(agent: AgentBase) -> set[str]:
    names = set()
    for provider in agent.context_engine._providers:
        if isinstance(provider, AvailableToolsProvider) and provider.enabled:
            names.update(tool.name for tool in Tool.get_all_tools()
                         if tool.field in provider._fields or tool.name in provider._tools)
    return names


class DelegateAgentTool:
    """Run temporary agents using concrete capabilities, without an app factory."""

    def __init__(self, *, text_llm=None, vision_llm=None, image_runtime: ImageRuntime | None = None,
                 wrap_llm: LLMWrapper | None = None, publish_context: ContextPublisher | None = None):
        self.text_llm = text_llm if text_llm is not None else llm_client
        self.vision_llm = vision_llm if vision_llm is not None else vlm_client
        self.image_runtime = image_runtime
        self.wrap_llm = wrap_llm
        self.publish_context = publish_context
        self._default_images = ImageRuntime(self.vision_llm)

    def _images(self) -> ImageRuntime:
        if self.image_runtime is not None:
            return self.image_runtime
        active = ImageRuntime.current()
        # Files IDs must belong to the same client/account as visual inference.
        if active is not None and active.client is self.vision_llm:
            return active
        return self._default_images

    async def run(self, parent: AgentBase, agent_type: str, task: str, images=None) -> str:
        if getattr(parent, "is_temporary", False):
            raise ValueError("临时子智能体不能再次委托")
        if agent_type not in {"react", "vlm"} or not isinstance(task, str) or not task.strip():
            raise ValueError("必须提供 agent_type(react/vlm) 与非空 task")
        if images and agent_type != "vlm":
            raise ValueError("图片输入需要委托 vlm")
        await ensure_execution_active()
        image_runtime = self._images()
        cls = Vlm if agent_type == "vlm" else React
        base_llm = self.vision_llm if cls is Vlm else self.text_llm
        agent_id = f"delegate_{uuid.uuid4().hex}"
        kwargs = {}
        if cls is Vlm:
            kwargs.update(image_loader=image_runtime.load, image_scope=image_runtime.scope)
        child = cls(id=agent_id, name=f"{parent.name} / {agent_type}", llm=base_llm,
                    work_path=parent.work_path,
                    role_prompt="只完成本次委托任务，返回所要求的结果。" +
                    ("按任务要求解析图片并返回描述。" if cls is Vlm else ""), **kwargs)
        try:
            child.is_temporary = True
            child.parent_agent_id = parent.id
            allowed = (available_tool_names(child) & available_tool_names(parent)) - {"delegate_agent", "load_images"}
            if cls is Vlm:
                allowed.add("load_images")
            child.permitted_tool_names = frozenset(allowed)
            for provider in child.context_engine._providers:
                if isinstance(provider, AvailableToolsProvider):
                    provider._fields, provider._tools = [], sorted(allowed)
            if self.wrap_llm is not None:
                child._llm = self.wrap_llm(base_llm, child, parent)

            async def publish(name, payload):
                await self.publish_context(name,
                    {**payload, "temporary": True, "parent_agent_id": parent.id})

            bind_context_runtime(child, publish=publish if self.publish_context is not None else None)
            token = current_agent.set(child)
            try:
                # Inherit run_id, execution_id and cancellation guard; only the
                # active agent changes, so nested observations return to child.
                async with image_runtime.scope():
                    if cls is Vlm:
                        await child.start(task, images=images)
                    else:
                        await child.start(task)
                await ensure_execution_active()
                return child.states.get("final", "") or child.states.get("finish_reason", "")
            finally:
                current_agent.reset(token)
        finally:
            registry = AgentBase.get_instance_dict()
            if registry.get(agent_id) is child:
                registry.pop(agent_id, None)


_default_delegate_tool = DelegateAgentTool()
on_tool = On_bind()
factory._build_and_register_list([DELEGATE_AGENT, LOAD_IMAGES], bus)


@on_tool.on(factory.tool("delegate_agent").called())
async def delegate_agent(**kwargs):
    agent = current_agent.get() or agent_dict.get(kwargs.get("agent_id"))
    if agent is None:
        raise RuntimeError("无法定位调用委托工具的智能体")
    tool = getattr(agent, "delegate_tool", _default_delegate_tool)
    result = await tool.run(agent, kwargs.get("agent_type"), kwargs.get("task"), kwargs.get("images"))
    return factory.tool("delegate_agent").succeeded(Tool_respond(
        agent_id=agent.id, name="delegate_agent", success=True, respond=result))


@on_tool.on(factory.tool("load_images").called())
async def load_images(**kwargs):
    agent = current_agent.get() or agent_dict.get(kwargs.get("agent_id"))
    if not isinstance(agent, Vlm):
        raise ValueError("load_images 仅供 VLM 使用；普通 LLM 请使用 delegate_agent 的 vlm 类型")
    images = kwargs.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("images 必须是非空图片引用列表")
    refs = await agent.load_images(images)
    return factory.tool("load_images").succeeded(Tool_respond(
        agent_id=agent.id, name="load_images", success=True,
        respond={"status": "loaded", "images": [ref.to_dict() for ref in refs], "message": "图片已加入下一轮推理"}))
