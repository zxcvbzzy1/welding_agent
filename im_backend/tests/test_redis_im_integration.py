"""Business/API regression coverage against isolated Mongo and Redis namespaces."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from redis.exceptions import ConnectionError
from pymongo.errors import OperationFailure
from im_backend.tests.test_redis_runtime import runtime, decode

import httpx
import pytest
import pytest_asyncio

from im_backend.api import core
from im_backend.api.index import app
from im_backend.application.services.messaging.agent_builder import AgentBuilderService
from im_backend.application.services.orchestration.runs import GroupRunService
from im_backend.infra.storage.files import LocalFileStorage
from im_backend.infra.agent_flow_bridge.pathing import ensure_agent_flow_path
from domain.event import Event
from domain.state import Plan
from domain.run_context import current_run_id
from infra.runtime.control import ExecutionRevoked

ensure_agent_flow_path()

from application.services.run_state import RunStateService


@pytest_asyncio.fixture
async def backend(monkeypatch, tmp_path):
    db = 'agenthub_redis_test_' + uuid.uuid4().hex
    prefix = 'agenthub-test:' + uuid.uuid4().hex
    monkeypatch.setenv('IM_MONGO_DB', db)
    monkeypatch.setenv('REDIS_KEY_PREFIX', prefix)
    monkeypatch.setenv('IM_ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    container = core.IMContainer()
    container.files.storage = LocalFileStorage(tmp_path / 'uploads')
    monkeypatch.setattr(core, 'get_container', lambda: container)
    await container.bridge.runtime.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/im/auth/register', json={
            'username':'redis-user', 'email':'redis@example.com', 'password':'test-password'})
        headers = {'Authorization': 'Bearer ' + response.json()['item']['token']}
        try:
            yield container, client, headers
        finally:
            await container.bridge.runtime.close()
            r = container.bridge.runtime
            keys = [k async for k in r.redis.scan_iter(match=prefix + ':*')]
            if keys:
                await r.redis.delete(*keys)
            await r.redis.aclose()
            assert container.store._db.name == db
            container.store._client.drop_database(db)
            container.store.close()


async def post(client, path, headers, body):
    response = await client.post('/api/im' + path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()['item']


async def wait_finished(runtime, kind, target):
    for _ in range(100):
        state = await runtime.get_state(kind, target)
        if state and state['status'] not in {'pending','running'}:
            return state
        await asyncio.sleep(.05)
    pytest.fail(f'Task never finished: {kind}:{target}')


@pytest.mark.asyncio
async def test_dm_reply_artifact_history_state_and_cleanup(backend, monkeypatch):
    container, client, headers = backend
    original = container.bridge.agents.build_run_agent
    instances = []
    def build(agent_id):
        agent = original(agent_id)
        instances.append(agent)
        async def execute(prompt):
            await container.bridge.frontend_bridge.mirror_tool_event(Event('artifacts.document', {
                'agent_id': agent_id, 'artifact_type':'document',
                'artifact':{'type':'document','title':'result','content':'hello'}}))
            await container.bridge.events.no_store_publish(current_run_id.get(), 'llm.delta', {'delta':'answer'})
            agent.states['final'] = 'answer'
        agent.start_with_history = execute
        return agent
    monkeypatch.setattr(container.bridge.agents, 'build_run_agent', build)
    conv = await post(client, '/agents/default_executor/conversations', headers, {})
    cid = conv['conversation_id']
    await post(client, '/favorites', headers, {'scope_type':'conversation','scope_id':cid,'content':'remember'})
    mid = (await post(client, f'/conversations/{cid}/messages', headers,
        {'content_parts':[{'type':'text','text':'hello'}]}))['message_id']
    run_id = (await post(client, f'/conversations/{cid}/reply', headers, {'message_id':mid}))['run_id']
    assert (await wait_finished(container.bridge.runtime, 'dm_reply', run_id))['status'] == 'finished'
    response = await client.get(f'/api/im/conversations/{cid}/messages')
    messages = response.json()['items']
    assert messages[0]['status'] == 'finished'
    reply = next(m for m in messages if m['sender_type']=='agent')
    assert reply['metadata']['reply_to'] == mid
    assert any(p['type']=='artifact' for p in reply['content_parts'])
    assert (await container.bridge.events.list_events(run_id))
    assert instances[0].states['pinned_context']
    r = await client.delete(f'/api/im/conversations/{cid}', headers=headers)
    assert r.status_code == 200, r.text
    runtime = container.bridge.runtime
    assert await runtime.get_state('dm_reply', run_id) is None
    assert await runtime.redis.exists(runtime.event_key('scope', cid)) == 0
    assert (await container.bridge.events.list_events(run_id)) == []


@pytest.mark.asyncio
async def test_group_first_event_association_and_final_reply(backend, monkeypatch):
    container, client, headers = backend
    original = container.bridge.agents.build_run_agent
    first_seen = []
    async def observe(event):
        if event['name'] == 'workflow.started':
            first_seen.append(container.store.find_one('im_messages', {'run_id':event['run_id']}))
    container.bridge.events.subscribe(observe)
    def build(agent_id):
        agent = original(agent_id)
        if agent_id == 'default_planner':
            async def generate(state, executor_ids):
                return Plan(steps=[])
            async def summary(state):
                await container.bridge.events.publish(current_run_id.get(), 'planner.final',
                    {'planner_id':agent_id,'final':'group answer'})
                return 'group answer'
            agent.generate_plan = generate
            agent.summarize_result = summary
        return agent
    monkeypatch.setattr(container.bridge.agents, 'build_run_agent', build)
    room = await post(client, '/rooms', headers, {'member_agent_ids':['default_executor'],'title':'room'})
    rid = room['room_id']
    conv = await post(client, f'/rooms/{rid}/conversations', headers, {})
    message = await post(client, f'/rooms/{rid}/messages', headers,
        {'conversation_id':conv['conversation_id'],'content_parts':[{'type':'text','text':'do work'}]})
    dispatched = await post(client, f'/rooms/{rid}/dispatch', headers, {'message_id':message['message_id']})
    run_id = dispatched['run']['run_id']
    state = await wait_finished(container.bridge.runtime, 'orchestration', run_id)
    assert state['status'] == 'finished', state
    assert first_seen and first_seen[0]['message_id'] == message['message_id']
    msgs = (await client.get(f'/api/im/rooms/{rid}/messages')).json()['items']
    assert any(m['sender_type']=='agent' and m['content_parts'][0]['text']=='group answer' for m in msgs)
    tasks = (await client.get(f'/api/im/rooms/{rid}/tasks')).json()['items']
    assert tasks[0]['status'] == 'finished'
    assert [task['run_id'] for task in tasks] == [run_id]
    entries = await container.bridge.runtime.redis.xrange(container.bridge.runtime.event_key('scope', rid))
    names = [json.loads(f['event'])['name'] for _, f in entries]
    assert names.index('run.created') < names.index('workflow.started')
    assert names[-1] == 'workflow.finished'
    assert (await client.delete(f'/api/im/rooms/{rid}',headers=headers)).status_code == 200
    assert await container.bridge.runtime.get_state('orchestration', run_id) is None


@pytest.mark.asyncio
async def test_cross_worker_dm_cancel_and_monitor(backend, monkeypatch, tmp_path):
    container, client, headers = backend
    original = container.bridge.agents.build_run_agent
    entered = asyncio.Event()
    def build(agent_id):
        agent = original(agent_id)
        async def execute(prompt):
            entered.set()
            await asyncio.Event().wait()
        agent.start_with_history = execute
        return agent
    monkeypatch.setattr(container.bridge.agents,'build_run_agent',build)
    cid = (await post(client, '/agents/default_executor/conversations',headers,{}))['conversation_id']
    mid = (await post(client,f'/conversations/{cid}/messages',headers,{'content_parts':[{'type':'text','text':'wait'}]}))['message_id']
    run_id = (await post(client,f'/conversations/{cid}/reply',headers,{'message_id':mid}))['run_id']
    await asyncio.wait_for(entered.wait(),3)
    peer = core.IMContainer()
    peer.files.storage = LocalFileStorage(tmp_path/'peer-uploads')
    await peer.bridge.runtime.start()
    monkeypatch.setattr(core,'get_container',lambda:peer)
    try:
        assert (await container.bridge.runtime.get_state('dm_reply',run_id))['status']=='running'
        active = (await client.get('/api/im/runs/active',headers=headers)).json()['items']
        assert any(r['run_id']==run_id for r in active)
        response = await client.post(f'/api/im/conversations/{cid}/messages/{mid}/cancel',headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()['item']['cancel_requested'] is True
        state = await wait_finished(container.bridge.runtime,'dm_reply',run_id)
        assert state['status']=='cancelled'
    finally:
        await peer.bridge.runtime.close()
        peer.store.close()


@pytest.mark.asyncio
async def test_only_native_agents_can_be_created_or_loaded(backend):
    container, _, _ = backend
    with pytest.raises(ValueError, match='仅支持 native'):
        container.agents.create_agent(
            name='legacy', metadata={'agent_kind': 'external'}, owner_user_id='owner'
        )

    legacy_id = 'legacy-external-agent'
    container.store.update_one(
        'agents',
        {'agent_id': legacy_id},
        {
            'agent_id': legacy_id,
            'name': 'legacy',
            'agent_type': 'executor',
            'context_id': 'default_executor',
            'metadata': {'agent_kind': 'external'},
        },
        upsert=True,
    )
    try:
        assert legacy_id not in {
            item['agent_id'] for item in container.agents.list_visible_agents('owner')
        }
        with pytest.raises(ValueError, match='仅支持 native'):
            container.bridge.get_agent(legacy_id)
    finally:
        container.store.delete_one('agents', {'agent_id': legacy_id})


@pytest.mark.asyncio
async def test_agent_builder_returns_native_shape(backend):
    container, _, _ = backend

    class LLM:
        async def chat(self, _messages):
            return (
                '已整理。\n```json\n'
                '{"name":"Planner","agent_type":"planner","tool_names":["bash"],"ready":true}'
                '\n```'
            )

    result = await AgentBuilderService(agents=container.agents, llm=LLM()).chat(
        messages=[{'role': 'user', 'content': '创建 planner'}], draft=None
    )
    assert result['ready'] is True
    assert result['draft']['agent_type'] == 'planner'
    assert result['draft']['tool_names'] == []
    assert 'agent_kind' not in result['draft']


@pytest.mark.asyncio
async def test_standalone_agent_flow_routes_use_shared_runtime(backend, monkeypatch):
    container, _, _ = backend
    from api.core import dependencies
    from api import index
    monkeypatch.setattr(dependencies, 'get_container', lambda: container.bridge)
    monkeypatch.setattr(index, 'get_container', lambda: container.bridge)
    # The bridge is deliberately usable as the runtime service container.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=index.app), base_url='http://test') as client:
        conversation = (await client.post('/api/conversations',json={'title':'standalone'})).json()['item']
        cid = conversation['conversation_id']
        response = await client.post(f'/api/conversations/{cid}/messages',json={'role':'user','content':'hello'})
        assert response.status_code == 200, response.text
        message = response.json()['item']
        response = await client.post('/api/runs',json={'prompt':'hello','mode':'react',
            'executor_agent_id':'default_executor','conversation_id':cid,
            'message_id':message['message_id'],'auto_start':False})
        assert response.status_code == 200, response.text
        run_id = response.json()['item']['run_id']
        assert (await client.get(f'/api/runs/{run_id}')).json()['item']['status']=='pending'
        response = await client.post(f'/api/runs/{run_id}/cancel')
        assert response.status_code == 202
        assert (await client.get(f'/api/runs/{run_id}')).json()['item']['status']=='cancelled'
        response = await client.delete(f'/api/conversations/{cid}')
        assert response.status_code==200, response.text
        assert await container.bridge.runtime.get_state('orchestration',run_id) is None


@pytest.mark.asyncio
async def test_file_deletion_publishes_after_threaded_io(backend):
    container, client, headers = backend
    cid = (await post(client,'/agents/default_executor/conversations',headers,{}))['conversation_id']
    response = await client.post('/api/im/files/upload',headers=headers,files={'file':('note.txt',b'hello','text/plain')})
    assert response.status_code==201,response.text
    file_id = response.json()['item']['file_id']
    await post(client,f'/conversations/{cid}/messages',headers,{'content_parts':[{'type':'file','file_id':file_id}]})
    response = await client.delete(f'/api/im/files/{file_id}',headers=headers)
    assert response.status_code==200,response.text
    assert container.files.get(file_id)['status']=='delete'
    records = await container.bridge.runtime.redis.xrange(container.bridge.runtime.event_key('scope', cid))
    assert any(json.loads(f['event'])['name']=='file.deleted' for _,f in records)


@pytest.mark.asyncio
async def test_cache_aside_db_first_no_redis_business_overlay(backend, monkeypatch):
    container, _, _ = backend
    state = container.bridge.runs.states
    rt = container.bridge.runtime
    await state.create({'run_id': 'cache', 'status': 'pending', 'plan': {}})
    assert (await state.get('cache'))['status'] == 'pending'
    assert await rt.redis.get(rt.cache.keys('cache')[0])
    await rt.put_state('orchestration', 'cache', {'status': 'running', 'plan': {'wrong': True}})
    assert (await state.get('cache'))['status'] == 'pending'
    assert 'plan' not in await rt.get_state('orchestration', 'cache')
    real_update = container.store.update_one
    def fail(*args, **kwargs):
        raise OperationFailure('simulated DB failure')
    monkeypatch.setattr(container.store, 'update_one', fail)
    with pytest.raises(OperationFailure):
        await state.update('cache', {'status': 'finished'})
    assert json.loads(await rt.redis.get(rt.cache.keys('cache')[0]))['status'] == 'pending'
    monkeypatch.setattr(container.store, 'update_one', real_update)
    await state.update('cache', {'status': 'finished', 'plan': {'saved': True}})
    assert await rt.redis.get(rt.cache.keys('cache')[0]) is None
    assert (await state.get('cache'))['plan'] == {'saved': True}
    assert (await rt.get_state('orchestration', 'cache'))['status'] == 'running'


@pytest.mark.asyncio
async def test_invalidation_fences_concurrent_old_fill(runtime):
    entered, resume = asyncio.Event(), asyncio.Event()
    async def old_loader():
        entered.set()
        await resume.wait()
        return {'status': 'pending'}
    read = asyncio.create_task(runtime.cache.get('race', old_loader))
    await entered.wait()
    await runtime.cache.invalidate('race')
    resume.set()
    assert (await read)['status'] == 'pending'  # The already in-flight read may finish.
    assert await runtime.redis.get(runtime.cache.keys('race')[0]) is None
    assert await runtime.cache.get('race', lambda: {'status': 'finished'}) == {'status': 'finished'}


@pytest.mark.asyncio
async def test_cache_failure_retries_and_expires(runtime, monkeypatch):
    runtime.cache.ttl = 1
    await runtime.cache.get('ttl', lambda: {'status': 'pending'})
    original_eval = runtime.redis.eval
    calls = 0
    async def broken(*args):
        nonlocal calls
        calls += 1
        raise ConnectionError('cache unavailable')
    monkeypatch.setattr(runtime.redis, 'eval', broken)
    assert not await runtime.cache.invalidate('ttl')
    assert calls == 3
    monkeypatch.setattr(runtime.redis, 'eval', original_eval)
    await asyncio.sleep(1.05)
    assert await runtime.cache.get('ttl', lambda: {'status': 'finished'}) == {'status': 'finished'}
    async def unavailable(*args):
        raise ConnectionError('offline')
    monkeypatch.setattr(runtime.redis, 'mget', unavailable)
    assert await runtime.cache.get('ttl', lambda: {'status': 'database'}) == {'status': 'database'}


@pytest.mark.asyncio
async def test_summary_pagination_details_and_execution_filter(backend):
    container, client, headers = backend
    events = container.bridge.events
    await container.bridge.runs.states.create({'run_id': 'details', 'scope_id': 'room', 'status': 'running'})
    actor = SimpleNamespace(id='agent', name='Agent', states={'is_finished': True})
    for _ in range(2):
        async with events.execution('details', actor):
            await events.publish('details', 'tool.called', {'agent_id': 'agent', 'arguments': {'large': 'x' * 10000}})
            await events.publish('details', 'llm.completed', {'agent_id': 'agent', 'content': 'private body'})
            await events.no_store_publish('details', 'llm.delta', {'delta': 'private token'})
    archived = (await events.list_events('details'))
    ids = {e['execution_id'] for e in archived}
    assert len(ids) == 2
    execution_id = archived[0]['execution_id']
    first = (await client.get('/api/im/runs/details/events', headers=headers,
                             params={'view': 'summary', 'execution_id': execution_id, 'limit': 1})).json()
    assert len(first['items']) == 1 and first['next_cursor']
    assert 'payload' not in first['items'][0]
    second = (await client.get('/api/im/runs/details/events', headers=headers,
                              params={'view': 'summary', 'execution_id': execution_id, 'limit': 1,
                                      'after': first['next_cursor']})).json()
    assert second['next_cursor'] is None
    assert second['items'][0]['event_id'] != first['items'][0]['event_id']
    detail = await client.get(f"/api/im/runs/details/events/{first['items'][0]['event_id']}", headers=headers)
    assert detail.json()['item']['payload']['arguments']['large'] == 'x' * 10000
    scope = (await container.room_events.list_events('room'))
    assert len(scope) == 4
    assert not any(e['name'].startswith(('llm.', 'tool.')) for e in scope)
    assert all(e['execution_id'] in ids for e in scope)
    assert (await client.get('/api/im/runs/missing/events?view=summary', headers=headers)).status_code == 404
    # The old full-history contract still accepts a legacy DM conversation ID.
    container.store.insert_one('events', {'run_id': 'legacy-conversation', 'event_id': 'legacy',
                                          'name': 'agent.think', 'payload': {'think': 'old'}})
    legacy = await client.get('/api/im/runs/legacy-conversation/events', headers=headers)
    assert legacy.json()['items'][0]['payload']['think'] == 'old'


@pytest.mark.asyncio
async def test_summary_publish_failure_is_explicit_and_resume_transforms_live(backend, monkeypatch):
    container, _, _ = backend
    events, rt = container.bridge.events, container.bridge.runtime
    await container.bridge.runs.states.create({'run_id': 'replay', 'scope_id': 'scope', 'status': 'running'})
    await events.publish('replay', 'llm.started', {})
    stream = events.stream('replay', summary=True)
    assert decode(await anext(stream))['name'] == 'stream.reset'
    assert decode(await anext(stream))['name'] == 'llm.started'
    ready = await anext(stream)
    cursor = next(line[4:] for line in ready.splitlines() if line.startswith('id: '))
    await stream.aclose()
    original_append = rt.append
    attempts = 0
    async def fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError('lost delivery')
    monkeypatch.setattr(rt, 'append', fail)
    with pytest.raises(ConnectionError):
        await events.publish('replay', 'agent.think', {'think': 'failed event'})
    assert attempts == 3
    monkeypatch.setattr(rt, 'append', original_append)
    missed = await events.publish('replay', 'agent.think', {'think': 'must not appear in summary'})
    stream = events.stream('replay', last_id=cursor, summary=True)
    assert decode(await anext(stream))['name'] == 'stream.ready'
    item = decode(await asyncio.wait_for(anext(stream), 3))
    assert item['event_id'] == missed['event_id']
    assert 'payload' not in item
    await events.publish('replay', 'llm.completed', {'content': 'full answer'})
    assert 'payload' not in decode(await asyncio.wait_for(anext(stream), 3))
    await stream.aclose()


@pytest.mark.asyncio
async def test_regeneration_separates_attempts_and_cleanup_removes_all(backend, monkeypatch):
    container, client, headers = backend
    original = container.bridge.agents.build_run_agent
    def build(agent_id):
        agent = original(agent_id)
        async def execute(prompt):
            from domain.run_context import current_run_id
            await container.bridge.events.publish(current_run_id.get(), 'agent.think', {'agent_id': agent_id, 'think': 'reason'})
            agent.states.update(final='answer', is_finished=True)
        agent.start_with_history = execute
        return agent
    monkeypatch.setattr(container.bridge.agents, 'build_run_agent', build)
    cid = (await post(client, '/agents/default_executor/conversations', headers, {}))['conversation_id']
    mid = (await post(client, f'/conversations/{cid}/messages', headers, {'content_parts': [{'type': 'text', 'text': 'hello'}]}))['message_id']
    first = (await post(client, f'/conversations/{cid}/reply', headers, {'message_id': mid}))['run_id']
    await wait_finished(container.bridge.runtime, 'dm_reply', first)
    result = await container.im.regenerate_conversation_reply(conversation_id=cid, message_id=mid)
    second = result['reply']['run_id']
    assert first != second and first != cid and first != mid
    await wait_finished(container.bridge.runtime, 'dm_reply', second)
    # Simulate an old completion arriving after regeneration.
    with pytest.raises(ExecutionRevoked):
        await container.bridge.runs.states.update(first, {'status': 'failed'})
    assert container.store.find_one('im_messages', {'message_id': mid})['status'] == 'finished'
    assert (await container.bridge.events.list_events(first))[0]['execution_id'] != (await container.bridge.events.list_events(second))[0]['execution_id']
    deleted = await client.delete(f'/api/im/conversations/{cid}', headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()['item']['stats']['runs'] == 2
    for rid in (first, second):
        assert container.store.find_one('runs', {'run_id': rid}) is None
        assert (await container.bridge.events.list_events(rid)) == []
        assert await container.bridge.runtime.get_state('dm_reply', rid) is None
        assert await container.bridge.runtime.redis.get(container.bridge.runtime.cache.keys(rid)[0]) is None


@pytest.mark.asyncio
async def test_plan_steps_persist_before_events_and_share_execution_links(backend):
    from application.services.runs import ObservablePlanOrchestrator, RecordingEventBus
    from domain.state import Plan, PlanStep
    from domain.run_context import current_run_id
    container, _, _ = backend
    streams, states = container.bridge.events, container.bridge.runs.states
    await states.create({'run_id': 'steps', 'scope_id': 'room', 'status': 'running'})
    agent = SimpleNamespace(id='executor', name='Executor', states={})
    async def execute(prompt):
        record = container.store.find_one('runs', {'run_id': 'steps'})
        assert any(s['status'] == 'in_progress' for s in record['plan']['steps'])
        await streams.publish('steps', 'tool.called', {'agent_id': 'executor', 'arguments': {}})
        agent.states.update(is_finished=True, final='done')
    agent.start = execute
    plan = Plan(steps=[PlanStep(step_id=str(i), title=f'Step {i}', executor_id='executor') for i in range(2)])
    orchestrator = ObservablePlanOrchestrator(
        planner=SimpleNamespace(id='planner'), executors={'executor': agent},
        step_context_engine=SimpleNamespace(build=lambda state: 'prompt'),
        event_bus=RecordingEventBus('steps', streams), run_id='steps', streams=streams, states=states)
    await orchestrator._dispatch({'event_dispatch': 'plan.generated', 'playload': {'plan': plan}})
    token = current_run_id.set('steps')
    try:
        await asyncio.gather(*(orchestrator._run_plan_step(step, plan) for step in plan.steps))
    finally:
        current_run_id.reset(token)
    calls = (await streams.list_events('steps'))
    assert len(calls) == 2
    assert len({e['execution_id'] for e in calls}) == 2
    business = (await container.room_events.list_events('room'))
    for call in calls:
        linked = [e for e in business if e['execution_id'] == call['execution_id']]
        assert {e['name'] for e in linked} == {
            'plan.step.started', 'agent.execution.started', 'agent.execution.finished', 'plan.step.observed'}
    record = container.store.find_one('runs', {'run_id': 'steps'})
    assert all(step['status'] == 'done' for step in record['plan']['steps'])


@pytest.mark.asyncio
async def test_im_sse_auth_user_identity_and_cursor_precedence(backend, monkeypatch):
    container, client, headers = backend
    cid = (await post(client, '/agents/default_executor/conversations', headers, {}))['conversation_id']
    room = (await post(client, '/rooms', headers, {'member_agent_ids': ['default_executor']}))['room_id']
    await container.bridge.runs.states.create({'run_id': 'sse-auth', 'scope_id': room, 'status': 'pending'})
    calls = []
    async def finite(scope, last_id=None, **kwargs):
        calls.append((scope, last_id, kwargs))
        yield 'event: stream.ready\ndata: {"name":"stream.ready"}\n\n'
    monkeypatch.setattr(container.room_events, 'stream', finite)
    monkeypatch.setattr(container.bridge.events, 'stream', finite)
    me = (await client.get('/api/im/auth/me', headers=headers)).json()['item']
    for path in (f'/api/im/conversations/{cid}/events', f'/api/im/rooms/{room}/events',
                 '/api/im/runs/sse-auth/events/stream'):
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers=headers, params={'last_id': 'query/1-0', 'user_id': 'forged'})
        assert response.status_code == 200
        assert calls[-1][1] == 'query/1-0'
        assert calls[-1][2]['user_id'] == me['user_id']
        response = await client.get(path, headers={**headers, 'Last-Event-ID': 'header/2-0'},
                                    params={'last_id': 'query/1-0'})
        assert response.status_code == 200
        assert calls[-1][1] == 'header/2-0'
        cookie = 'im_sse_session=' + headers['Authorization'].split(' ', 1)[1]
        response = await client.get(path, headers={'Cookie': cookie}, params={'last_id': 'cookie/3-0'})
        assert response.status_code == 200
        assert calls[-1][1] == 'cookie/3-0'
        assert calls[-1][2]['user_id'] == me['user_id']
        assert (await client.get(path, headers={'Cookie': cookie, 'Authorization': 'Bearer invalid'})).status_code == 401


@pytest.mark.asyncio
async def test_room_tasks_deduplicate_runs_and_load_unique_runs_concurrently():
    messages = [
        {"message_id": "agent-a", "sender_type": "agent", "run_id": "a", "created_at": 3},
        {"message_id": "user-a", "sender_type": "user", "run_id": "a", "created_at": 1},
        {"message_id": "user-b", "sender_type": "user", "run_id": "b", "created_at": 2},
        {"message_id": "agent-b", "sender_type": "agent", "run_id": "b", "created_at": 4},
    ]

    class Runs:
        def __init__(self):
            self.calls = []
            self.inflight = 0
            self.max_inflight = 0

        async def get_run(self, run_id):
            self.calls.append(run_id)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.01)
            self.inflight -= 1
            return {"run_id": run_id, "status": "finished", "prompt": f"prompt-{run_id}"}

    runs = Runs()
    service = GroupRunService(
        store=None,
        bridge=SimpleNamespace(runs=runs),
        events=None,
        rooms=SimpleNamespace(ensure_group_room=lambda _: {"type": "group"}),
        messages=SimpleNamespace(
            list_messages=lambda *_args, **_kwargs: messages,
            message_text=lambda message: message["message_id"],
        ),
        agents=None,
        favorites=None,
        default_workdir=".",
    )

    tasks = await service.list_room_tasks("room", conversation_id="conversation")

    assert set(runs.calls) == {"a", "b"}
    assert runs.max_inflight == 2
    assert {task["run_id"] for task in tasks} == {"a", "b"}
    assert {task["message_id"] for task in tasks} == {"user-a", "user-b"}


@pytest.mark.asyncio
async def test_terminal_run_state_does_not_read_redis_control():
    class Cache:
        async def get(self, _run_id, _loader):
            return {"run_id": "done", "status": "finished"}

    class Runtime:
        cache = Cache()

        async def get_state(self, *_args):
            raise AssertionError("terminal runs must not query live Redis control")

    result = await RunStateService(SimpleNamespace(), Runtime()).get("done")
    assert result == {"run_id": "done", "status": "finished", "cancel_requested": False}
