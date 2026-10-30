from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from contextlib import asynccontextmanager

from redis.exceptions import RedisError
from domain.run_context import current_execution_id
from infra.runtime import RedisRuntime
from infra.runtime.control import current_execution_guard, ensure_execution_active, ExecutionRevoked

DELTAS = {'llm.delta'}
BUSINESS_EVENTS = {
    'workflow.started', 'workflow.finished', 'workflow.failed', 'run.cancelled',
    'plan.generated', 'plan.replanned', 'wave.completed', 'plan.wave.completed',
    'plan.step.started', 'plan.step.observed', 'plan.step.failed', 'task.updated',
}
SUMMARY_FIELDS = ('event_id', 'version', 'category', 'scope_id', 'run_id', 'execution_id', 'agent_name',
                  'agent_id', 'conversation_id', 'message_id', 'name', 'created_at', 'trace')


def event_summary(event):
    if event.get('version') != 2 or event.get('name') in DELTAS:
        return None
    return {key: event[key] for key in SUMMARY_FIELDS if key in event}


def scope_delivery(event):
    if event.get('version') != 2:
        return None
    if event.get('trace'):
        # Status consumers still need these small identifiers, never execution bodies.
        return {**event_summary(event), 'payload': {k: v for k, v in event.get('payload', {}).items()
                if k in {'run_id', 'message_id', 'agent_id', 'status', 'cancelled', 'run', 'conversation_id', 'execution_health', 'error', 'error_code'}}}
    return event


