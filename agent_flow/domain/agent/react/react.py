"""A standalone ReAct executor with an instance-local default context."""
from __future__ import annotations

from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider, PinnedContextProvider, SkillProvider, HistoryProvider,
    UserPromptProvider, ToolOutputProvider, StateProvider, ErrorProvider,
)
from domain.context.strategy import FullHistoryStrategy, RecencyStrategy, CompactHistoryStrategy
from domain.memory.long.providers import MemoryDocumentProvider
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory

REACT_OUTPUT_FORMAT = """
## 输出格式
用 JSON 严格按以下格式回复：
{{
  "think": "你的思考过程",
  "tool_calls": [
    {{
      "tool_name": "工具名",
      "arguments": {{"参数名": "参数值"}},
      "reasoning": "为什么调用这个工具"
    }}
  ],
  "is_finished": false
}}

## 任务完成时输出
{{
  "think": "...",
  "tool_calls": [],
  "is_finished": true,
  "finish_reason": "完成原因",
  "final": "最终结果(string)"
}}
"""


class React(AgentBase):
    def __init__(self, id: str, name: str, llm, context=None, *, role_prompt="", work_path="."):
        super().__init__(id=id, name=name, llm=llm, context=context if context is not None else self._build_context())
        self._role_prompt = role_prompt
        self.work_path = work_path

    def _build_agent_prompt(self) -> str:
        return (f"你是 ReAct 执行者。当前工作目录：{self.work_path}。根据任务选择可用工具。"
                "重要需求不明确时使用 ask_user 澄清。\n" + self._role_prompt + REACT_OUTPUT_FORMAT)

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
