// Gateway status is separate from agent/tool business state. Only scope-safe
// notifications enter this view; model fragments remain in execution traces.
export function selectGatewayCalls(events, { conversationId, runIds = [], runStatuses = {} } = {}) {
  if (!conversationId) return []
  const ids = new Set(runIds)
  const latest = new Map()
  const terminals = new Map()
  const ordered = events.filter(event => event.category === 'scope' && (
    event.conversation_id === conversationId || (!event.conversation_id && ids.has(event.run_id))
  )).slice().sort((a, b) => (a.created_at || 0) - (b.created_at || 0)
    || String(a.event_id).localeCompare(String(b.event_id)))
  for (const event of ordered) {
    const p = event.payload || {}
    const run = event.run_id || p.run_id
    if (!run) continue
    let terminal = null
    if (['workflow.finished', 'agent.reply.finished'].includes(event.name)) terminal = 'finished'
    if (event.name === 'run.cancelled' || (event.name === 'workflow.failed' && p.cancelled)) terminal = 'cancelled'
    else if (event.name === 'workflow.failed') terminal = 'failed'
    if (event.name === 'task.updated' && ['finished', 'failed', 'cancelled'].includes(p.status)) terminal = p.status
    if (terminal) terminals.set(run, terminal)
    if (event.name !== 'llm.call.updated' || !p.call_id || !p.agent_id) continue
    const key = JSON.stringify([run, event.execution_id || '', p.agent_id])
    const old = latest.get(key)
    if (old) {
      if (old.call_id === p.call_id) {
        if (old.terminal || (p.status_sequence || 0) <= (old.status_sequence || 0)) continue
      } else if ((p.call_started_at || 0) < (old.call_started_at || 0)
        || ((p.call_started_at || 0) === (old.call_started_at || 0) && event.created_at < old.created_at)) continue
    }
    latest.set(key, { ...p, key, run_id: run, execution_id: event.execution_id, created_at: event.created_at })
  }
  return [...latest.values()].filter(call => {
    if (!['waiting', 'retrying', 'failed'].includes(call.status)) return false
    const terminal = terminals.get(call.run_id) || runStatuses[call.run_id]
    if (['finished', 'cancelled'].includes(terminal)) return false
    return terminal !== 'failed' || call.status === 'failed'
  })
}

export function gatewayCallLabel(call) {
  return { waiting: '等待输出', retrying: '重试中', failed: '调用失败' }[call?.status] || ''
}
