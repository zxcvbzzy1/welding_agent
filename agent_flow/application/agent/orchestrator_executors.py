import os
from domain.agent.plan.orchestrator import OrchestratorState, PlanOrchestrator
from domain.agent.plan.planAgent import PlanAgent
from domain.agent.plan.providers import (
    AvailableExecutorsProvider, ExecutorStatusProvider, PlanObservationProvider, PlanStepPromptProvider,
)
from domain.context.context import ContextEngine
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.eventbus import EventBus


from domain.context.providers import (
    AvailableToolsProvider, ErrorProvider, HistoryProvider, PinnedContextProvider,
    SkillProvider, StateProvider, ToolOutputProvider, UserPromptProvider,
)
from domain.context.strategy import CompactHistoryStrategy, FullHistoryStrategy
from domain.memory.long.providers import MemoryDocumentProvider
from infra.tool.builtin import ask_user
import infra.tool.tools_attach_methods  # 注册工具中间件和结果回调
from infra.context.runtime import bind_context_runtime, clarify_invalid_replan
from application.services.llm_streaming import StreamingObservableLLMClient

# LLM 客户端配置
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.minimaxi.com/v1"),
    model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.MINMAX,
    max_tokens=131072,
)


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

# 编排者上下文
step_context = ContextEngine(
    providers=[
        PlanStepPromptProvider(),
    ],
    memory=DefaultShortTermMemory(["tool_respond", "agent_history", "error"]),
)


# 组装 planner 和 executors
planner = PlanAgent(
    id="plan_agent",
    name="任务编排者",
    llm=StreamingObservableLLMClient(llm_client, None, agent_id='plan_agent',
        agent_name='任务编排者', agent_type='planner', context_engine=planner_context),
    context=planner_context,
)

planner._llm.agent = planner
bind_context_runtime(planner)

executors ={}
orchestrator = PlanOrchestrator(
    planner=planner,
    executors=executors,
    step_context_engine=step_context,
    event_bus=EventBus(),
    state=OrchestratorState(),
    max_replan_rounds=3,
    on_invalid_replan=clarify_invalid_replan,
)
