import inspect
import logging
from contextvars import ContextVar

from domain.event import EventBusReturn, Event
from domain.tool import Tool_respond
from domain.run_context import current_agent, current_run_id
from infra.runtime.control import ensure_execution_active
from domain.runtime_hooks import get_tool_event_observer
from infra.event_bind import On_bind, ToolResultDeliveryError
from infra.eventbus import log_exception_once
from infra.config import factory, agent_dict, bus
from infra.tool.common_func import HumanCollaborationAuditor


# factory = StaticToolEventFactory(prefix="infra") #编写时方便
on_tool = On_bind()
human_auditor = HumanCollaborationAuditor(factory, bus)
log = logging.getLogger(__name__)
_current_tool_completion = ContextVar('tool_completion', default=None)

# Builtin modules own their Tool declarations and called/human handlers, like
# system.py. Import both welding tools here as well as in the application loader
# so the event-binding entrypoint is complete for standalone event-bus users.
from infra.tool.builtin import point_cloud_welding, robot, robot_simulation  # noqa: E402,F401


# 中间件

# 必须位于最外层，覆盖审核、权限检查及工具处理器的普通执行异常。
@on_tool.use()
async def tool_exception_middleware(event: Event, call_next):
    if not event.name.endswith('.called'):
        return await call_next()
    try:
        spec = factory.get_spec(event.name)
    except KeyError:
        return await call_next()
    agent_id = event.unpack().get('agent_id', '')
    completion = {'agent_id': agent_id, 'tool_name': spec.tool_name,
                  'delivery_started': False, 'completed': False}
    token = _current_tool_completion.set(completion)
    try:
        try:
            return await call_next()
        except ToolResultDeliveryError:
            raise
        except Exception as exc:
            if completion['completed'] or completion['delivery_started']:
                raise
            failure = Tool_respond(agent_id=agent_id, name=spec.tool_name, success=False,
                respond=f'工具 {spec.tool_name} 执行异常（{type(exc).__name__}）：{exc}')
            try:
                return await spec.emit_failed(failure)
            except ToolResultDeliveryError:
                raise
            except Exception as delivery_exc:
                raise ToolResultDeliveryError(f'工具失败结果回灌失败: {spec.tool_name}: {delivery_exc}') from delivery_exc
    finally:
        _current_tool_completion.reset(token)


async def _record_tool_result(agent, tool_name, success, respond, event):
    completion = _current_tool_completion.get()
    if completion is not None and (completion['agent_id'], completion['tool_name']) != (agent.id, tool_name):
        completion = None
    if completion is not None:
        if completion['delivery_started'] or completion['completed']:
            return
        # Set before awaiting: duplicate results may arrive concurrently.
        completion['delivery_started'] = True
    try:
        await agent.on_tool_call(tool_name=tool_name, success=success, respond=respond)
    except Exception as exc:
        name = event.name if event is not None else tool_name
        raise ToolResultDeliveryError(f'工具结果回调失败: {name}: {exc}') from exc
    if completion is not None:
        completion['completed'] = True


# logging middleware
@on_tool.use()
async def logging_middleware(event: Event, call_next):
    event_payload = event.unpack()
    agent_id = event_payload.get("agent_id", "Unknown")
    print(f"[LOG] {agent_id} 事件触发: {event.name}")
    # print(f"{event.name}负载为:\n {event.payload}")

    try:
        result = await call_next()
        print(f"[LOG]  {agent_id}  事件完成: {event.name}")
        return result

    except Exception as e:
        try:
            tool_name = factory.get_spec(event.name).tool_name
        except KeyError:
            tool_name = event_payload.get('tool_name', '')
        log_exception_once(log, e, '[ERROR] %s 事件异常: %s -> %s run=%s tool=%s',
                           agent_id, event.name, e, current_run_id.get(), tool_name)
        raise   # ⚠️ 一定要 rethrow


# frontend_sse_bridge middleware
@on_tool.use()
async def frontend_sse_bridge_middleware(event: Event, call_next):
    try:
        observer = get_tool_event_observer()
        if observer is not None:
            result = observer.on_tool_event(event)
            if inspect.isawaitable(result):
                await result
    except Exception as exc:
        print(f"[SSE-BRIDGE] 工具事件镜像失败: {event.name} -> {exc}")
    return await call_next()


# human_collaboration middleware
@on_tool.use()
async def human_collaboration_middleware(event: Event, call_next):
    return await human_auditor.handle(event, call_next)


# execution_guard middleware：放在人工审批之后，等待结束后再检查执行权限。
@on_tool.use()
async def execution_guard_middleware(event: Event, call_next):
    if event.name.endswith('.called'):
        await ensure_execution_active()
    return await call_next()


# 成功/失败返回事件
@on_tool.on_pattern("*.succeeded")
async def on_tool_success(**kwargs):
    agent_id = kwargs.get("agent_id")
    event = kwargs.get("_event")
    if event is not None:
        try:
            factory.get_spec(event.name)
        except KeyError:
            return  # Other lifecycle events are not tool observations.
    # per-run 隔离：优先用 contextvar 里的“本实例”，回退到全局 agent_dict[agent_id]（向后兼容）。
    agent = current_agent.get() or agent_dict.get(agent_id)
    if agent is None:
        # 无法定位 agent（contextvar 未设且全局映射缺失）：优雅跳过，避免 AttributeError。
        print(f"[tool] 无法定位 agent，跳过 succeeded 回调: agent_id={agent_id}")
        return EventBusReturn(agent_id=agent_id, src_object=event, results=kwargs.get("respond"), success=True)
    await _record_tool_result(
        agent=agent,
        tool_name=kwargs.get("name"),
        success=True,
        respond=kwargs.get("respond"),
        event=event,
    )
    return EventBusReturn(agent_id=agent_id,src_object=event, results=kwargs.get("respond"), success=True)

@on_tool.on_pattern("*.failed")
async def on_tool_fail(**kwargs):  # event.playload为Tool_respond类，kwargs为Tool_respond类解构可字典调用
    agent_id = kwargs.get("agent_id")
    event = kwargs.get("_event")
    if event is not None:
        try:
            factory.get_spec(event.name)
        except KeyError:
            return
    # per-run 隔离：优先用 contextvar 里的“本实例”，回退到全局 agent_dict[agent_id]（向后兼容）。
    agent = current_agent.get() or agent_dict.get(agent_id)
    if agent is None:
        print(f"[tool] 无法定位 agent，跳过 failed 回调: agent_id={agent_id}")
        return EventBusReturn(agent_id=agent_id, src_object=event, results="工具调用失败事件处理完成", success=False)
    await _record_tool_result(
        agent=agent,
        tool_name=kwargs.get("name"),
        success=False,
        respond=kwargs.get("respond"),
        event=event,
    )
    return EventBusReturn(agent_id=agent_id, src_object=event, results="工具调用失败事件处理完成", success=False)

# @on_tool.on(factory.tool("query_tool_respond").succeeded({}))
# async def on_query_tool_respond_tool_successed(**kwargs):  
#     agent_id = kwargs.get("agent_id")
#     agent:AgentBase = agent_dict.get(agent_id)
#     full_respond = kwargs.get("respond")
#     def callBack() -> str:
#         summary = f"{full_respond}" 
#         return summary           
#     await agent.on_tool_call(
#         tool_name=kwargs.get("name"),
#         success=True,
#         respond=kwargs.get("respond"),
#         callBack=callBack
#     )
