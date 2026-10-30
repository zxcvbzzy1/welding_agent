// Merge independently timestamped input and compaction snapshots per agent.
export function mergeContextUsage(items, event) {
  const payload = event.payload || {}
  if (!payload.agent_id) return items
  const map = new Map(items.map(item => [item.agent_id, item]))
  const old = map.get(payload.agent_id) || { agent_id: payload.agent_id }
  const at = value => value?.updated_at || value?.created_at || 0
  if (event.name === 'context.usage.updated' && at(payload) >= at(old)) {
    map.set(payload.agent_id, { ...payload, compaction: old.compaction })
  } else if (event.name.startsWith('context.compaction.') && at(payload) >= at(old.compaction)) {
    map.set(payload.agent_id, { ...old, agent_name: old.agent_name || payload.agent_name, compaction: payload })
  }
  return [...map.values()]
}

export function preferredContextAgent(items, current = '') {
  if (items.some(item => item.agent_id === current)) return current
  return (items.find(item => item.agent_type === 'planner') || items[0])?.agent_id || ''
}

export const contextModuleNames = {
  system_prompt: '系统指令', available_tools: '工具定义', pinned_context: '固定上下文',
  long_term_memory: '长期记忆', skill: '技能', history: '对话历史', user_prompt: '当前任务',
  tool_output: '工具反馈', tool_feedback: '工具反馈', plan_observations: '计划观察',
  user: '当前任务', task: '当前状态', executors: '执行者状态',
  executor_status: '执行者状态', available_executors: '执行者定义', state: '当前状态',
  error: '错误反馈', run_workspace: '工作目录', message_format: '消息格式开销', other_input: '其他输入',
}
