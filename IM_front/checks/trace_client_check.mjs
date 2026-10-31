import test from 'node:test'
import assert from 'node:assert/strict'
import { TraceClient } from '../src/utils/traceClient.js'

class Stream {
  handlers = new Map()
  closed = false
  addEventListener(name, handler) { this.handlers.set(name, handler) }
  close() { this.closed = true }
  emit(name, event) { this.handlers.get(name)?.({ data: JSON.stringify(event) }) }
}
const event = { event_id: 'scope-1', category: 'scope', scope_id: 'scope', run_id: 'run', execution_id: 'exec-1' }
function fixture(overrides = {}) {
  const calls = [], streams = []
  const client = new TraceClient({
    api: {
      runSummaries: async (run, params) => { calls.push(['summary', run, params]); return { items: [], next_cursor: null } },
      runEvent: async (run, id) => { calls.push(['body', run, id]); return { item: { payload: { content: 'body' } } } },
      ...overrides,
    },
    eventNames: ['tool.called', 'llm.delta', 'llm.call.updated'],
    openStream: () => { const stream = new Stream(); streams.push(stream); return stream },
  })
  return { client, calls, streams }
}

test('collapsed makes no requests; names and bodies are loaded separately', async () => {
  const { client, calls, streams } = fixture()
  assert.equal(calls.length, 0)
  await client.expand(event)
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'summary')
  const item = { version: 2, category: 'run', run_id: 'run', execution_id: 'exec-1', event_id: 'tool', name: 'tool.called' }
  streams[0].emit('tool.called', item)
  streams[0].emit('tool.called', item)
  streams[0].emit('tool.called', { ...item, event_id: 'other', execution_id: 'exec-2' })
  streams[0].emit('llm.delta', { ...item, event_id: 'delta', name: 'llm.delta' })
  streams[0].emit('llm.call.updated', { ...item, event_id: 'gateway-status', name: 'llm.call.updated' })
  assert.deepEqual(client.state(event).items, [item])
  await client.detail(item)
  await client.detail(item)
  assert.equal(calls.filter(call => call[0] === 'body').length, 1)
  client.close()
})

test('shared run subscriptions close only after last collapse', async () => {
  const { client, streams } = fixture()
  const other = { ...event, event_id: 'scope-2' }
  await client.expand(event)
  await client.expand(other)
  assert.equal(streams.length, 1)
  client.collapse(event)
  assert.equal(streams[0].closed, false)
  client.collapse(other)
  assert.equal(streams[0].closed, true)
  await client.expand(event)
  assert.equal(streams.length, 2)
  client.close()
})

test('scope changes discard late pages and late body responses', async () => {
  let finishPage, finishBody
  const { client, streams } = fixture({
    runSummaries: () => new Promise(resolve => { finishPage = resolve }),
    runEvent: () => new Promise(resolve => { finishBody = resolve }),
  })
  const loading = client.expand(event)
  const detail = client.detail({ ...event, category: 'run' })
  client.close()
  finishPage({ items: [{ event_id: 'late' }], next_cursor: null })
  finishBody({ item: { payload: 'late' } })
  await Promise.all([loading, detail])
  assert.equal(client.runs.size, 0)
  assert.equal(client.details.size, 0)
  assert.equal(streams[0].closed, true)
})

test('failed body is retryable and pagination requests the next cursor', async () => {
  let attempt = 0
  const { client, calls } = fixture({
    runEvent: async () => { if (++attempt === 1) throw Error('offline'); return { item: { payload: 'ok' } } },
    runSummaries: async (_, params) => {
      calls.push(params.after)
      return { items: [{ event_id: params.after ? 'two' : 'one', execution_id: 'exec-1' }], next_cursor: params.after ? null : 'one' }
    },
  })
  await client.expand(event)
  await client.more(event)
  assert.deepEqual(calls, [undefined, 'one'])
  const item = { ...event, category: 'run' }
  await client.detail(item)
  assert.equal(client.body(item).error, true)
  await client.detail(item)
  assert.equal(client.body(item).item.payload, 'ok')
  client.close()
})
