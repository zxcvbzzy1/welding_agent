"""Redis integration tests use only a unique prefix; never FLUSHDB/FLUSHALL.

REDIS_URL may point at a shared development server. LLM/tool execution is stubbed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

from im_backend.infra.agent_flow_bridge.pathing import ensure_agent_flow_path
ensure_agent_flow_path()
from infra.runtime import RedisRuntime
from application.services.events import EventStreamService
from application.events.human_confirmation import HumanConfirmationService
from im_backend.application.services.platform.events import RoomEventStreamService


class Archive:
    def __init__(self):
        self.rows = {}

    def insert_one(self, collection, event):
        self.rows.setdefault(collection, []).append(dict(event))
        return event

    def upsert_events(self, collection, events):
        for event in events:
            if not self.find_one(collection, {'event_id': event['event_id']}):
                self.insert_one(collection, event)
        return {e['event_id'] for e in events}

    def find_one(self, collection, query):
        return next(iter(self.find_many(collection, query)), None)

    def find_many(self, collection, query=None, **kwargs):
        return [dict(e) for e in self.rows.get(collection, [])
                if all(e.get(k) == v for k, v in (query or {}).items())]


@pytest_asyncio.fixture
async def runtime():
    rt = RedisRuntime(prefix="agenthub-test:" + uuid.uuid4().hex)
    await rt.start()
    try:
        yield rt
    finally:
        # Stop tasks before removing only keys owned by this test.
        await rt.close()
        keys = [key async for key in rt.redis.scan_iter(match=rt.prefix + ':*')]
        if keys:
            await rt.redis.delete(*keys)
        await rt.redis.aclose()


def decode(raw):
    return json.loads(next(line[6:] for line in raw.splitlines() if line.startswith('data: ')))


def cursor(raw):
    return next(line[4:] for line in raw.splitlines() if line.startswith('id: '))


@pytest.mark.asyncio
async def test_idle_stream_heartbeats_then_delivers_new_event(runtime):
    stream = runtime.stream('scope', 'idle-room', lambda: [])
    try:
        assert decode(await anext(stream))['name'] == 'stream.reset'
        assert decode(await anext(stream))['name'] == 'stream.ready'
        # Exercise a real 15-second blocking read. An immediate event would hide
        # a client socket timeout shorter than the server's blocking interval.
        heartbeat = await asyncio.wait_for(anext(stream), timeout=20)
        assert heartbeat == ': heartbeat\n\n'
        event = dict(event_id='after-idle', name='llm.delta', payload={'delta': 'hello'})
        await runtime.append('scope', 'idle-room', event)
        received = await asyncio.wait_for(anext(stream), timeout=5)
        assert decode(received) == event
        assert cursor(received)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_history_handoff_resume_and_separate_channels(runtime):
    archive = Archive()
    events = EventStreamService(archive, runtime)
    im = RoomEventStreamService(archive, runtime)
    archive.insert_one('runs', {'run_id': 'run', 'scope_id': 'room'})
    # Old events stay archived, but are not part of the v2 scope UI.
    archive.insert_one('im_events', dict(event_id='legacy', scope_id='room', name='tool.called',
                                       payload={'arguments': 'heavy'}, created_at=1))
    first = await events.publish('run', 'workflow.started', {'run_id': 'run'})
    await events.no_store_publish('run', 'llm.delta', {'delta': 'one'})
    await events.publish('run', 'tool.called', {'arguments': 'heavy'})
    stream = im.stream('room')
    received = []
    while True:
        raw = await anext(stream)
        if decode(raw)['name'] == 'stream.ready':
            checkpoint = cursor(raw)
            break
        if decode(raw)['name'] != 'stream.reset':
            received.append(decode(raw))
    assert [e['name'] for e in received] == ['workflow.started']
    assert received[0]['event_id'] == first['event_id']
    await stream.aclose()
    await events.publish('run', 'workflow.finished', {'final': 'onetwo'})
    resumed = runtime.stream('scope', 'room', lambda: [], last_id=checkpoint)
    try:
        assert decode(await anext(resumed))['name'] == 'stream.ready'
        assert decode(await anext(resumed))['name'] == 'workflow.finished'
    finally:
        await resumed.aclose()
    assert [e['name'] for e in (await events.list_events('run'))] == ['tool.called']
    assert await runtime.redis.xlen(runtime.event_key('scope', 'other-room')) == 0


@pytest.mark.asyncio
async def test_expired_cursor_resets_to_archive(runtime):
    events = EventStreamService(Archive(), runtime)
    await events.publish('run', 'llm.started', {})
    rt_stream = events.stream('run', last_id='1-0')
    try:
        assert decode(await anext(rt_stream))['name'] == 'stream.reset'
        assert decode(await anext(rt_stream))['name'] == 'llm.started'
        assert decode(await anext(rt_stream))['name'] == 'stream.ready'
    finally:
        await rt_stream.aclose()


@pytest.mark.asyncio
async def test_redis_state_claim_cancel_and_worker_start(runtime):
    peer = RedisRuntime(url=runtime.url, prefix=runtime.prefix)
    await peer.start()
    try:
        assert await runtime.claim('dm_reply', 'message', {'run_id': 'message'})
        assert not await peer.claim('dm_reply', 'message', {'run_id': 'message'})
        assert (await peer.get_state('dm_reply', 'message'))['status'] == 'running'
        stopped = asyncio.Event()

        async def run():
            try:
                await asyncio.Event().wait()
            finally:
                await runtime.put_state('dm_reply', 'message', {'status': 'cancelled'})
                stopped.set()
        task = asyncio.create_task(run())
        runtime.track('dm_reply', 'message', task)
        await peer.request_cancel('dm_reply', 'message')
        await asyncio.wait_for(stopped.wait(), 3)
        assert (await peer.get_state('dm_reply', 'message'))['status'] == 'cancelled'
        assert await peer.active_states() == []
        assert await peer.redis.ttl(peer.key('control:v2:dm_reply:message')) > 0
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_approval_shared_idempotent_and_cancelled(runtime):
    requester = HumanConfirmationService(EventStreamService(Archive(), runtime))
    peer = RedisRuntime(url=runtime.url, prefix=runtime.prefix)
    resolver = HumanConfirmationService(EventStreamService(Archive(), peer))
    try:
        task = asyncio.create_task(requester.request_approval(
            run_id='run', agent_id='agent', tool_name='bash', called_event_name='bash.called', arguments={}))
        for _ in range(30):
            pending = await resolver.list_pending('run')
            if pending:
                break
            await asyncio.sleep(.02)
        item = pending[0]
        first = await resolver.resolve(run_id='run', confirmation_id=item['confirmation_id'], approved=True)
        duplicate = await resolver.resolve(run_id='run', confirmation_id=item['confirmation_id'], approved=False)
        assert duplicate == first
        assert (await asyncio.wait_for(task, 2))['approved'] is True
        assert await resolver.list_pending('run') == []
        task = asyncio.create_task(requester.request_approval(
            run_id='cancel', agent_id='agent', tool_name='bash', called_event_name='bash.called', arguments={}))
        while not await resolver.list_pending('cancel'):
            await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await resolver.list_pending('cancel') == []
    finally:
        await peer.redis.aclose()


@pytest.mark.asyncio
async def test_orphans_do_not_cancel_other_workers_or_unstarted_runs(runtime, monkeypatch):
    lost = RedisRuntime(url=runtime.url, prefix=runtime.prefix)
    await lost.claim('orchestration', 'lost', {'run_id': 'lost'})
    await runtime.claim('orchestration', 'alive', {'run_id': 'alive'})
    await runtime.put_state('orchestration', 'pending', {'run_id': 'pending', 'status': 'pending', 'owner_worker': ''})
    await lost.redis.delete(lost.key('worker:' + lost.worker_id))
    clock = [1000]
    async def now():
        return clock[0], 0
    monkeypatch.setattr(runtime.redis, 'time', now)
    async def handle(state):
        await runtime.put_state(state['kind'], state['target_id'], {'status': 'failed'})
    runtime.on_orphan = handle
    await runtime.reap_orphans()
    assert (await runtime.get_state('orchestration', 'lost'))['status'] == 'running'
    for delay in (5, 10, 20, 40, 80):
        clock[0] += delay
        await runtime.reap_orphans()
    assert (await runtime.get_state('orchestration', 'lost'))['status'] == 'failed'
    assert (await runtime.get_state('orchestration', 'alive'))['status'] == 'running'
    assert (await runtime.get_state('orchestration', 'pending'))['status'] == 'pending'
    await lost.redis.aclose()


async def child(runtime, program):
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'REDIS_URL': runtime.url, 'REDIS_KEY_PREFIX': runtime.prefix,
           'PYTHONPATH': os.pathsep.join((str(repo_root), str(repo_root / 'agent_flow')))}
    return await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', program,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


@pytest.mark.asyncio
async def test_two_process_sse_broadcast_and_remote_controls(runtime):
    program = '''
import asyncio, json
from infra.runtime import RedisRuntime
async def main():
    r = RedisRuntime()
    s = r.stream('scope', 'room', lambda: [])
    try:
        await anext(s)  # initial reset
        print(await anext(s), flush=True)
        print('READY', flush=True)
        print(json.dumps(await anext(s)), flush=True)
    finally:
        await s.aclose()
        await r.redis.aclose()
asyncio.run(main())
'''
    readers = [await child(runtime, program) for _ in range(2)]
    try:
        for proc in readers:
            while (await asyncio.wait_for(proc.stdout.readline(), 5)).strip() != b'READY':
                if proc.returncode is not None:
                    pytest.fail((await proc.stderr.read()).decode())
        await runtime.append('scope', 'room', dict(event_id='broadcast', name='llm.delta', payload={'delta':'hello'}, created_at=1))
        for proc in readers:
            line = await asyncio.wait_for(proc.stdout.readline(), 5)
            assert decode(json.loads(line))['event_id'] == 'broadcast'
            assert await asyncio.wait_for(proc.wait(), 5) == 0
    finally:
        for proc in readers:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    program = '''
import asyncio
from infra.runtime import RedisRuntime
from application.services.events import EventStreamService
from application.events.human_confirmation import HumanConfirmationService
class Store:
    def insert_one(self, *args): pass
    def find_one(self, *args): return None
async def main():
    r = RedisRuntime()
    await r.start()
    await r.claim('dm_reply', 'remote', {'run_id':'remote'})
    async def execute():
        try:
            h = HumanConfirmationService(EventStreamService(Store(), r))
            result = await h.request_approval(run_id='scope', agent_id='a', tool_name='bash', called_event_name='bash.called', arguments={})
            print('APPROVED' if result['approved'] else 'REJECTED', flush=True)
            await asyncio.Event().wait()
        finally:
            await r.put_state('dm_reply', 'remote', {'status':'cancelled'})
    task = asyncio.create_task(execute())
    r.track('dm_reply', 'remote', task)
    await asyncio.gather(task, return_exceptions=True)
    await r.close()
asyncio.run(main())
'''
    proc = await child(runtime, program)
    try:
        for _ in range(100):
            pending = await runtime.confirmations('scope')
            if pending:
                break
            await asyncio.sleep(.05)
        assert pending
        assert (await runtime.get_state('dm_reply', 'remote'))['status'] == 'running'
        await runtime.resolve_confirmation('scope', pending[0]['confirmation_id'], True, 'approved from peer')
        assert (await asyncio.wait_for(proc.stdout.readline(), 5)).strip() == b'APPROVED'
        await runtime.request_cancel('dm_reply', 'remote')
        assert await asyncio.wait_for(proc.wait(), 5) == 0
        assert (await runtime.get_state('dm_reply', 'remote'))['status'] == 'cancelled'
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_delete_runtime_does_not_touch_unrelated_scope_events(runtime):
    await runtime.append('run', 'run', dict(event_id='a', run_id='run', name='workflow.started', payload={}, created_at=1))
    await runtime.append('scope', 'room', dict(event_id='b', name='message.created', payload={}, created_at=2))
    await runtime.claim('orchestration', 'run', {'run_id':'run'})
    await runtime.delete_runtime('run')
    assert await runtime.get_state('orchestration','run') is None
    remaining = await runtime.redis.xrange(runtime.event_key('scope', 'room'))
    assert len(remaining) == 1
    assert json.loads(remaining[0][1]['event'])['event_id'] == 'b'


@pytest.mark.asyncio
async def test_native_bootstrap_resets_id_and_filtered_events_dispatch_checkpoints(runtime):
    calls = 0
    def history():
        nonlocal calls
        calls += 1
        return []
    stream = runtime.stream('scope', 'native', history, transform=lambda event: None, user_id='native-user')
    try:
        reset = await anext(stream)
        assert reset.startswith('id: \nevent: stream.reset\n')
        assert decode(reset)['payload']['reason'] == 'initial'
        ready = await anext(stream)
        assert decode(ready)['name'] == 'stream.ready'
        await runtime.append('scope', 'native', dict(event_id='filtered', name='hidden', payload={}, created_at=1))
        checkpoint = await asyncio.wait_for(anext(stream), 5)
        assert decode(checkpoint) == {'name': 'stream.checkpoint', 'payload': {}}
        assert cursor(checkpoint) != cursor(ready)
    finally:
        await stream.aclose()
    resumed = runtime.stream('scope', 'native', history, last_id=cursor(checkpoint), user_id='native-user')
    try:
        assert decode(await anext(resumed))['name'] == 'stream.ready'
        assert calls == 1
    finally:
        await resumed.aclose()
