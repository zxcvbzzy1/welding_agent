"""Visual ReAct behavior. Image storage and transport are injected capabilities."""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Literal

from domain.agent_base import AgentBase, AgentDecision
from domain.agent.react.react import REACT_OUTPUT_FORMAT
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider, PinnedContextProvider, SkillProvider, HistoryProvider,
    UserPromptProvider, ToolOutputProvider, StateProvider, ErrorProvider,
)
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy, CompactHistoryStrategy
from domain.memory.long.providers import MemoryDocumentProvider
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory


@dataclass(frozen=True)
class ImageReference:
    kind: Literal["upload", "path", "url"]
    value: str

    def __post_init__(self):
        if self.kind not in {"upload", "path", "url"} or not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("图片引用必须包含 kind(upload/path/url) 和非空 value")
        if self.value.strip().lower().startswith("data:"):
            raise ValueError("不接受内联图片数据，请使用文件引用")

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) != {"kind", "value"}:
            raise ValueError("图片引用格式为 {kind: upload/path/url, value: 文件ID/路径/公网URL}")
        return cls(kind=value["kind"], value=value["value"])

    def to_dict(self):
        return {"kind": self.kind, "value": self.value}


ImageLoader = Callable[[list[ImageReference], str], Awaitable[list[str]]]


class Vlm(AgentBase):
    def __init__(self, id: str, name: str, llm, context=None, *, role_prompt="", work_path=".",
                 image_loader: ImageLoader | None = None, image_scope=None):
        original = context if context is not None else self._build_context()
        context = ContextEngine(list(original._providers), original.get_memory(),
                                summarize=original.summarize, notify=original.notify)
        # Visual input is intrinsic to Vlm; keep caller-owned template objects intact.
        for index, provider in enumerate(context._providers):
            if isinstance(provider, AvailableToolsProvider):
                visual_tools = AvailableToolsProvider(provider._fields,
                    list(dict.fromkeys([*provider._tools, 'load_images'])))
                visual_tools.enabled = provider.enabled
                context._providers[index] = visual_tools
                break
        else:
            context._providers.insert(0, AvailableToolsProvider(available_tools=['load_images']))
        super().__init__(id=id, name=name, llm=llm, context=context)
        self._role_prompt = role_prompt
        self.work_path = work_path
        self._image_loader = image_loader
        self._image_scope = image_scope
        self.initial_images = []
        self._image_files: dict[ImageReference, str] = {}

    async def start(self, prompt: str, *, images=None) -> None:
        await self._start_visual(prompt, images, keep_history=False)

    async def start_with_history(self, prompt: str, *, images=None) -> None:
        await self._start_visual(prompt, images, keep_history=True)

    async def _start_visual(self, prompt, images, *, keep_history):
        self._image_files.clear()
        scope = self._image_scope() if self._image_scope is not None else nullcontext()
        try:
            async with scope:
                refs = [ImageReference.parse(image) for image in (self.initial_images if images is None else images)]
                await self.load_images(refs)
                if refs:
                    prompt += "\n\n图片引用：" + json.dumps([ref.to_dict() for ref in refs], ensure_ascii=False)
                if keep_history:
                    await super().start_with_history(prompt)
                else:
                    await super().start(prompt)
        finally:
            self._image_files.clear()

    async def load_images(self, images) -> list[ImageReference]:
        refs = [ImageReference.parse(image) for image in images]
        if not refs:
            return []
        if self._image_loader is None:
            raise RuntimeError("未配置图片加载服务")
        file_ids = await self._image_loader(refs, self.work_path)
        if len(file_ids) != len(refs):
            raise RuntimeError("图片加载结果与输入不匹配")
        self._image_files.update(zip(refs, file_ids))
        return refs

    def _build_agent_prompt(self) -> str:
        return (f"你是能够查看图片的 ReAct 智能体。当前工作目录：{self.work_path}。"
                "按用户要求分析图片并返回文字，不臆测无法辨认的内容。"
                "当前消息的图片已经加载；历史或新生成图片可用 load_images 按引用加载。"
                "遇到重要歧义时使用 ask_user。\n" + self._role_prompt + REACT_OUTPUT_FORMAT)

    def _build_messages(self, context: str) -> list[dict]:
        content = [{"type": "text", "text": context}]
        content.extend({"type": "file", "file_id": file_id} for file_id in dict.fromkeys(self._image_files.values()))
        return [{"role": "system", "content": self._build_agent_prompt()},
                {"role": "user", "content": content}]

    async def _think(self) -> AgentDecision:
        state = self.states_manage.get_state()
        await self.context_engine.prepare(state)
        context = self.context_engine.build(state)
        response = await self._llm.chat(self._build_messages(context))
        decision = self._parse_decision(response)
        if decision.think:
            self.states["think"] += decision.think + "\n"
        return decision

    @staticmethod
    def _build_context() -> ContextEngine:
        # Match the effective IM new-executor template, including runtime defaults.
        memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])
        return ContextEngine(providers=[
            AvailableToolsProvider(available_fields=["system", "human"], available_tools=["ask_user"]),
            PinnedContextProvider(),
            MemoryDocumentProvider(),
            SkillProvider(memory),
            HistoryProvider(memory, "agent_history", FullHistoryStrategy() | RecencyStrategy(keep_last=35)),
            UserPromptProvider(),
            ToolOutputProvider(memory, "tool_respond", CompactHistoryStrategy(threshold=100, keep_recent=10)),
            StateProvider(),
            ErrorProvider(memory),
        ], memory=memory)
