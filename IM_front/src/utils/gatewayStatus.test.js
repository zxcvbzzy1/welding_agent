import test from 'node:test'
import assert from 'node:assert/strict'
import { selectGatewayCalls, gatewayCallLabel } from './gatewayStatus.js'
import { compactLlmEvents, sseEventNames } from './runtimeEvents.js'

const scope = { conversationId: 'chat', runIds: ['run'] }
function update(sequence, status, extra = {}) {
  return { event_id: `s${sequence}`, version: 2, category: 'scope', trace: false,
    conversation_id: 'chat', run_id: 'run', execution_id: 'execution', created_at: sequence,
    name: 'llm.call.updated', payload: { call_id: 'call', call_started_at: 1, agent_id: 'agent',
      status_sequence: sequence, status, terminal: status === 'failed', ...extra } }
}
const end = (name, time = 100, payload = {}) => ({ event_id: name, category: 'scope',
  conversation_id: 'chat', run_id: 'run', created_at: time, name, payload })

test('only three gateway labels; body, complete and cancel clear waiting', () => {
  assert.deepEqual(['waiting', 'retrying', 'failed'].map(status => gatewayCallLabel({ status })),
    ['等待输出', '重试中', '调用失败'])
  assert.ok(sseEventNames.includes('llm.call.updated'))
  assert.equal(selectGatewayCalls([update(1, 'waiting')], scope)[0].status, 'waiting')
  assert.equal(selectGatewayCalls([update(1, 'waiting'), update(2, 'retrying')], scope)[0].status, 'retrying')
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), update(2, null, { has_output: true })], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), update(2, null, { terminal: true })], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), update(2, null, { terminal: true, cancelled: true })], scope), [])
})

test('replay, duplicates and late updates cannot roll terminal states back', () => {
  const failure = update(4, 'failed')
  const late = { ...update(2, 'waiting'), created_at: 5 }
  assert.equal(selectGatewayCalls([failure, update(1, 'waiting'), late, failure], scope)[0].status, 'failed')
  assert.deepEqual(selectGatewayCalls([failure, update(5, 'waiting')], scope).map(c => c.status), ['failed'])
  assert.deepEqual(selectGatewayCalls([update(4, null, { terminal: true }), late], scope), [])
})

test('new calls replace old calls; late failure cannot overwrite a newer call', () => {
  const newer = update(2, 'waiting', { call_id: 'new', call_started_at: 2, status_sequence: 1 })
  const oldFailure = { ...update(3, 'failed'), created_at: 9 }
  const state = selectGatewayCalls([update(1, 'waiting'), newer, oldFailure], scope)
  assert.equal(state.length, 1)
  assert.equal(state[0].call_id, 'new')
  assert.equal(state[0].status, 'waiting')
})

test('parent, child, execution, conversation and run states stay isolated', () => {
  const child = update(2, 'retrying', { call_id: 'child-call', agent_id: 'child', parent_agent_id: 'agent', temporary: true })
  const otherExecution = { ...update(3, 'waiting'), execution_id: 'other-execution' }
  const otherChat = { ...update(4, 'failed'), conversation_id: 'other' }
  const runTrace = { ...update(5, 'failed'), category: 'run' }
  const otherRun = { ...update(6, 'waiting'), run_id: 'other-run' }
  const result = selectGatewayCalls([update(1, 'waiting'), child, otherExecution, otherChat, runTrace, otherRun], scope)
  assert.equal(result.length, 4)
  assert.equal(result.find(c => c.agent_id === 'child').status, 'retrying')
  assert.deepEqual(selectGatewayCalls([otherChat, runTrace], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting')], { conversationId: '' }), [])
})

test('run terminal events and restored message status take precedence', () => {
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), end('workflow.finished')], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), end('run.cancelled')], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting'), end('workflow.failed')], scope), [])
  assert.equal(selectGatewayCalls([update(1, 'failed'), end('workflow.failed')], scope)[0].status, 'failed')
  assert.deepEqual(selectGatewayCalls([update(1, 'failed'), end('workflow.finished')], scope), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'waiting')], { ...scope, runStatuses: { run: 'failed' } }), [])
  assert.deepEqual(selectGatewayCalls([update(1, 'failed')], { ...scope, runStatuses: { run: 'cancelled' } }), [])
})

test('streaming buffers use call identity and retain legacy compatibility', () => {
  const delta = (call, text, id) => ({ event_id: id, name: 'llm.delta', run_id: 'run', created_at: 1,
    payload: { agent_id: 'agent', call_id: call, call_role: 'executor_think', delta: text, sequence: 1 } })
  const result = compactLlmEvents([delta('one', 'first', 'd1'), delta('two', 'second', 'd2')])
  assert.equal(result.length, 2)
  assert.deepEqual(result.map(e => e.payload.content).sort(), ['first', 'second'])
  const completed = compactLlmEvents([delta('one', 'first', 'd1'), { event_id: 'complete', name: 'llm.completed',
    run_id: 'run', payload: { call_id: 'one', agent_id: 'agent', call_role: 'executor_think' } }])
  assert.equal(completed.length, 1)
  assert.equal(completed[0].name, 'llm.completed')
  const retried = compactLlmEvents([
    { ...delta('retry', 'body after retry', 'd3'), payload: { ...delta('retry', '', 'd3').payload, delta: 'body after retry', attempt: 4 } },
    { event_id: 'retry-complete', name: 'llm.completed', run_id: 'run', payload: {
      call_id: 'retry', attempt: 4, agent_id: 'agent', call_role: 'executor_think',
    } },
  ])
  assert.equal(retried.length, 1)
  assert.equal(retried[0].name, 'llm.completed')
})
