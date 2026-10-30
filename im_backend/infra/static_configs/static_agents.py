from im_backend.infra.agent_flow_bridge.pathing import ensure_agent_flow_path

ensure_agent_flow_path()

from domain.agent.plan.planAgent import PlanAgent  # type: ignore  # noqa: E402
from domain.agent_base import AgentBase  # type: ignore  # noqa: E402
from domain.context.context import ContextEngine  # type: ignore  # noqa: E402
from domain.context.providers import (  # type: ignore  # noqa: E402
    AvailableToolsProvider, ErrorProvider, HistoryProvider, PinnedContextProvider,
    SkillProvider, StateProvider, ToolOutputProvider, UserPromptProvider,
)
from domain.agent.plan.providers import AvailableExecutorsProvider, ExecutorStatusProvider, PlanObservationProvider  # type: ignore  # noqa: E402
from domain.context.strategy import CompactHistoryStrategy, FullHistoryStrategy  # type: ignore  # noqa: E402
from domain.memory.long.providers import MemoryDocumentProvider  # type: ignore  # noqa: E402
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory  # type: ignore  # noqa: E402

nums = 1

class OperatorExecutor(AgentBase):
    """负责系统操作步骤的 ReACT executor"""

    def _build_agent_prompt(self) -> str:
        return f"""
你是一个系统操作执行者，当前工作目录为：{self.work_path}
你可以使用 bash 工具执行命令来完成任务。需求、设计或外部副作用边界不清楚时，先使用 ask_user 提问。

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
    
operators =[]
for i in range(nums): 
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
  operator = OperatorExecutor(
      id=f"operator_{i}",
      name=f"系统操作执行者_{i}",
      llm=None,
      context=operator_context,
  )
  operator.inject_attribute(description='系统操作执行者,可以使用 bash 工具执行命令来完成任务')
  operators.append(operator)


plan_nums = 1
plans =[]

for i in range(plan_nums):
  workflow_memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])
  planner_context = ContextEngine(
      providers=[
          AvailableToolsProvider(available_tools=["ask_user"]),
          PinnedContextProvider(),
          MemoryDocumentProvider(),
          SkillProvider(workflow_memory),
          HistoryProvider(workflow_memory, "agent_history", FullHistoryStrategy()),
          UserPromptProvider(),
          AvailableExecutorsProvider(),
          ToolOutputProvider(workflow_memory, "tool_respond", CompactHistoryStrategy()),
          PlanObservationProvider(),
          ExecutorStatusProvider(),
          StateProvider(),
          ErrorProvider(workflow_memory),
      ],
      memory=workflow_memory,
  )
  planner = PlanAgent(
    id=f"plan_agent_{i}",
    name=f"任务编排者_{i}",
    llm=None,
    context=planner_context)
  planner.inject_attribute(description='计划编排智能体，负责创建计划')
  plans.append(planner)


