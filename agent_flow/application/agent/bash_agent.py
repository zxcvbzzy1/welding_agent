import os

from domain.agent_base import AgentBase
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.tool.builtin import system, ask_user
import infra.tool.tools_attach_methods  # 注册工具中间件和结果回调
from domain.context.context import ContextEngine
from domain.context.providers import (
    AvailableToolsProvider, ErrorProvider, HistoryProvider, PinnedContextProvider,
    SkillProvider, StateProvider, ToolOutputProvider, UserPromptProvider,
)
from domain.context.strategy import CompactHistoryStrategy, FullHistoryStrategy
from domain.memory.long.providers import MemoryDocumentProvider
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory

from infra.context.runtime import bind_context_runtime
from application.services.llm_streaming import StreamingObservableLLMClient

# LLM 客户端配置
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


# CLI agent 操作主体
class OperatorExecutor(AgentBase):
    """负责系统操作步骤的 ReACT executor"""

    def _build_agent_prompt(self) -> str:
        return f"""
你是一个系统操作执行者，当前工作目录为：{self.work_path}
你可以使用 bash 工具执行命令来完成任务。需求、设计或外部副作用边界不明确时，先调用 ask_user 澄清。

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
  "final": "最终结果"
}}
"""
    
# 手动创建本示例独有的记忆和上下文，不依赖 config.py 的示例实例。

operator_memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])
operator_context = ContextEngine(
    providers=[
        AvailableToolsProvider(available_fields=["system", "human"], available_tools=["ask_user"]),
        PinnedContextProvider(),
        MemoryDocumentProvider(),
        SkillProvider(operator_memory),
        HistoryProvider(operator_memory, "agent_history", FullHistoryStrategy()),
        UserPromptProvider(),
        ToolOutputProvider(operator_memory, "tool_respond", CompactHistoryStrategy()),
        StateProvider(),
        ErrorProvider(operator_memory),
    ],
    memory=operator_memory,
)

# 组装
operator = OperatorExecutor(
    id="operator",
    name="系统操作执行者",
    llm=StreamingObservableLLMClient(llm_client, None, agent_id='operator',
        agent_name='系统操作执行者', agent_type='executor', context_engine=operator_context),
    context=operator_context,
)
operator._llm.agent = operator
bind_context_runtime(operator)
