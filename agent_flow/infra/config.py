import os

from domain.agent_base import AgentBase
from domain.agent.plan.providers import AvailableExecutorsProvider, ExecutorStatusProvider, PlanObservationProvider
from domain.context.context import ContextEngine
from domain.context.providers import *
from domain.memory.long.providers import MemoryDocumentProvider
from domain.context.strategy import FullHistoryStrategy, LatestOnlyStrategy, RecencyStrategy, TokenBudgetStrategy, CompactHistoryStrategy
from domain.event import ToolEventFactory
from domain.memory.short.default_short_term_memory import DefaultShortTermMemory
from infra.LLM.LLM_infra import LLM_Client, LLM_Model_Provider
from infra.eventbus import EventBus


# 事件总线
bus = EventBus()
# agent注册表
agent_dict = AgentBase.get_instance_dict()
# 工具工厂
factory = ToolEventFactory(prefix="infra")._build()._resigister_bus(bus)
# 默认工具使用模型
llm_client = LLM_Client(
    url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
    model_class="deepseek-v4-flash",
    # model_class="MiniMax-M2.7",
    model_provider=LLM_Model_Provider.DEEPSEEK,
    max_tokens=131072,
)


# 以下是独立智能体的手动组装示例，直接修改列表即可调整上下文。
# 每个智能体单独创建 memory，避免共享工具反馈和对话历史。
# 这里不连接数据库、不创建 IM 容器，也不启动后台任务。
memory = DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])
memory2 = DefaultShortTermMemory(["tool_respond", "agent_history", "error", "skill"])

# ReACT 执行者：稳定的工具定义在前，运行状态和一次性错误在后。
providers = [
    AvailableToolsProvider(
        available_fields=["system", "search", "memory", "write_agent", "human"],
        available_tools=["ask_user"],
    ),
    PinnedContextProvider(),
    MemoryDocumentProvider(),
    StateProvider(),
    ErrorProvider(memory),
    AvailableToolsProvider(["system", "search", "memory", "write_agent", "human", "camera"]),
    SkillProvider(memory),
    HistoryProvider(memory, "agent_history", FullHistoryStrategy()),
    UserPromptProvider(),
    ReActToolFeedbackProvider(
        memory,
        "tool_respond",
        CompactHistoryStrategy(threshold=100, keep_recent=10),
    ),
    StateProvider(),
    ErrorProvider(memory),
]

engine = ContextEngine(
    providers=providers,
    memory=memory,
)

# Planner：独立记忆，只开放澄清工具，并显式列出计划相关 provider。
plan_providers = [
    AvailableToolsProvider(available_tools=["ask_user"]),
    PinnedContextProvider(),
    MemoryDocumentProvider(),
    SkillProvider(memory2),
    HistoryProvider(memory2, "agent_history", FullHistoryStrategy()),
    UserPromptProvider(),
    AvailableExecutorsProvider(),
    ReActToolFeedbackProvider(
        memory2,
        "tool_respond",
        CompactHistoryStrategy(threshold=100, keep_recent=10),
    ),
    PlanObservationProvider(),
    ExecutorStatusProvider(),
    StateProvider(),
    ErrorProvider(memory2),
]

plan_engine = ContextEngine(
    providers=plan_providers,
    memory=memory2,
)

# 在自己的入口中，按顺序注册工具、创建智能体、显式启用摘要，再启动执行：
# from infra.tool.builtin import system, ask_user
# import infra.tool.tools_attach_methods
# from infra.context.runtime import bind_context_runtime
# agent = YourAgent(id="demo", name="示例智能体", llm=llm_client, context=engine)
# bind_context_runtime(agent)  # 使用当前智能体模型生成工具反馈摘要；不启动 IM 服务。
# await agent.start("你的任务")
# 如果不需要摘要，把上面的 CompactHistoryStrategy() 改为 FullHistoryStrategy() 即可。
