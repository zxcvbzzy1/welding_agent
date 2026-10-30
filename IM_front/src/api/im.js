import http from '@/api/http'

export const imApi = {
  contextUsage(runId) {
    return http.get(`/api/im/runs/${runId}/context-usage`)
  },
  agents() {
    return http.get('/api/im/agents')
  },
  contexts() {
    return http.get('/api/im/contexts')
  },
  createAgent(payload) {
    return http.post('/api/im/agents', payload)
  },
  updateAgent(agentId, payload) {
    return http.patch(`/api/im/agents/${agentId}`, payload)
  },
  builderChat(payload) {
    return http.post('/api/im/agents/builder/chat', payload)
  },
  deleteAgent(agentId) {
    return http.delete(`/api/im/agents/${agentId}`)
  },
  agentMessages(agentId) {
    return http.get(`/api/im/agents/${agentId}/messages`)
  },
  agentConversations(agentId) {
    return http.get(`/api/im/agents/${agentId}/conversations`)
  },
  createAgentConversation(agentId, payload) {
    return http.post(`/api/im/agents/${agentId}/conversations`, payload)
  },
  conversation(conversationId) {
    return http.get(`/api/im/conversations/${conversationId}`)
  },
  conversationMessages(conversationId, params = {}) {
    // params 支持懒加载：{ limit, before_id }；不传则返回全量（兼容旧行为）。
    return http.get(`/api/im/conversations/${conversationId}/messages`, { params })
  },
  addConversationMessage(conversationId, payload) {
    return http.post(`/api/im/conversations/${conversationId}/messages`, payload)
  },
  replyConversation(conversationId, payload) {
    return http.post(`/api/im/conversations/${conversationId}/reply`, payload)
  },
  cancelConversationMessage(conversationId, messageId) {
    return http.post(`/api/im/conversations/${conversationId}/messages/${messageId}/cancel`)
  },
  deleteConversation(conversationId) {
    return http.delete(`/api/im/conversations/${conversationId}`)
  },
  rooms() {
    return http.get('/api/im/rooms')
  },
  createRoom(payload) {
    return http.post('/api/im/rooms', payload)
  },
  updateRoom(roomId, payload) {
    return http.patch(`/api/im/rooms/${roomId}`, payload)
  },
  roomWorkspace(roomId) {
    return http.get(`/api/im/rooms/${roomId}/workspace`)
  },
  importWorkspace(roomId, form, config = {}) {
    return http.post(`/api/im/rooms/${roomId}/workspace/import`, form, config)
  },
  deleteRoom(roomId) {
    return http.delete(`/api/im/rooms/${roomId}`)
  },
  room(roomId) {
    return http.get(`/api/im/rooms/${roomId}`)
  },
  messages(roomId) {
    return http.get(`/api/im/rooms/${roomId}/messages`)
  },
  tasks(roomId) {
    return http.get(`/api/im/rooms/${roomId}/tasks`)
  },
  roomConversations(roomId) {
    return http.get('/api/im/rooms/' + roomId + '/conversations')
  },
  createRoomConversation(roomId, payload) {
    return http.post('/api/im/rooms/' + roomId + '/conversations', payload)
  },
  roomMessages(roomId, conversationId, params = {}) {
    // params 支持懒加载：{ limit, before_id }；conversationId 仍以独立参数传入。
    const query = { ...params }
    if (conversationId) query.conversation_id = conversationId
    return http.get('/api/im/rooms/' + roomId + '/messages', { params: query })
  },
  roomTasks(roomId, conversationId) {
    return http.get('/api/im/rooms/' + roomId + '/tasks', { params: conversationId ? { conversation_id: conversationId } : {} })
  },
  addMessage(roomId, payload) {
    return http.post(`/api/im/rooms/${roomId}/messages`, payload)
  },
  dispatch(roomId, payload) {
    return http.post(`/api/im/rooms/${roomId}/dispatch`, payload)
  },
  cancelRoomRun(roomId, runId) {
    return http.post(`/api/im/rooms/${roomId}/runs/${runId}/cancel`)
  },
  action(messageId, payload) {
    return http.post(`/api/im/messages/${messageId}/actions`, payload)
  },
  runEvents(runId) {
    return http.get(`/api/im/runs/${runId}/events`)
  },
  runSummaries(runId, params = {}) {
    return http.get(`/api/im/runs/${runId}/events`, { params: { view: 'summary', ...params } })
  },
  runEvent(runId, eventId) {
    return http.get(`/api/im/runs/${runId}/events/${eventId}`)
  },
  scopeEvent(scopeId, eventId) {
    return http.get(`/api/im/scopes/${scopeId}/events/${eventId}`)
  },
  artifacts() {
    return http.get('/api/im/artifacts')
  },
  activity() {
    return http.get('/api/im/activity')
  },
  toolsCatalog() {
    return http.get('/api/im/tools')
  },
  uploadArtifact(payload) {
    return http.post('/api/im/artifacts/upload', payload)
  },
  favorites(scopeType, scopeId) {
    return http.get('/api/im/favorites', { params: { scope_type: scopeType, scope_id: scopeId } })
  },
  createFavorite(payload) {
    return http.post('/api/im/favorites', payload)
  },
  favoriteMessage(messageId, payload) {
    return http.post('/api/im/messages/' + messageId + '/favorite', payload)
  },
  updateFavorite(favoriteId, payload) {
    return http.patch('/api/im/favorites/' + favoriteId, payload)
  },
  deleteFavorite(favoriteId) {
    return http.delete('/api/im/favorites/' + favoriteId)
  },
  bundleArtifacts(payload) {
    return http.post('/api/im/artifacts/bundle', payload, { responseType: 'blob' })
  },
  updateConversation(conversationId, payload) {
    return http.patch('/api/im/conversations/' + conversationId, payload)
  },
  regenerateConversationMessage(conversationId, messageId) {
    return http.post('/api/im/conversations/' + conversationId + '/messages/' + messageId + '/regenerate', {})
  },
  listDeployments() {
    return http.get('/api/im/deployments')
  },
  stopDeployment(deploymentId) {
    return http.delete(`/api/im/deployments/${deploymentId}`)
  },
  restartDeployment(deploymentId) {
    return http.post(`/api/im/deployments/${deploymentId}/restart`)
  },
  touchDeployment(deploymentId) {
    return http.post(`/api/im/deployments/${deploymentId}/touch`)
  },
  downloadDeployment(deploymentId, dir) {
    return http.get('/api/im/deployments/' + deploymentId + '/download', { params: { dir }, responseType: 'blob' })
  },
  // 协同文件编辑：一键应用 Diff（仅传 edit_id，落盘内容由服务端 pending 决定）
  applyEdit(editId) {
    return http.post(`/api/im/artifacts/edits/${editId}/apply`)
  },
  // 文档卡片编辑回写原文件
  saveArtifactFile(payload) {
    return http.post('/api/im/artifacts/files/save', payload)
  },
  // 文件版本历史（工作区快照）
  editHistory(agentId, filePath) {
    return http.get('/api/im/artifacts/edits/history', { params: { agent_id: agentId, file_path: filePath } })
  },
  // 回退到指定版本
  revertEdit(payload) {
    return http.post('/api/im/artifacts/edits/revert', payload)
  },
  // 人工确认：解析一条危险命令确认（允许/拒绝）
  resolveConfirmation(runId, confirmationId, payload) {
    return http.post(`/api/im/runs/${runId}/confirmations/${confirmationId}`, payload)
  },
  listRunConfirmations(runId) {
    return http.get(`/api/im/runs/${runId}/confirmations`)
  },
  answerQuestion(runId, confirmationId, payload) {
    return http.post(`/api/im/runs/${runId}/confirmations/${confirmationId}/answer`, payload)
  },
  // 工具详情 + 运行时参数配置
  toolDetail(toolName) {
    return http.get(`/api/im/tools/${toolName}`)
  },
  patchToolConfig(toolName, payload) {
    return http.patch(`/api/im/tools/${toolName}/config`, payload)
  },
}
