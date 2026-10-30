import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mergeContextUsage, preferredContextAgent } from './contextUsage.js'

test('late usage cannot overwrite a newer snapshot or another agent', () => {
  const items = [{ agent_id: 'p', agent_type: 'planner', input_tokens: 200, updated_at: 3 },
    { agent_id: 'e', input_tokens: 50, updated_at: 2 }]
  const merged = mergeContextUsage(items, { name: 'context.usage.updated', payload: {
    agent_id: 'p', input_tokens: 1, updated_at: 1,
  } })
  assert.equal(merged[0].input_tokens, 200)
  assert.equal(merged[1].input_tokens, 50)
})

test('compaction does not replace business input and survives snapshot updates', () => {
  let items = mergeContextUsage([{ agent_id: 'p', input_tokens: 100, updated_at: 1 }], {
    name: 'context.compaction.started', payload: { agent_id: 'p', status: 'started', updated_at: 2 },
  })
  items = mergeContextUsage(items, { name: 'context.usage.updated', payload: {
    agent_id: 'p', input_tokens: 20, updated_at: 3,
  } })
  assert.equal(items[0].input_tokens, 20)
  assert.equal(items[0].compaction.status, 'started')
  items = mergeContextUsage(items, { name: 'context.compaction.finished', payload: {
    agent_id: 'p', status: 'finished', updated_at: 4,
  } })
  items = mergeContextUsage(items, { name: 'context.compaction.started', payload: {
    agent_id: 'p', status: 'started', updated_at: 2,
  } })
  assert.equal(items[0].compaction.status, 'finished')
})

test('Planner is default; explicit executor selection stays selected', () => {
  const items = [{ agent_id: 'e', agent_type: 'executor' }, { agent_id: 'p', agent_type: 'planner' }]
  assert.equal(preferredContextAgent(items), 'p')
  assert.equal(preferredContextAgent(items, 'e'), 'e')
  assert.equal(preferredContextAgent([], 'e'), '')
})
