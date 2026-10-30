# 重规划与上下文运行支持

重规划先验证候选图，再整体替换 pending 步骤。保留所有历史步骤，但新依赖不能引用失败／跳过的历史、被替换步骤或形成环；失败候选不会改动旧计划。每个波次最多连续尝试三次，再通过 `on_invalid_replan` 回调复用 `ask_user`，由用户选择继续旧计划或补充要求后重新规划。

`infra/config.py` 是完整展开的独立组装示例：直接展示模型、两份独立记忆、provider 列表和 `ContextEngine`，保留已有模块级导出，不提供业务构建函数。IM 种子和 CLI 示例在各自文件显式组装；应用服务继续使用 `ContextService` 创建每次执行的独立上下文。Planner 默认开放 `ask_user`；执行者可同时使用普通工具和问答。显式工具配置沿用原有入口。业务模型调用日志包含 agent、context、阶段和实际注入的工具名称，可用于区分工具配置问题和模型未选择调用。

工具反馈按真实写入顺序保留。`ContextEngine.prepare()` 在构建输入前运行策略管道：达到 100 条时，以当前智能体模型将旧反馈归纳为最多 4000 tokens 的摘要，代码另外完整保留最新 10 条原文，合并写入 `__previous_tool_feedback__`。并行轮次超过阈值的反馈统一处理。压缩块计作一条，随后继续追加；`prepare_start()` 清空字段时同时清除此块。替换使用短期记忆的前缀比较接口，等待期间追加的反馈不丢失。

摘要和通知在智能体组装位置通过 `infra/context/runtime.py` 的回调显式注入，工具结果回调只处理已注册工具事件，不自动安装摘要支持。独立示例显式调用 `bind_context_runtime(agent)`；不需要摘要时可使用 `FullHistoryStrategy`。domain 不依赖模型客户端、HTTP 或 Redis。摘要输入按模型容量分批归纳，最多尝试三次；失败保留原文，取消和撤权不会提交压缩结果。每次模型调用仍执行共享运行权限检查；Redis 不可达时等待恢复，不重复执行已完成工具。失败后若完整输入超出容量，则复用问答工具等待用户选择重试或取消；若完整输入仍可容纳，则继续使用原文。

`ContextService` 启动时迁移已有 executor／planner 上下文，版本为 `runtime_context_version=1`。迁移补齐问答工具、替换工具反馈策略、调整 provider 顺序，保留对话历史等其他策略；条件更新与缓存失效支持重复启动。正在执行的 engine 不受影响，新执行读取新配置。上线前排空旧执行，再整体切换 worker。

## 输入用量

业务模型包装器复用本次构建记录的模块边界，用 `EstimatedTokenCounter`（`cjk-char-latin-4-v1`）统计系统指令、各模块及消息格式开销。分项之和等于总量；该值是估算值，不是模型服务商的账单 token 数。统计不会再次运行 provider，也不会重复消费一次性错误。

每个 run、agent 仅保留最近一次业务调用快照；包含模型、阶段、时间、容量、估算方法与模块计数，不持久化上下文原文。压缩调用单独记录状态，不覆盖业务快照。`context.usage.updated`、`context.compaction.started/finished/failed` 通过现有事件服务推送；刷新时由 `GET /api/runs/{run_id}/context-usage` 或 IM 的 `GET /api/im/runs/{run_id}/context-usage` 恢复，后者校验任务发起人。Redis 不可达时查询仍从 MongoDB 读取。

`LLM_Client.context_window` 与输出上限 `max_tokens` 分开，可通过构造参数显式覆盖。Provider 默认值仅以指定模型作参考，不代表旗下全部模型；官方配置核对日期为 2026-10-29：

| Provider | 参考模型 | 容量 | 官方配置 |
| --- | --- | ---: | --- |
| GLM | GLM-5 | 202752 | https://huggingface.co/zai-org/GLM-5/raw/main/config.json |
| DEEPSEEK | DeepSeek-V4-Flash | 1048576 | https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/raw/main/config.json |
| MINMAX | MiniMax-M2.7 | 204800 | https://huggingface.co/MiniMaxAI/MiniMax-M2.7/raw/main/config.json |

输入框共用 `ContextUsage.vue`：饼图显示输入／容量，点击显示模块横向堆叠图、token 数和占比。群聊默认 Planner，切换执行者时展示独立统计，不跨智能体相加。没有业务调用时显示暂无统计。ECharts 按官方按需导入方式注册饼图、柱状图、Tooltip、Grid 和 SVGRenderer：https://echarts.apache.org/handbook/en/basics/import/。

## 工具执行异常

最外层工具异常中间件仅捕获已注册工具调用中的普通执行异常，复用标准 `failed` 事件回灌工具名、异常类型和信息。单次调用使用上下文标记避免重复完成；并行同名工具相互隔离，已经回灌后再发生异常时直接向上传播，不再追加失败结果。工具不会被自动重跑。

`publish()` 保持广播收集结果的行为；`publish_one()` 对收集到的异常重新抛出。`On_bind` 返回事件的投递和工具结果回调异常以 `ToolResultDeliveryError` 向上传播，避免失败回灌自身失败时递归补发或静默等待。正常无订阅、空返回及没有完成反馈的行为不变；取消和执行撤权原样传播，不增加等待超时策略。

工具调用异常由原有 `logging_middleware` 统一记录堆栈及运行、智能体和工具标识；异常回灌中间件不再额外打印。总线只兜底记录尚未记录的异常，沿显式异常原因链避免包装后重复打印；执行失败后发生的新回调错误仍单独记录。

异步 bash 子进程显式接收的环境映射会复制为普通 `dict`，兼容 uvloop；`None` 保持环境继承，现有审核与 30 秒执行上限不变。