class EventStreamService:
    """Application-owned event classification; Redis only transports one channel."""

    def __init__(self, store, runtime: RedisRuntime | None = None):
        self._store = store
        self.runtime = runtime or RedisRuntime()
        self.runtime.journal.bind(store)
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    async def _notify_subscribers(self, event):
        for callback in self._subscribers:
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    async def _broadcast(self, category, scope_id, event, *, durable=False):
        if not durable:
            # A lost token chunk must not abort or restart the model's stream.
            if not self.runtime.redis_available:
                return None
            try:
                return await asyncio.wait_for(self.runtime.append(category, scope_id, event), 0.5)
            except (RedisError, OSError, TimeoutError):
                self.runtime.redis_failed()
                return None
        if current_execution_guard.get() is not None:
            async def append():
                await current_execution_guard.get()(publication=True)
                return await self.runtime.append(category, scope_id, event, durable=True)
            return await self.runtime.retry_redis(append)
        for attempt in range(3):
            try:
                return await asyncio.wait_for(self.runtime.append(category, scope_id, event, durable=durable), 5)
            except (RedisError, OSError, TimeoutError):
                if attempt < 2:
                    await asyncio.sleep((0.05, 0.15)[attempt])
                else:
                    raise

    def _event(self, run_id, name, payload):
        run = self._store.find_one('runs', {'run_id': run_id}) or {}
        return dict(event_id=str(uuid.uuid4()), version=2, category='run', run_id=run_id,
                    scope_id=run.get('scope_id') or run_id,
                    conversation_id=run.get('im_conversation_id') or run.get('conversation_id', ''),
                    message_id=run.get('source_message_id') or run.get('message_id', ''),
                    execution_id=payload.get('execution_id') or current_execution_id.get(),
                    agent_id=payload.get('agent_id') or payload.get('executor_id') or payload.get('planner_id', ''),
                    name=name, payload=payload, created_at=time.time())

    async def publish(self, run_id, name, payload, *, event_id=None):
        if name in BUSINESS_EVENTS:
            base = self._event(run_id, name, payload)
            event = await self.publish_scope(base['scope_id'], name, payload, context=base,
                                             event_id=event_id, notify=False)
        else:
            event = self._event(run_id, name, payload)
            if event_id is not None:
                event['event_id'] = event_id
            await self._broadcast('run', run_id, event, durable=True)
            # Only explicit product notifications are projected, never raw traces.
            if name.startswith(('artifacts.', 'human.confirmation.', 'context.')):
                notification = dict(payload)
                notification.pop('arguments', None)
                await self.publish_scope(event['scope_id'], name, notification, context=event, trace=False,
                    event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, event['event_id'] + ':scope')), notify=False)
        await self._notify_subscribers(event)
        return event

    async def publish_context_event(self, run_id, name, payload):
        if not run_id:
            return
        # Different agents update disjoint fields. Never replace another agent's
        # snapshot or recreate a deleted/terminal run from a late publication.
        payload = {**payload, 'run_id': run_id, 'updated_at': time.time()}
        agent_key = payload['agent_id'].encode('utf-8').hex()
        leaf = 'usage' if name == 'context.usage.updated' else 'compaction'
        result = await asyncio.to_thread(self._store.update_operators, 'runs',
            {'run_id': run_id, 'status': {'$in': ['pending', 'running']}},
            {'$set': {f'context_usage.{agent_key}.{leaf}': payload}})
        if result is not None:
            await self.publish(run_id, name, payload)

    async def context_usage(self, run_id):
        run = await asyncio.to_thread(self._store.find_one, 'runs', {'run_id': run_id})
        if run is None:
            raise KeyError('运行不存在')
        items = []
        for row in run.get('context_usage', {}).values():
            compaction = row.get('compaction')
            if compaction and compaction.get('status') == 'started' and run.get('status') in {'finished', 'failed', 'cancelled'}:
                compaction = {**compaction, 'status': 'cancelled' if run['status'] == 'cancelled' else 'failed',
                              'updated_at': run.get('updated_at', time.time())}
            items.append({**(row.get('usage') or {key: value for key, value in (compaction or {}).items()
                         if key in {'agent_id', 'agent_name', 'agent_type', 'model'}}), 'compaction': compaction})
        return items

    async def publish_scope(self, scope_id, name, payload, *, context=None, trace=None, event_id=None, notify=True):
        context = context or {}
        run_id = context.get('run_id') or payload.get('run_id') or (payload.get('run') or {}).get('run_id', '')
        run = self._store.find_one('runs', {'run_id': run_id}) if run_id else None
        run = run or {}
        trace = (name in BUSINESS_EVENTS or name.startswith(('agent.execution.', 'agent.reply.'))
                 or name == 'run.created') if trace is None else trace
        body = dict(payload)
        if trace:
            # Business state/links only. Model outputs stay in run events or messages.
            body = {k: v for k, v in body.items() if k in {
                'run_id', 'message_id', 'agent_id', 'conversation_id', 'scope_id', 'status',
                'cancelled', 'error', 'reason', 'mode', 'phase', 'step_id', 'execution_id',
                'execution_health', 'error_code',
            }}
            if name == 'run.created':
                body['run'] = {k: v for k, v in payload.get('run', {}).items() if k in {'run_id', 'status'}}
            plan = payload.get('plan') or {}
            if isinstance(plan, dict) and 'steps' in plan:
                body['steps'] = [{k: v for k, v in step.items() if k in {'step_id', 'title', 'executor_id', 'status'}}
                                 for step in plan['steps']]
            if isinstance(payload.get('step'), dict):
                body['step'] = {k: v for k, v in payload['step'].items()
                                if k in {'step_id', 'title', 'executor_id', 'status', 'status_reason'}}
        event = dict(event_id=event_id or str(uuid.uuid4()), version=2, category='scope',
                     scope_id=scope_id, room_id=scope_id, name=name, payload=body,
                     run_id=run_id, execution_id=context.get('execution_id') or current_execution_id.get(),
                     agent_id=context.get('agent_id') or payload.get('agent_id', ''),
                     agent_name=payload.get('agent_name', ''),
                     conversation_id=context.get('conversation_id') or run.get('im_conversation_id')
                     or payload.get('conversation_id') or run.get('conversation_id', ''),
                     message_id=context.get('message_id') or run.get('source_message_id')
                     or payload.get('message_id', ''), trace=trace, created_at=time.time())
        if run_id:
            body.setdefault('run_id', run_id)
        if event['message_id']:
            body.setdefault('message_id', event['message_id'])
        await self._broadcast('scope', scope_id, event, durable=True)
        if notify:
            await self._notify_subscribers(event)
        return event

    async def no_store_publish(self, run_id, name, payload):
        event = self._event(run_id, name, payload)
        await self._broadcast('run', run_id, event)
        return event

    @asynccontextmanager
    async def execution(self, run_id, agent, *, phase='execute', step_id='', execution_id=None):
        await ensure_execution_active()
        token = current_execution_id.set(execution_id or str(uuid.uuid4()))
        payload = {'run_id': run_id, 'agent_id': agent.id, 'agent_name': getattr(agent, 'name', agent.id),
                   'phase': phase, 'step_id': step_id}
        try:
            context = self._event(run_id, 'agent.execution.started', payload)
            await self.publish_scope(context['scope_id'], 'agent.execution.started', payload, context=context)
            try:
                yield
            except asyncio.CancelledError as exc:
                if not isinstance(exc, ExecutionRevoked) and exc.args not in {('execution_revoked',), ('worker_shutdown',)}:
                    binding = current_execution_guard.set(None)
                    try:
                        await self.publish_scope(context['scope_id'], 'agent.execution.cancelled', payload, context=context)
                    finally:
                        current_execution_guard.reset(binding)
                raise
            except Exception as exc:
                await self.publish_scope(context['scope_id'], 'agent.execution.failed', {**payload, 'error': str(exc)}, context=context)
                raise
            else:
                failed = getattr(agent, 'states', {}).get('is_finished') is False and phase == 'execute'
                await self.publish_scope(context['scope_id'], 'agent.execution.failed' if failed else 'agent.execution.finished',
                                         payload, context=context)
        finally:
            current_execution_id.reset(token)

    async def list_events(self, run_id, *, user_id='service'):
        if current_execution_guard.get() is not None:
            return await self.runtime.retry_redis(
                lambda: self.runtime.journal.events('run', run_id, user_id=user_id), revalidate=True)
        return await self.runtime.journal.events('run', run_id, user_id=user_id)

    async def summaries(self, run_id, *, execution_id=None, after=None, limit=100, user_id='service'):
        events = await self.list_events(run_id, user_id=user_id)
        anchor = next((e for e in events if e['event_id'] == after), None) if after else None
        if after and anchor is None:
            raise KeyError('事件游标不存在')
        items = [e for e in events if event_summary(e) is not None
                 and (not execution_id or e.get('execution_id') == execution_id)
                 and (not anchor or (e.get('created_at', 0), e['event_id']) >
                      (anchor.get('created_at', 0), anchor['event_id']))]
        return {'items': [event_summary(e) for e in items[:limit]],
                'next_cursor': items[limit - 1]['event_id'] if len(items) > limit else None}

    async def get_event(self, run_id, event_id, *, user_id='service'):
        return next((e for e in await self.list_events(run_id, user_id=user_id) if e['event_id'] == event_id), None)

    async def stream(self, run_id, last_id=None, *, summary=False, execution_id=None, user_id='service'):
        def transform(event):
            if execution_id and event.get('execution_id') != execution_id:
                return None
            return event_summary(event) if summary else event
        async for raw in self.runtime.stream('run', run_id,
                lambda: self.runtime.journal.history('run', run_id), last_id=last_id,
                transform=transform, user_id=user_id):
            yield raw

    def format_sse(self, event, cursor=None):
        return self.runtime.sse(event, cursor)
