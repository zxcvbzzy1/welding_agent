from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from redis.exceptions import RedisError
from pymongo.errors import PyMongoError
from im_backend.api.routes.files import file_error

from im_backend.api.core import get_sse_user, get_current_user, get_im_service
from im_backend.application.services.facade import IMService


router = APIRouter()


class ConfirmationResolveRequest(BaseModel):
    approved: bool
    reason: str = ""


class QuestionAnswerRequest(BaseModel):
    answers: list[dict] = Field(min_length=1, max_length=3)
    file_ids: list[str] = Field(default_factory=list, max_length=10)


@router.get('/runs/{run_id}/context-usage')
async def run_context_usage(run_id: str, service: IMService = Depends(get_im_service),
                            current_user: dict = Depends(get_current_user)):
    try:
        _, source = await service._question_source(run_id)
        if source['sender_id'] != current_user['user_id']:
            raise HTTPException(status_code=403, detail='只能查看自己发起的运行上下文')
        return {'items': await service._bridge.events.context_usage(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/active")
async def list_active_runs(
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    """全局「正在运行的智能体」视图：单聊回复任务 + 群聊编排 run，附最近结束的 run。

    桌面端进程页/顶栏徽标轮询本接口；items 为活跃（可中断），recent 为最近结束（只读）。
    """
    _ = current_user
    return await service.monitor.list_active_runs()


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_any_run(
    run_id: str,
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    """通用中断：run_id 可以是编排 run_id，也可以是单聊回复的 user message_id。"""
    _ = current_user
    try:
        return await service.monitor.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/confirmations")
async def list_run_confirmations(
    run_id: str,
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    """列出该 run 待处理的人工确认（前端重连后可补拉，避免错过 SSE）。"""
    _ = current_user
    try:
        return {"items": await service.list_human_confirmations(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RedisError, OSError, PyMongoError) as exc:
        raise HTTPException(status_code=503, detail="连接暂时不可用，请重试") from exc


@router.post("/runs/{run_id}/confirmations/{confirmation_id}/answer")
async def answer_run_question(run_id: str, confirmation_id: str, request: QuestionAnswerRequest,
                              service: IMService = Depends(get_im_service),
                              current_user: dict = Depends(get_current_user)):
    try:
        return await service.answer_question(run_id=run_id, confirmation_id=confirmation_id,
            user_id=current_user['user_id'], answers=request.answers, file_ids=request.file_ids)
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="连接暂时不可用，已保存的回答可重试提交") from exc
    except (ValueError, KeyError, PyMongoError) as exc:
        raise file_error(exc) from exc


@router.post("/runs/{run_id}/confirmations/{confirmation_id}")
async def resolve_run_confirmation(
    run_id: str,
    confirmation_id: str,
    request: ConfirmationResolveRequest,
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    """解析一条人工确认（允许/拒绝），唤醒在 agent_flow 侧等待的工具调用。"""
    _ = current_user
    try:
        item = await service._bridge.runtime.get_confirmation(confirmation_id)
        if item and item.get('kind') == 'question':
            raise ValueError('问题必须通过答案接口提交')
        return await service._bridge.human_confirmations.resolve(
            run_id=run_id,
            confirmation_id=confirmation_id,
            approved=request.approved,
            reason=request.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="确认请求不存在或已处理") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def require_run(service, run_id):
    if not service._store.find_one("runs", {"run_id": run_id}):
        raise HTTPException(status_code=404, detail="运行不存在")


@router.get("/runs/{run_id}/events")
async def list_run_events(
    run_id: str,
    view: str = Query(default="full", pattern="^(full|summary)$"),
    execution_id: str | None = None,
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    if view == "summary":
        require_run(service, run_id)
        try:
            return await service._bridge.events.summaries(run_id, execution_id=execution_id, after=after, limit=limit, user_id=current_user['user_id'])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": await service._bridge.events.list_events(run_id, user_id=current_user['user_id'])}


@router.get("/runs/{run_id}/events/stream")
async def stream_run_summaries(
    run_id: str,
    execution_id: str | None = None,
    last_event_id: str | None = Header(default=None),
    last_id: str | None = None,
    current_user: dict = Depends(get_sse_user),
    service: IMService = Depends(get_im_service),
):
    require_run(service, run_id)
    return StreamingResponse(
        service._bridge.events.stream(run_id, last_id=last_event_id or last_id, summary=True, execution_id=execution_id, user_id=current_user['user_id']),
        media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


@router.get("/runs/{run_id}/events/{event_id}")
async def get_run_event(
    run_id: str, event_id: str,
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    require_run(service, run_id)
    event = await service._bridge.events.get_event(run_id, event_id, user_id=current_user['user_id'])
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return {"item": event}


@router.get("/scopes/{scope_id}/events/{event_id}")
async def get_scope_event(
    scope_id: str, event_id: str,
    service: IMService = Depends(get_im_service),
    current_user: dict = Depends(get_current_user),
):
    event = await service._events.get_event(scope_id, event_id, user_id=current_user['user_id'])
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return {"item": event}
