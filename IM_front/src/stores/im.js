import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { EventStream, clearEventSnapshot } from '@/utils/eventStream'
import { message as toast } from 'ant-design-vue'
import { API_BASE_URL } from '@/api/http'
import { imApi } from '@/api/im'
import { sseEventNames } from '@/utils/runtimeEvents'
import { selectedRunIds, selectionMatches } from '@/utils/conversationScope'

// 高频流式增量事件：后端快速输出时每个 token/chunk 都会发一条，
// 若每条都同步并入 reactive events 数组，会触发整条 computed 链（compactLlmEvents /
// chatItems）全量重算 + 整列表重渲染，主线程被打满导致页面卡死。这里把它们合批。
const HIGH_FREQUENCY_EVENTS = new Set(['llm.delta'])
// 聊天记录懒加载窗口：打开对话只取最新这么多条，向上滚动再按页补拉更早记录，
// 避免历史消息很多时一次性渲染整列造成主线程卡顿。
const MESSAGE_PAGE_SIZE = 30
// 非 reactive 的流式状态：去重索引 + 待落库缓冲 + 刷新句柄，放在模块级避免 Pinia 代理开销。
let seenEventIds = new Set()
let pendingEvents = []
let rafHandle = null
let timeoutHandle = null

function cancelEventFlush() {
  if (rafHandle !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(rafHandle)
  if (timeoutHandle !== null) clearTimeout(timeoutHandle)
  rafHandle = null
  timeoutHandle = null
}

function resetEventStreamState() {
  cancelEventFlush()
  seenEventIds = new Set()
  pendingEvents = []
}

function currentUserId() {
  try {
    return JSON.parse(localStorage.getItem('agent-im-auth') || 'null')?.user?.user_id || ''
  } catch {
    return ''
  }
}

export const useIMStore = defineStore('im', {
  state: () => ({
    agents: [],
    rooms: [],
    currentAgentId: '',
    deletedFileIds: [],
    currentConversation: null,
    currentGroupRoom: null,
    currentRoom: null,
    mode: 'empty',
    messages: [],
    // 懒加载：是否还有更早的历史可拉、是否正在拉更早、以及"切换对话"自增信号（用于回到底部）。
    hasMoreMessages: false,
    loadingOlderMessages: false,
    messagesEpoch: 0,
    conversations: [],
    groupConversations: [],
    currentGroupConversation: null,
    tasks: [],
    artifacts: [],
    contexts: [],
    events: [],
    loading: false,
    source: null,
    streamPath: '',
    streamStatus: 'closed',
    streamReadyRevision: 0,
    selectionRevision: 0,
    tools: [],
    // 待处理的人工确认（危险命令）：{confirmation_id, run_id, tool_name, arguments, ...}
    humanConfirmations: [],
    favorites: [],
    activity: [],
    lastSeenCount: (() => {
      try {
        return JSON.parse(localStorage.getItem('im-unread-lastseen-v1')) || {}
      } catch {
        return {}
      }
    })(),
  }),
  getters: {
    executorAgents(state) {
      return state.agents.filter((agent) => agent.agent_type === 'executor')
    },
    plannerAgents(state) {
      return state.agents.filter((agent) => agent.agent_type === 'planner')
    },
    groupRooms(state) {
      return state.rooms.filter((room) => room.type === 'group')
    },
    currentAgent(state) {
      return state.agents.find((agent) => agent.agent_id === state.currentAgentId) || null
    },
    unreadForConversation() {
      return (cid) => {
        const activeCid =
          this.currentGroupConversation?.conversation_id || this.currentConversation?.conversation_id || ''
        if (cid && cid === activeCid) return 0
        const item = this.activity.find((a) => a.conversation_id === cid)
        const total = item ? item.message_count : 0
        const seen = this.lastSeenCount[cid]
        if (seen === undefined) return 0
        return Math.max(0, total - seen)
      }
    },
    unreadForAgent() {
      return (agentId) =>
        this.activity
          .filter((a) => a.agent_id === agentId)
          .reduce((s, a) => s + this.unreadForConversation(a.conversation_id), 0)
    },
    unreadForRoom() {
      return (roomId) =>
        this.activity
          .filter((a) => a.room_id === roomId)
          .reduce((s, a) => s + this.unreadForConversation(a.conversation_id), 0)
    },
  },
  actions: {
    async bootstrap() {
      this.loading = true
      try {
        await Promise.all([this.fetchAgents(), this.fetchRooms(), this.fetchArtifacts(), this.fetchContexts()])
        if (this.groupRooms[0]) {
          await this.selectGroupRoom(this.groupRooms[0].room_id)
        } else if (this.executorAgents[0]) {
          await this.selectAgent(this.executorAgents[0].agent_id)
        }
        this.fetchTools().catch(() => {})
      } finally {
        this.loading = false
      }
    },
    persistLastSeen() {
      try {
        localStorage.setItem('im-unread-lastseen-v1', JSON.stringify(this.lastSeenCount))
      } catch {}
    },
    async fetchActivity() {
      try {
        const res = await imApi.activity()
        const items = res?.items || []
        this.activity = items
        const seen = { ...this.lastSeenCount }
        for (const item of items) {
          // 首次见到的会话以当前 message_count 作为基线，避免历史消息被算作未读。
          if (seen[item.conversation_id] === undefined) {
            seen[item.conversation_id] = item.message_count
          }
        }
        const activeCid =
          this.currentGroupConversation?.conversation_id || this.currentConversation?.conversation_id || ''
        if (activeCid) {
          // 打开中的会话保持已读，离开后即归零。
          const activeItem = items.find((a) => a.conversation_id === activeCid)
          if (activeItem) seen[activeCid] = activeItem.message_count
        }
        this.lastSeenCount = seen
        this.persistLastSeen()
      } catch {}
    },
    markConversationRead(conversationId, count) {
      // 以 activity 上报的 message_count 为准（与 unreadForConversation 的 total 同源），
      // 保证打开即读后 total - seen = 0；群聊里 this.messages.length 走的是 room 维度过滤，
      // 与 activity 的 conversation 维度计数口径可能不同，直接用它会让红点清不掉。
      const activityCount = this.activity.find((a) => a.conversation_id === conversationId)?.message_count
      const total = activityCount != null ? activityCount : count != null ? count : this.messages.length
      this.lastSeenCount = { ...this.lastSeenCount, [conversationId]: total }
      this.persistLastSeen()
    },
    startActivityPolling() {
      this.stopActivityPolling()
      this.fetchActivity()
      this._activityTimer = setInterval(() => {
        this.fetchActivity()
        // Health can change while SSE is unreachable; polling is read-only.
        if (this.tasks.some(task => ['pending', 'running'].includes(task.status))) this.fetchTasks().catch(() => {})
        if (this.messages.some(item => item.sender_type === 'user' && ['pending', 'running'].includes(item.status))) {
          this.refreshMessages().catch(() => {})
        }
      }, 10000)
      this._activityVisHandler = () => {
        if (document.visibilityState === 'visible') this.fetchActivity()
      }
      document.addEventListener('visibilitychange', this._activityVisHandler)
    },
    stopActivityPolling() {
      if (this._activityTimer) {
        clearInterval(this._activityTimer)
        this._activityTimer = null
      }
      if (this._activityVisHandler) {
        document.removeEventListener('visibilitychange', this._activityVisHandler)
        this._activityVisHandler = null
      }
    },
    async fetchAgents() {
      const response = await imApi.agents()
      this.agents = response.items || []
    },
    async fetchRooms() {
      const response = await imApi.rooms()
      this.rooms = response.items || []
    },
    async fetchArtifacts() {
      const response = await imApi.artifacts()
      this.artifacts = response.items || []
    },
    async fetchContexts() {
      const response = await imApi.contexts()
      this.contexts = response.items || []
    },
    async fetchConversations(agentId = this.currentAgentId) {
      if (!agentId) {
        this.conversations = []
        return []
      }
      const revision = this.selectionRevision
      const response = await imApi.agentConversations(agentId)
      const items = response.items || []
      if (revision === this.selectionRevision && this.currentAgentId === agentId) this.conversations = items
      return items
    },
    async fetchTasks(roomId = this.currentGroupRoom?.room_id) {
      const revision = this.selectionRevision
      const conversationId = this.currentGroupConversation?.conversation_id
      if (!roomId || !conversationId) {
        this.tasks = []
        return []
      }
      const response = await imApi.roomTasks(roomId, conversationId)
      const items = response.items || []
      if (selectionMatches(this, revision, roomId, conversationId)) this.tasks = items
      return items
    },
    async fetchGroupConversations(roomId) {
      const revision = this.selectionRevision
      const r = await imApi.roomConversations(roomId)
      const items = r.items || []
      if (revision === this.selectionRevision && this.currentRoom?.room_id === roomId) {
        this.groupConversations = items
      }
      return items
    },
    async refreshMessages() {
      // 只重拉最新窗口并与已加载的更早分页合并，避免发送/取消/重生成后把上滑加载的历史一次性丢弃。
      if (this.currentRoom?.type === 'group') {
        const revision = this.selectionRevision
        const roomId = this.currentRoom.room_id
        const conversationId = this.currentGroupConversation?.conversation_id
        if (!roomId || !conversationId) return this.messages
        const response = await imApi.roomMessages(
          roomId,
          conversationId,
          { limit: MESSAGE_PAGE_SIZE },
        )
        if (selectionMatches(this, revision, roomId, conversationId)) {
          this.applyLatestWindow(response.items || [])
        }
        return this.messages
      }
      if (this.currentConversation?.conversation_id) {
        const revision = this.selectionRevision
        const conversationId = this.currentConversation.conversation_id
        const response = await imApi.conversationMessages(
          conversationId,
          { limit: MESSAGE_PAGE_SIZE },
        )
        if (selectionMatches(this, revision, '', conversationId)) {
          this.applyLatestWindow(response.items || [])
        }
        return this.messages
      }
      return []
    },
    applyLatestWindow(items) {
      // 合并最新窗口：保留比窗口更早、且不在窗口内的已加载历史；窗口区间内消失的消息
      // （如重新生成删除的旧回复）随之移除。窗口为空时不动既有列表，避免瞬态清空。
      const list = items || []
      const oldestRecent = list.length ? (list[0].created_at || 0) : Infinity
      const recentIds = new Set(list.map((item) => item.message_id))
      const olderKept = this.messages.filter(
        (item) => (item.created_at || 0) < oldestRecent && !recentIds.has(item.message_id),
      )
      this.messages = [...olderKept, ...list]
    },
    async loadOlderMessages() {
      // 向上滚动触发：以当前最早一条为游标，往上补拉一页更早记录并前插。返回新增条数。
      if (this.loadingOlderMessages || !this.hasMoreMessages) return 0
      const oldest = this.messages[0]
      if (!oldest?.message_id) return 0
      this.loadingOlderMessages = true
      const revision = this.selectionRevision
      const roomId = this.currentRoom?.type === 'group' ? this.currentRoom.room_id : ''
      const conversationId = roomId
        ? this.currentGroupConversation?.conversation_id
        : this.currentConversation?.conversation_id
      try {
        let response
        if (this.currentRoom?.type === 'group') {
          if (!this.currentRoom.room_id) return 0
          response = await imApi.roomMessages(
            this.currentRoom.room_id,
            this.currentGroupConversation?.conversation_id,
            { limit: MESSAGE_PAGE_SIZE, before_id: oldest.message_id },
          )
        } else if (this.currentConversation?.conversation_id) {
          response = await imApi.conversationMessages(
            this.currentConversation.conversation_id,
            { limit: MESSAGE_PAGE_SIZE, before_id: oldest.message_id },
          )
        } else {
          return 0
        }
        if (!selectionMatches(this, revision, roomId, conversationId)) return 0
        const older = response.items || []
        const existing = new Set(this.messages.map((item) => item.message_id))
        const fresh = older.filter((item) => !existing.has(item.message_id))
        if (fresh.length) this.messages = [...fresh, ...this.messages]
        this.hasMoreMessages = Boolean(response.has_more)
        return fresh.length
      } finally {
        if (this.selectionRevision === revision) this.loadingOlderMessages = false
      }
    },
    mergeMessage(messageItem) {
      if (!messageItem?.message_id) return
      const index = this.messages.findIndex((item) => item.message_id === messageItem.message_id)
      if (index >= 0) {
        this.messages.splice(index, 1, { ...this.messages[index], ...messageItem })
      } else {
        this.messages.push(messageItem)
        this.messages.sort((a, b) => (a.created_at || 0) - (b.created_at || 0))
      }
    },
    async createRoom(payload) {
      const response = await imApi.createRoom(payload)
      await this.fetchRooms()
      if (response.item.type === 'group') {
        await this.selectGroupRoom(response.item.room_id)
      }
      return response.item
    },
    async updateRoom(roomId, payload) {
      const response = await imApi.updateRoom(roomId, payload)
      await this.fetchRooms()
      if (this.currentGroupRoom?.room_id === roomId) {
        this.currentGroupRoom = response.item
        this.currentRoom = response.item
      }
      return response.item
    },
    async deleteRoom(roomId) {
      this.selectionRevision += 1
      await imApi.deleteRoom(roomId)
      await this.invalidateStreamSnapshot(`/api/im/rooms/${roomId}/events`)
      if (this.currentGroupRoom?.room_id === roomId) {
        this.currentGroupRoom = null
        this.currentRoom = null
        this.groupConversations = []
        this.currentGroupConversation = null
        this.messages = []
        this.events = []
        this.tasks = []
        this.mode = 'empty'
      }
      await this.fetchRooms()
      if (this.mode === 'empty') {
        if (this.groupRooms[0]) await this.selectGroupRoom(this.groupRooms[0].room_id)
        else if (this.executorAgents[0]) await this.selectAgent(this.executorAgents[0].agent_id)
      }
    },
    async createAgent(payload) {
      const response = await imApi.createAgent(payload)
      await Promise.all([this.fetchAgents(), this.fetchContexts()])
      await this.selectAgent(response.item.agent_id)
      return response.item
    },
    async updateAgent(agentId, payload) {
      const response = await imApi.updateAgent(agentId, payload)
      // 重新拉取 agents 列表，让侧栏/抽屉/标题等基于 im.agents 的派生 UI 立即反映改动。
      await this.fetchAgents()
      return response.item
    },
    async builderChat(payload) {
      // 仅转发：对话式创建的草稿/回复由 ChatView 本地维护，不入全局 store。
      return imApi.builderChat(payload)
    },
    async deleteAgent(agentId) {
      if (this.currentAgentId === agentId) this.selectionRevision += 1
      await imApi.deleteAgent(agentId)
      if (this.currentAgentId === agentId) {
        this.closeStream()
        this.currentAgentId = ''
        this.currentConversation = null
        this.messages = []
        this.events = []
        this.conversations = []
        this.mode = 'empty'
      }
      await Promise.all([this.fetchAgents(), this.fetchRooms()])
      if (this.mode === 'empty') {
        if (this.groupRooms[0]) await this.selectGroupRoom(this.groupRooms[0].room_id)
        else if (this.executorAgents[0]) await this.selectAgent(this.executorAgents[0].agent_id)
      }
    },
    async createConversation(agentId = this.currentAgentId, title = '') {
      if (!agentId) return null
      const agent = this.agents.find((item) => item.agent_id === agentId)
      const response = await imApi.createAgentConversation(agentId, {
        title: title || `${agent?.name || 'Agent'} 对话`,
        metadata: { source: 'IM_front' },
      })
      await Promise.all([this.fetchRooms(), this.fetchConversations(agentId)])
      await this.selectConversation(response.item.conversation_id)
      return response.item
    },
    async deleteConversation(conversationId) {
      const groupRoomId = this.currentRoom?.type === 'group' ? this.currentRoom.room_id : ''
      const agentId = this.currentAgentId
      const wasCurrent = groupRoomId
        ? this.currentGroupConversation?.conversation_id === conversationId
        : this.currentConversation?.conversation_id === conversationId
      this.selectionRevision += 1
      await imApi.deleteConversation(conversationId)
      if (groupRoomId) {
        await this.invalidateStreamSnapshot(`/api/im/rooms/${groupRoomId}/events`)
        this.events = []
        this.humanConfirmations = []
        const conversations = await imApi.roomConversations(groupRoomId)
        if (this.currentRoom?.room_id !== groupRoomId) return
        this.groupConversations = conversations.items || []
        const previousId = this.currentGroupConversation?.conversation_id
        const nextId = this.groupConversations.some(item => item.conversation_id === previousId)
          ? previousId
          : this.groupConversations[0]?.conversation_id
        if (nextId) {
          await this.selectGroupConversation(nextId)
        } else {
          this.currentGroupConversation = null
          this.messages = []
          this.tasks = []
        }
        if (this.currentRoom?.room_id === groupRoomId) this.connectRoom(groupRoomId)
        return
      }
      await this.invalidateStreamSnapshot(`/api/im/conversations/${conversationId}/events`)
      if (this.currentRoom?.type === 'group' || this.currentAgentId !== agentId) return
      if (wasCurrent && this.currentConversation?.conversation_id === conversationId) {
        this.currentConversation = null
        this.messages = []
        this.events = []
        this.mode = agentId ? 'agent' : 'empty'
      }
      await this.fetchConversations(agentId)
      if (wasCurrent && this.currentAgentId && this.conversations[0]) {
        await this.selectConversation(this.conversations[0].conversation_id)
      }
    },
    async selectAgent(agentId) {
      const revision = ++this.selectionRevision
      this.currentAgentId = agentId
      this.currentConversation = null
      this.currentGroupRoom = null
      this.currentRoom = null
      this.groupConversations = []
      this.currentGroupConversation = null
      this.messages = []
      this.hasMoreMessages = false
      this.events = []
      this.tasks = []
      this.mode = 'agent'
      this.closeStream()
      const conversations = await this.fetchConversations(agentId)
      if (revision !== this.selectionRevision || this.currentAgentId !== agentId) return
      if (conversations[0]) {
        await this.selectConversation(conversations[0].conversation_id)
      }
    },
    async selectConversation(conversationId) {
      const revision = ++this.selectionRevision
      const [conversationResponse, messageResponse] = await Promise.all([
        imApi.conversation(conversationId),
        imApi.conversationMessages(conversationId, { limit: MESSAGE_PAGE_SIZE }),
      ])
      if (revision !== this.selectionRevision) return
      this.currentConversation = conversationResponse.item
      this.currentGroupRoom = null
      this.currentRoom = null
      this.groupConversations = []
      this.currentGroupConversation = null
      this.currentAgentId = conversationResponse.item.agent_id || this.currentAgentId
      this.mode = 'dm'
      this.messages = messageResponse.items || []
      this.hasMoreMessages = Boolean(messageResponse.has_more)
      this.messagesEpoch += 1
      this.events = []
      this.tasks = []
      this.markConversationRead(conversationId, this.messages.length)
      this.connectConversation(conversationId)
      await this.fetchConversations(this.currentAgentId)
      this.loadFavorites().catch(() => {})
    },
    async selectGroupRoom(roomId) {
      const revision = ++this.selectionRevision
      const [roomResponse, conversationsResponse] = await Promise.all([
        imApi.room(roomId),
        imApi.roomConversations(roomId),
      ])
      if (revision !== this.selectionRevision) return
      this.currentGroupRoom = roomResponse.item
      this.currentConversation = null
      this.currentRoom = roomResponse.item
      this.currentAgentId = ''
      this.mode = 'group'
      this.messages = []
      this.hasMoreMessages = false
      this.events = []
      this.conversations = []
      this.currentGroupConversation = null
      this.groupConversations = conversationsResponse.items || []
      const first = this.groupConversations[0]
      if (first) {
        await this.selectGroupConversation(first.conversation_id)
      } else {
        this.currentGroupConversation = null
        this.messages = []
        this.tasks = []
      }
      // ROOM SSE 只在这里连接一次；先确定会话，避免 ready 回调拉取整个房间的数据。
      if (this.currentRoom?.room_id === roomId) this.connectRoom(roomId)
    },
    async selectGroupConversation(conversationId) {
      const revision = ++this.selectionRevision
      this.currentGroupConversation = this.groupConversations.find(
        (item) => item.conversation_id === conversationId,
      ) || null
      const roomId = this.currentRoom?.room_id
      if (!roomId || !this.currentGroupConversation) return
      this.messages = []
      this.tasks = []
      this.humanConfirmations = []
      this.loadingOlderMessages = false
      const [messageResponse, taskResponse] = await Promise.all([
        imApi.roomMessages(roomId, conversationId, { limit: MESSAGE_PAGE_SIZE }),
        imApi.roomTasks(roomId, conversationId),
      ])
      if (!selectionMatches(this, revision, roomId, conversationId)) return
      this.messages = messageResponse.items || []
      this.hasMoreMessages = Boolean(messageResponse.has_more)
      this.messagesEpoch += 1
      this.tasks = taskResponse.items || []
      this.markConversationRead(conversationId, this.messages.length)
      this.loadFavorites().catch(() => {})
    },
    async createGroupConversation(title = '') {
      const r = await imApi.createRoomConversation(this.currentRoom.room_id, { title })
      await this.fetchGroupConversations(this.currentRoom.room_id)
      await this.selectGroupConversation(r.item.conversation_id)
      return r.item
    },
    async sendMessage(payload, options = {}) {
      // Capture destination before any await: switching chats must not redirect the send.
      const roomId = this.mode === 'group' ? this.currentRoom?.room_id : ''
      const agentId = this.currentAgentId
      let conversationId = roomId ? this.currentGroupConversation?.conversation_id : this.currentConversation?.conversation_id
      if (!roomId && !conversationId && agentId) {
        const created = await imApi.createAgentConversation(agentId, { metadata: { source: 'IM_front' } })
        conversationId = created.item.conversation_id
        if (typeof payload.metadata?.memory_write_enabled === 'boolean') {
          await imApi.updateConversation(conversationId, { memory_write_enabled: payload.metadata.memory_write_enabled })
        }
        options.onConversationCreated?.(conversationId)
        if (this.mode === 'agent' && this.currentAgentId === agentId) await this.selectConversation(conversationId)
      }
      if (!roomId && !conversationId) throw new Error('请先选择或创建会话')
      const isCurrent = () => roomId
        ? this.currentRoom?.room_id === roomId && (this.currentGroupConversation?.conversation_id || '') === (conversationId || '')
        : this.currentConversation?.conversation_id === conversationId
      const response = roomId
        ? await imApi.addMessage(roomId, { ...payload, conversation_id: conversationId })
        : await imApi.addConversationMessage(conversationId, payload)
      const messageItem = response.item
      options.onCommitted?.(messageItem)
      if (isCurrent()) this.mergeMessage(messageItem)
      try {
        if (roomId) {
          const { onCommitted, onConversationCreated, ...dispatchOptions } = options
          const result = await imApi.dispatch(roomId, { message_id: messageItem.message_id, ...dispatchOptions })
          if (isCurrent() && result.item?.type === 'confirmation') this.mergeMessage(result.item.confirmation)
          if (isCurrent()) await this.fetchTasks()
        } else {
          await imApi.replyConversation(conversationId, { message_id: messageItem.message_id, auto_start: options.auto_start ?? true })
        }
        if (isCurrent()) await this.refreshMessages()
      } catch {
        toast.warning('消息已发送，启动或刷新失败；可在原消息上重试，无需重新发送')
      }
      return messageItem
    },
    markFileDeleted(fileId) {
      if (!this.deletedFileIds.includes(fileId)) this.deletedFileIds.push(fileId)
      this.messages = this.messages.map(item => ({ ...item, content_parts: (item.content_parts || []).map(part =>
        part.file_id === fileId ? { ...part, metadata: { ...part.metadata, file_status: 'delete' } } : part) }))
    },
    async dispatch(messageId, payload = {}) {
      return imApi.dispatch(this.currentGroupRoom.room_id, { message_id: messageId, ...payload })
    },
    async cancelActiveRun(runId) {
      if (!this.currentGroupRoom?.room_id || !runId) return null
      const response = await imApi.cancelRoomRun(this.currentGroupRoom.room_id, runId)
      this.messages = this.messages.map((messageItem) => (
        messageItem.run_id === runId ? { ...messageItem, cancel_requested: true } : messageItem
      ))
      await Promise.all([this.fetchTasks(), this.refreshMessages()])
      return response.item
    },
    async cancelActiveReply(messageId) {
      if (!this.currentConversation?.conversation_id || !messageId) return null
      const response = await imApi.cancelConversationMessage(this.currentConversation.conversation_id, messageId)
      this.messages = this.messages.map((messageItem) => (
        messageItem.message_id === messageId ? { ...messageItem, cancel_requested: true } : messageItem
      ))
      await Promise.all([this.refreshMessages(), this.fetchConversations()])
      return response.item
    },
    async recordAction(messageId, payload) {
      return imApi.action(messageId, payload)
    },
    async resolveHumanConfirmation(runId, confirmationId, approved, reason = '') {
      // 乐观移除：无论后端结果如何先从待办里去掉，避免重复点击
      this.humanConfirmations = this.humanConfirmations.filter((c) => c.confirmation_id !== confirmationId)
      return imApi.resolveConfirmation(runId, confirmationId, { approved, reason })
    },
    async answerHumanQuestion(runId, confirmationId, payload) {
      const revision = this.selectionRevision
      const response = await imApi.answerQuestion(runId, confirmationId, payload)
      if (revision === this.selectionRevision) {
        this.mergeMessage(response.message)
        this.messages = this.messages.map(item => item.metadata?.question?.confirmation_id === confirmationId
          ? { ...item, metadata: { ...item.metadata, question: response.item } } : item)
        this.humanConfirmations = this.humanConfirmations.filter(item => item.confirmation_id !== confirmationId)
      }
      return response
    },
    async fetchTools() {
      if (this.tools.length) return this.tools
      const r = await imApi.toolsCatalog()
      this.tools = r.items || []
      return this.tools
    },
    async uploadAvatar(file) {
      const base64 = await new Promise((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = (e) => resolve(e.target.result.split(',')[1])
        reader.onerror = reject
        reader.readAsDataURL(file)
      })
      const r = await imApi.uploadArtifact({
        filename: file.name,
        content_base64: base64,
        content_type: file.type,
      })
      const artifact_id = r.item.artifact_id
      return API_BASE_URL + '/api/im/artifacts/' + artifact_id
    },
    async loadFavorites() {
      let scopeType, scopeId
      const revision = this.selectionRevision
      if (this.currentRoom?.type === 'group') {
        if (!this.currentGroupConversation) {
          this.favorites = []
          return
        }
        scopeType = 'conversation'
        scopeId = this.currentGroupConversation.conversation_id
      } else if (this.currentConversation) {
        scopeType = 'conversation'
        scopeId = this.currentConversation.conversation_id
      } else {
        this.favorites = []
        return
      }
      const r = await imApi.favorites(scopeType, scopeId)
      const roomId = this.currentRoom?.type === 'group' ? this.currentRoom.room_id : ''
      if (selectionMatches(this, revision, roomId, scopeId)) this.favorites = r.items || []
    },
    async favoriteMessage(messageId, title = '') {
      await imApi.favoriteMessage(messageId, { title })
      await this.loadFavorites()
    },
    async removeFavorite(favoriteId) {
      await imApi.deleteFavorite(favoriteId)
      await this.loadFavorites()
    },
    async toggleFavoriteEnabled(favoriteId, enabled) {
      await imApi.updateFavorite(favoriteId, { enabled })
      await this.loadFavorites()
    },
    async bundleArtifacts(artifacts, filename = 'artifacts.zip') {
      return imApi.bundleArtifacts({ artifacts, filename })
    },
    async toggleConversationPinned(conversationId, pinned) {
      await imApi.updateConversation(conversationId, { pinned })
      if (this.currentRoom?.type === 'group') {
        await this.fetchGroupConversations(this.currentRoom.room_id)
      } else {
        await this.fetchConversations()
      }
    },
    async toggleConversationArchived(conversationId, archived) {
      await imApi.updateConversation(conversationId, { archived })
      if (this.currentRoom?.type === 'group') {
        await this.fetchGroupConversations(this.currentRoom.room_id)
      } else {
        await this.fetchConversations()
      }
    },
    async regenerateReply(message) {
      // 既支持点 agent 回复重生（取其 reply_to 指向的用户消息），也支持直接对失败/取消的
      // 用户消息重试（此时没有 agent 回复消息可点）。后端 regenerate 始终以用户消息为入口。
      const userId =
        message?.sender_type === 'user' ? message.message_id : message?.metadata?.reply_to
      if (!userId || !this.currentConversation) return
      await imApi.regenerateConversationMessage(this.currentConversation.conversation_id, userId)
      await this.refreshMessages()
      await this.fetchConversations()
    },
    closeStream() {
      if (this.source) {
        this.source.close()
        this.source = null
      }
      // 切换/断开会话时清空去重索引与增量缓冲，避免跨会话串味或残留 rAF 把旧 batch 并入新数组。
      resetEventStreamState()
      this.streamStatus = 'closed'
    },
    async invalidateStreamSnapshot(path) {
      const source = this.streamPath === path ? this.source : null
      if (source) {
        this.closeStream()
        try { await source.clearSnapshot() } catch { /* IndexedDB may be unavailable. */ }
        return
      }
      const userId = currentUserId()
      if (!userId) return
      try { await clearEventSnapshot(`${API_BASE_URL}${path}`, userId) } catch { /* Cache cleanup is best-effort. */ }
    },
    connectRoom(roomId) {
      this.connectStream(`/api/im/rooms/${roomId}/events`)
    },
    connectConversation(conversationId) {
      this.connectStream(`/api/im/conversations/${conversationId}/events`)
    },
    retryStream() {
      if (this.streamPath) this.connectStream(this.streamPath)
    },
    connectStream(path) {
      this.closeStream()
      this.streamPath = path
      const source = markRaw(new EventStream(`${API_BASE_URL}${path}`))
      this.streamStatus = 'connecting'
      let syncing = false
      const consume = (event) => {
        if (this.source !== source) return
        const item = JSON.parse(event.data)
        this.consumeEvent(item, { replay: syncing })
      }
      source.addEventListener('stream.restore', (event) => {
        if (this.source !== source) return
        resetEventStreamState()
        this.events = JSON.parse(event.data)
        seenEventIds = new Set(this.events.map(item => item.event_id))
      })
      source.onmessage = consume
      for (const name of sseEventNames) {
        source.addEventListener(name, consume)
      }
      source.addEventListener('stream.reset', () => {
        if (this.source !== source) return
        syncing = true
        resetEventStreamState()
        this.events = []
        this.humanConfirmations = []
      })
      source.addEventListener('stream.ready', async () => {
        if (this.source !== source) return
        syncing = false
        this.streamStatus = 'connected'
        this.streamReadyRevision++
        await this.refreshMessages().catch(() => {})
        if (this.source !== source) return
        if (this.currentRoom?.type === 'group') await this.fetchTasks().catch(() => {})
        await this.syncHumanConfirmations(source).catch(() => {})
      })
      source.onopen = () => { if (this.source === source) this.streamStatus = 'connected' }
      source.onerror = event => {
        if (this.source === source) this.streamStatus = event.terminal ? 'error' : 'reconnecting'
      }
      this.source = source
    },
    async syncHumanConfirmations(source = this.source) {
      const revision = this.selectionRevision
      const roomId = this.currentRoom?.type === 'group' ? this.currentRoom.room_id : ''
      const conversationId = roomId
        ? this.currentGroupConversation?.conversation_id
        : this.currentConversation?.conversation_id
      const ids = selectedRunIds(this)
      const responses = await Promise.all(ids.map(id => imApi.listRunConfirmations(id)))
      if (this.source === source && selectionMatches(this, revision, roomId, conversationId)) {
        this.humanConfirmations = responses.flatMap(r => r.items || [])
        if (this.humanConfirmations.some(item => item.kind === 'question') ||
            this.messages.some(item => item.metadata?.source === 'human_question')) {
          await this.refreshMessages()
        }
      }
    },
    flushPendingEvents() {
      cancelEventFlush()
      if (!pendingEvents.length) return
      const batch = pendingEvents
      pendingEvents = []
      // 单次 reactive 赋值合并整批增量，把每帧的重算/重渲染压到一次。
      this.events = this.events.concat(batch)
    },
    scheduleEventFlush() {
      if (rafHandle !== null || timeoutHandle !== null) return
      const flush = () => this.flushPendingEvents()
      if (typeof requestAnimationFrame === 'function') rafHandle = requestAnimationFrame(flush)
      // 安全网：后台标签页里 rAF 会暂停，用 timeout 兜底，避免缓冲无限增长。
      timeoutHandle = setTimeout(flush, 200)
    },
    consumeEvent(event, { replay = false } = {}) {
      if (!event?.event_id || seenEventIds.has(event.event_id)) return
      seenEventIds.add(event.event_id)

      // 高频流式增量先进缓冲区，rAF 合批落库，避免逐 token 卡死主线程。
      // 这类事件在下方 if 链里没有任何副作用，缓冲不影响业务逻辑。
      if (HIGH_FREQUENCY_EVENTS.has(event.name)) {
        pendingEvents.push(event)
        this.scheduleEventFlush()
        return
      }

      // 控制类事件即时处理：先把缓冲的尾部增量落库（保证顺序、不丢 llm.completed/agent.final 前的尾 token），
      // 再 push 自身并执行副作用。
      this.flushPendingEvents()
      this.events.push(event)
      // Historical display data must not replay notifications or approvals.
      if (replay) return
      if (event.name === 'task.updated') {
        const runId = event.run_id || event.payload?.run_id
        const health = event.payload?.execution_health
        if (runId && health) {
          this.tasks = this.tasks.map(item => item.run_id === runId ? { ...item, execution_health: health } : item)
          this.messages = this.messages.map(item => item.run_id === runId ? { ...item, execution_health: health } : item)
        }
        if (this.currentRoom?.type === 'group') this.fetchTasks().catch(() => {})
      }
      if (event.name === 'message.created') {
        const messageItem = event.payload?.message
        if (this.currentRoom?.type === 'group') {
          // 同房间内的多会话隔离：只并入当前会话的消息，忽略其它会话。
          if (messageItem && messageItem.conversation_id === this.currentGroupConversation?.conversation_id) {
            this.mergeMessage(messageItem)
          }
          this.fetchTasks().catch(() => {})
          // 刷新群会话列表排序（最近活跃靠前等）。
          this.fetchGroupConversations(this.currentRoom.room_id).catch(() => {})
        } else {
          if (messageItem) this.mergeMessage(messageItem)
        }
        if (this.currentAgentId) this.fetchConversations().catch(() => {})
        // 控制频次事件：每条持久化消息刷新一次未读，活跃会话同步保持已读。
        this.fetchActivity().catch(() => {})
      }
      // agent_flow 层的工具人工确认（如危险 bash 命令）：弹窗审批用
      if (event.name === 'human.confirmation.requested' && event.payload?.confirmation_id) {
        const item = event.payload
        this.syncHumanConfirmations().catch(() => {})
        if (!this.humanConfirmations.some((c) => c.confirmation_id === item.confirmation_id)) {
          this.humanConfirmations = [...this.humanConfirmations, item]
        }
      }
      if (event.name === 'human.confirmation.resolved' && event.payload?.confirmation_id) {
        this.humanConfirmations = this.humanConfirmations.filter(
          (c) => c.confirmation_id !== event.payload.confirmation_id,
        )
        const item = event.payload
        this.messages = this.messages.map(message => message.metadata?.question?.confirmation_id === item.confirmation_id
          ? { ...message, metadata: { ...message.metadata, question: item } } : message)
      }
      if (['workflow.failed', 'workflow.finished', 'run.cancelled', 'agent.reply.finished'].includes(event.name)) {
        this.syncHumanConfirmations().catch(() => {})
      }
      if (event.name === 'file.deleted') this.markFileDeleted(event.payload?.file_id)
      if (event.name === 'message.regenerated') {
        this.refreshMessages().catch(() => {})
      }
      if (event.name === 'conversation.updated' && this.currentAgentId) {
        this.fetchConversations().catch(() => {})
      }
      if (event.name === 'favorite.created' || event.name === 'favorite.updated' || event.name === 'favorite.deleted') {
        this.loadFavorites().catch(() => {})
      }
      if (event.name === 'run.created') {
        if (this.currentRoom?.type === 'group') this.fetchTasks().catch(() => {})
        // 把 run_id 回填到对应消息，便于 ChatView 推导会话的 run-id 集合做时间线隔离。
        const messageId = event.payload?.message_id
        const runId = event.payload?.run?.run_id
        if (messageId && runId) {
          this.messages = this.messages.map((messageItem) => (
            messageItem.message_id === messageId ? { ...messageItem, run_id: runId } : messageItem
          ))
        }
      }
      if (event.name === 'agent.reply.started') {
        const messageId = event.payload?.message_id
        this.messages = this.messages.map((messageItem) => (
          messageItem.message_id === messageId ? { ...messageItem, status: 'running', run_id: event.run_id || event.payload?.run_id || messageItem.run_id } : messageItem
        ))
      }
      if (event.name === 'agent.reply.finished' || event.name === 'workflow.finished') {
        const { message_id: messageId, run_id: runId } = event.payload || {}
        this.messages = this.messages.map(item => (
          (runId ? item.run_id === runId : messageId && item.message_id === messageId)
            ? { ...item, status: 'finished', cancel_requested: false } : item
        ))
        if (this.currentRoom?.type === 'group') this.fetchTasks().catch(() => {})
      }
      if (event.name === 'run.cancelled') {
        const runId = event.payload?.run_id
        const messageId = event.payload?.message_id
        this.messages = this.messages.map((messageItem) => (
          (runId ? messageItem.run_id === runId : messageId && messageItem.message_id === messageId)
            ? { ...messageItem, status: 'cancelled' }
            : messageItem
        ))
        if (this.currentRoom?.type === 'group') this.fetchTasks().catch(() => {})
      }
      if (event.name === 'workflow.failed') {
        // 用户中断 -> cancelled；真正执行失败 -> failed（缺这一支会导致失败消息一直停在 running，
        // 触发不了「重试」按钮）。run_id 可能缺省，加非空守卫避免误匹配 run_id 同为空的其它消息。
        const runId = event.payload?.run_id
        const messageId = event.payload?.message_id
        const nextStatus = event.payload?.cancelled ? 'cancelled' : 'failed'
        this.messages = this.messages.map((messageItem) => (
          (runId ? messageItem.run_id === runId : messageId && messageItem.message_id === messageId)
            ? { ...messageItem, status: nextStatus, cancel_requested: false }
            : messageItem
        ))
        if (this.currentRoom?.type === 'group') this.fetchTasks().catch(() => {})
        if (this.currentAgentId) this.fetchConversations().catch(() => {})
      }
    },
  },
})
