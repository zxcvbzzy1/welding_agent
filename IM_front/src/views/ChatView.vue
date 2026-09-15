<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import {
  BranchesOutlined,
  CheckOutlined,
  CloseOutlined,
  CloudUploadOutlined,
  CopyOutlined,
  DeleteOutlined,
  DeploymentUnitOutlined,
  DownloadOutlined,
  EditOutlined,
  EllipsisOutlined,
  ExpandAltOutlined,
  FileTextOutlined,
  InboxOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  PlusOutlined,
  PushpinOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SendOutlined,
  StarFilled,
  StarOutlined,
  StopOutlined,
  TeamOutlined,
  UserAddOutlined,
} from '@ant-design/icons-vue'
import {
  buildConversationTraces,
  buildGroupTimelineItems,
  compactLlmEvents,
  eventActor,
  eventActorId,
  eventColor,
  eventContent,
  eventRunScope,
  eventTitle,
  eventTone,
  isPrimaryOutputEvent,
  runtimeEventNames,
  traceDuration,
} from '@/utils/runtimeEvents'
import { useAuthStore } from '@/stores/auth'
import { useIMStore } from '@/stores/im'
import { imApi } from '@/api/im'
import ArtifactCard from '@/components/ArtifactCard.vue'
import ChatAttachments from '@/components/ChatAttachments.vue'
import ChatFileCard from '@/components/ChatFileCard.vue'
import { useChatFiles } from '@/composables/useChatFiles'
import { renderMarkdown, looksLikeMarkdownDoc } from '@/utils/markdown'

const im = useIMStore()
const auth = useAuthStore()
const listRef = ref(null)
const composerRef = ref(null)
const creatingRoom = ref(false)
const sending = ref(false)
const drawerOpen = ref(false)
const createOpen = ref(false)
const agentCreateOpen = ref(false)
const creatingAgent = ref(false)
// 编辑当前 Agent：复用创建弹窗/表单；空串=创建模式，非空=编辑该 agent_id。
const editingAgentId = ref('')
const isEditingAgent = computed(() => Boolean(editingAgentId.value))
const savingPlanner = ref(false)
const mentionOpen = ref(false)
const draftKey = computed(() => {
  if (im.currentRoom?.type === 'group') return `group:${im.currentRoom.room_id}:${im.currentGroupConversation?.conversation_id || ''}`
  return im.currentConversation ? `conversation:${im.currentConversation.conversation_id}` : `agent:${im.currentAgentId}`
})
const chatFiles = useChatFiles(draftKey)
const currentDraft = chatFiles.draft
function draftField(field) {
  return computed({ get: () => currentDraft.value[field], set: value => { currentDraft.value[field] = value } })
}
const composer = draftField('text')
// 输入框「展开」：打开一个接近全屏的弹窗编辑器，与底部输入框共享同一份 composer。
const composerExpanded = ref(false)
const expandedComposerRef = ref(null)
const mentions = draftField('mentions')
const drawerPlannerId = ref('default_planner')
const traceOpenOverrides = ref({})
// trace-card 懒加载缓存：{ [runId]: { status: 'loading'|'loaded'|'error', byId: Map<event_id, fullEvent> } }
// 折叠态只显示事件数量；展开某条 trace 时才按 run_id 拉全量正文，按 event_id 换入渲染。
const runEventsCache = ref({})
const sidebarCollapsed = ref(false)
// 侧栏分栏（Agents / Agent 群 / 对话列表）折叠状态，持久化到 localStorage。
function loadSidebarBool(key, defaultVal = true) {
  const raw = localStorage.getItem(key)
  return raw === null ? defaultVal : raw === 'true'
}
const agentsSectionOpen = ref(loadSidebarBool('im:sidebar:agents-open'))
const groupsSectionOpen = ref(loadSidebarBool('im:sidebar:groups-open'))
const feedSectionOpen = ref(loadSidebarBool('im:sidebar:feed-open'))
watch(agentsSectionOpen, (v) => localStorage.setItem('im:sidebar:agents-open', v))
watch(groupsSectionOpen, (v) => localStorage.setItem('im:sidebar:groups-open', v))
watch(feedSectionOpen, (v) => localStorage.setItem('im:sidebar:feed-open', v))
const previewMessage = ref(null)
const replyTarget = draftField('reply')
const quoteTarget = draftField('quote')
// 选区编辑目标：来自 ArtifactCard 的 selection-edit 事件（选中代码 -> 在聊天中描述修改）
const selectionEditTarget = draftField('selection')
const conversationQuery = ref('')
const archivedOpen = ref(false)
const uploadingAvatar = ref(false)
const roomForm = reactive({
  type: 'group',
  title: '',
  member_agent_ids: [],
})
const agentForm = reactive({
  name: '',
  agent_kind: 'native',
  agent_type: 'executor',
  description: '',
  role_prompt: '',
  workdir: '',
  permission_profile: 'human_confirm',
  avatar_url: '',
  tool_names: [],
  tool_fields: [],
  tags: [],
})
// 对话式创建：本地维护聊天记录与草稿，最终应用到 agentForm 后仍走原有 createAgent。
const agentCreateTab = ref('form')
const builderMessages = ref([])
const builderInput = ref('')
const builderDraft = ref(null)
const builderReady = ref(false)
const builderSending = ref(false)

const toolFieldLabels = {
  system: '系统',
  search: '搜索',
  memory: '记忆',
  human: '人工协作',
  write_agent: '编写 Agent',
  other: '其它',
}
const agentKindOptions = [
  { label: 'Native', value: 'native' },
  { label: 'Claude Code', value: 'claude_code' },
  { label: 'Codex', value: 'codex' },
]
const permissionProfileOptions = [
  { label: '人工确认', value: 'human_confirm' },
  { label: '只读计划', value: 'plan' },
]
const artifactTypeLabels = {
  message: '消息',
  image: '图片',
  diff: 'Diff',
  document: '文档',
  web: '网页',
  deploy: '部署',
}
const dispatchOptions = reactive({
  auto_start: true,
  context_id: 'default_step',
  max_replan_rounds: 3,
})

const isNativeAgentForm = computed(() => agentForm.agent_kind === 'native')

const showToolPicker = computed(() => isNativeAgentForm.value && agentForm.agent_type === 'executor')

const groupedTools = computed(() => {
  const order = ['system', 'search', 'memory', 'human', 'robot', 'other']
  const buckets = {}
  for (const tool of im.tools || []) {
    const field = tool.field || 'other'
    if (!buckets[field]) buckets[field] = []
    buckets[field].push(tool)
  }
  return order
    .filter((field) => buckets[field]?.length)
    .map((field) => ({
      field,
      label: toolFieldLabels[field] || field,
      tools: buckets[field],
    }))
})

// 群聊与单聊侧栏统一使用对话列表：群聊取 groupConversations，单聊取 conversations。
const feedConversations = computed(() => {
  return im.currentRoom?.type === 'group' ? im.groupConversations : im.conversations
})

const conversationGroups = computed(() => {
  const query = conversationQuery.value.trim().toLowerCase()
  const matches = (item) => {
    if (!query) return true
    const title = (item.title || '').toLowerCase()
    const text = (latestMessageText(item) || '').toLowerCase()
    return title.includes(query) || text.includes(query)
  }
  const list = (feedConversations.value || [])
    .filter(matches)
    .slice()
    .sort((a, b) => (sideItemTime(b) || 0) - (sideItemTime(a) || 0))
  // 归档优先级高于置顶（与后端"归档自动取消置顶"一致），保证三组互斥。
  return {
    pinned: list.filter((item) => item.pinned === true && item.archived !== true),
    normal: list.filter((item) => item.pinned !== true && item.archived !== true),
    archived: list.filter((item) => item.archived === true),
  }
})

const currentMembers = computed(() => {
  const ids = im.currentRoom?.member_agent_ids || []
  return ids.map(agentById).filter(Boolean)
})

const activeTitle = computed(() => {
  if (im.currentRoom?.type === 'group') {
    return im.currentGroupConversation?.title || im.currentRoom.title
  }
  if (im.currentRoom) return im.currentRoom.title
  if (im.currentConversation) return im.currentConversation.title
  if (im.currentAgent) return im.currentAgent.name
  return '选择 Agent 或群聊'
})

const activeSubtitle = computed(() => {
  if (im.currentRoom?.type === 'group') return `${currentMembers.value.length} agents · PlanOrchestrator`
  if (im.currentAgent) return `${agentKind(im.currentAgent.agent_id)} · ${im.currentAgent.agent_type}`
  return '开始一次协作'
})

const heroAvatar = computed(() => {
  if (im.currentRoom?.type === 'group') return im.currentRoom?.avatar_url || ''
  return im.currentAgent?.metadata?.avatar_url || ''
})

const roomAgentOptions = computed(() => {
  return im.executorAgents.map((agent) => ({
    label: `${agent.name} · ${agent.metadata?.agent_kind || 'native'}`,
    value: agent.agent_id,
}))
})

const mentionCandidates = computed(() => {
  if (im.currentRoom?.type !== 'group' || !mentionOpen.value) return []
  const lastAt = composer.value.lastIndexOf('@')
  const query = lastAt >= 0 ? composer.value.slice(lastAt + 1).trim().toLowerCase() : ''
  return currentMembers.value.filter((agent) => {
    return !query || agent.name.toLowerCase().includes(query) || agent.agent_id.toLowerCase().includes(query)
  })
})

const displayEvents = computed(() => compactLlmEvents(im.events))

// 仅在已分页的长对话里提示"到顶了"，短对话（首屏即全量）不显示，避免噪声。
const showHistoryStart = computed(() => !im.hasMoreMessages && chatItems.value.length > 20)

const runningGroupTask = computed(() => {
  if (im.currentRoom?.type !== 'group') return null
  return [...im.tasks]
    .filter((task) => task.run_id && ['running', 'pending', 'sent'].includes(task.status))
    .sort((a, b) => (b.updated_at || b.created_at || 0) - (a.updated_at || a.created_at || 0))[0] || null
})

const runningConversationMessage = computed(() => {
  if (im.currentRoom?.type === 'group') return null
  return [...im.messages]
    .filter((item) => item.sender_type === 'user' && item.status === 'running')
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0] || null
})

const canInterrupt = computed(() => {
  return im.currentRoom?.type === 'group' ? Boolean(runningGroupTask.value) : Boolean(runningConversationMessage.value)
})

const chatItems = computed(() => {
  if (im.currentRoom?.type === 'group') {
    // SSE 是房间级的，im.events 含本房间全部 run 的 runtime events；
    // 用当前对话消息上的 run_id 集合过滤，避免把其它对话的编排轨迹串进当前视图。
    const convRunIds = new Set(im.messages.map((message) => message.run_id).filter(Boolean))
    const groupEvents = displayEvents.value.filter(
      (event) => runtimeEventNames.has(event.name) && convRunIds.has(eventRunScope(event)),
    )
    return buildGroupTimelineItems({
      messages: im.messages,
      events: groupEvents,
    })
  }
  const traces = buildConversationTraces({
    messages: im.messages,
    events: displayEvents.value.filter((event) => runtimeEventNames.has(event.name)),
    conversationId: im.currentConversation?.conversation_id || '',
    agentId: im.currentConversation?.agent_id || im.currentAgentId,
  })
  const traceItems = traces.map((trace) => ({
    key: `trace-${trace.key}`,
    kind: 'trace',
    created_at: trace.insert_before_message_id
      ? (im.messages.find((message) => message.message_id === trace.insert_before_message_id)?.created_at || trace.created_at) - 0.0001
      : trace.created_at,
    trace,
  }))
  const messageItems = im.messages.map((message) => ({
    key: `message-${message.message_id}`,
    kind: 'message',
    created_at: message.created_at || 0,
    message,
  }))
  const eventItems = displayEvents.value
    .filter((event) => runtimeEventNames.has(event.name) && isPrimaryOutputEvent(event, 'dm'))
    .map((event) => ({
      key: `event-${event.event_id || `${event.name}-${event.created_at}`}`,
      kind: 'event',
      created_at: event.created_at || 0,
      event,
    }))
  return [...messageItems, ...traceItems, ...eventItems].sort((a, b) => a.created_at - b.created_at)
})

const chatScrollSignature = computed(() => {
  return chatItems.value
    .map((entry) => {
      if (entry.kind === 'message') {
        const messageItem = entry.message
        const contentSize = (messageItem.content_parts || [])
          .map((part) => part.text || part.diff || part.title || part.description || part.name || part.url || part.type || '')
          .join('').length
        return `m:${messageItem.message_id}:${messageItem.status}:${messageItem.created_at}:${contentSize}`
      }
      if (entry.kind === 'trace') {
        const trace = entry.trace
        const latest = trace.latest_event || {}
        return `t:${trace.key}:${trace.resolved}:${trace.event_count}:${latest.event_id || latest.name}:${latest.created_at}`
      }
      if (entry.kind === 'artifact') {
        const artifact = entry.artifact || {}
        const size = (artifact.content || artifact.url || artifact.after || artifact.html || '').length
        return `a:${entry.key}:${artifact.type}:${size}`
      }
      const event = entry.event
      return `e:${event.event_id || entry.key}:${event.name}:${event.created_at}`
    })
    .join('|')
})

function agentById(agentId) {
  return im.agents.find((agent) => agent.agent_id === agentId)
}

function agentName(agentId) {
  return agentById(agentId)?.name || agentId || 'unknown'
}

function agentKind(agentId) {
  return agentById(agentId)?.metadata?.agent_kind || 'native'
}

function avatarText(value = '') {
  return (value || 'A').slice(0, 1).toUpperCase()
}

function agentAvatar(agentId) {
  return agentById(agentId)?.metadata?.avatar_url || ''
}

function messageAvatar(item) {
  if (item.sender_type === 'agent') return agentAvatar(item.sender_id)
  if (item.sender_type === 'user' && item.sender_id === auth.user?.user_id) return auth.user?.avatar_url || ''
  return ''
}

function traceAvatar(trace) {
  return agentAvatar(trace.actor_id)
}

function eventAvatar(event) {
  return agentAvatar(eventActorId(event))
}

function itemAvatar(item) {
  if (item.type === 'group') return avatarText(item.title)
  return avatarText(item.name)
}

function messageTitle(item) {
  if (item?.metadata?.display_title) return item.metadata.display_title
  if (item.sender_type === 'user') {
    return item.sender_id === auth.user?.user_id ? '你' : item.sender_id
  }
  if (item.sender_type === 'system') return '系统'
  return agentName(item.sender_id)
}

function messageClass(item) {
  return {
    mine: item.sender_type === 'user' && item.sender_id === auth.user?.user_id,
    system: item.sender_type === 'system',
  }
}

function shortId(value = '') {
  return value ? value.slice(0, 8) : '-'
}

function formatTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : ''
}

function latestMessageText(item) {
  const source = item.last_message || item
  const part = source.content_parts?.find((entry) => entry.text || entry.diff || entry.title || entry.name)
  return part?.text || part?.diff || part?.title || part?.name || item.prompt || item.final || '暂无内容'
}

function isTraceOpen(trace) {
  if (Object.prototype.hasOwnProperty.call(traceOpenOverrides.value, trace.key)) {
    return traceOpenOverrides.value[trace.key]
  }
  return !trace.resolved
}

function toggleTrace(trace) {
  const willOpen = !isTraceOpen(trace)
  traceOpenOverrides.value = {
    ...traceOpenOverrides.value,
    [trace.key]: willOpen,
  }
  if (willOpen) ensureTraceEvents(trace)
}

// 该 trace 是否含被历史回放剥离正文的事件（后端标记 truncated=true）。
function traceHasTruncated(trace) {
  return (trace?.events || []).some((event) => event?.truncated)
}

// 展开时按 run_id 拉取全量事件并缓存（幂等：loading/loaded 不重复拉）。scope 即 run_id。
async function ensureTraceEvents(trace) {
  const runId = trace?.scope
  if (!runId || runId === 'scope') return
  if (!traceHasTruncated(trace)) return
  const cached = runEventsCache.value[runId]
  if (cached && (cached.status === 'loading' || cached.status === 'loaded')) return
  runEventsCache.value = { ...runEventsCache.value, [runId]: { status: 'loading', byId: cached?.byId || null } }
  try {
    const items = await im.fetchRunEvents(runId)
    const byId = new Map()
    for (const event of items) {
      if (event?.event_id) byId.set(event.event_id, event)
    }
    runEventsCache.value = { ...runEventsCache.value, [runId]: { status: 'loaded', byId } }
  } catch {
    runEventsCache.value = { ...runEventsCache.value, [runId]: { status: 'error', byId: null } }
  }
}

// 渲染用事件：已拉到全量则按 event_id 把被剥离的事件换成完整正文，否则用轻量事件。
function traceEvents(trace) {
  const cached = runEventsCache.value[trace?.scope]
  if (!cached || cached.status !== 'loaded' || !cached.byId) return trace?.events || []
  return (trace?.events || []).map((event) => (event?.event_id && cached.byId.get(event.event_id)) || event)
}

function traceEventsLoading(trace) {
  return runEventsCache.value[trace?.scope]?.status === 'loading'
}

// 默认展开（运行中）或用户展开后仍含 truncated 事件的 trace，自动补拉一次全量。
// 用稳定字符串做依赖键，避免每来一个事件就触发（缓存保证拉取幂等）。
watch(
  () =>
    chatItems.value
      .filter((entry) => entry.kind === 'trace' && isTraceOpen(entry.trace) && traceHasTruncated(entry.trace))
      .map((entry) => entry.trace.scope)
      .join(','),
  () => {
    for (const entry of chatItems.value) {
      if (entry.kind === 'trace' && isTraceOpen(entry.trace) && traceHasTruncated(entry.trace)) {
        ensureTraceEvents(entry.trace)
      }
    }
  },
  { immediate: true },
)

function traceActorName(trace) {
  if (trace.actor_id === 'workflow') return '系统流程'
  if (trace.actor_id === 'system') return '运行过程'
  if (trace.actor_id === 'planner') return agentName(im.currentRoom?.metadata?.planner_agent_id || 'default_planner')
  return agentName(trace.actor_id)
}

function traceTitle(trace) {
  return trace.title || eventTitle(trace.latest_event || {})
}

function traceStatusText(trace) {
  return trace.resolved ? `已完成 · ${traceDuration(trace)}` : `运行中 · ${traceDuration(trace)}`
}

function traceCanStop(trace) {
  if (trace.resolved) return false
  if (im.currentRoom?.type === 'group') return Boolean(trace.scope && trace.scope !== 'scope')
  return Boolean(runningConversationMessage.value)
}

function confirmInterruptActive() {
  if (!canInterrupt.value) return
  const isGroup = im.currentRoom?.type === 'group'
  Modal.confirm({
    title: isGroup ? '中断群聊编排？' : '中断单聊回复？',
    content: isGroup
      ? '将取消当前正在运行的 PlanOrchestrator run，并把对应用户消息标记为 cancelled。'
      : '将取消当前 Agent 回复任务，并把对应用户消息标记为 cancelled。',
    okText: '中断运行',
    okType: 'danger',
    cancelText: '继续等待',
    async onOk() {
      if (isGroup) {
        await im.cancelActiveRun(runningGroupTask.value?.run_id)
      } else {
        await im.cancelActiveReply(runningConversationMessage.value?.message_id)
      }
      message.warning('已发送中断请求')
    },
  })
}

function confirmInterruptTrace(trace) {
  if (!traceCanStop(trace)) return
  const isGroup = im.currentRoom?.type === 'group'
  Modal.confirm({
    title: isGroup ? '中断该运行轨迹对应的 run？' : '中断该回复？',
    content: '中断后后续输出会停止写入，当前消息会标记为 cancelled。',
    okText: '中断',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      if (isGroup) {
        await im.cancelActiveRun(trace.scope)
      } else {
        await im.cancelActiveReply(runningConversationMessage.value?.message_id)
      }
      message.warning('已发送中断请求')
    },
  })
}

function sideItemTitle(item) {
  // 群聊与单聊侧栏都渲染对话对象，统一以对话标题为主。
  if (im.currentRoom?.type === 'group') return item.title || '群聊对话'
  return item.title || `${agentName(item.agent_id)} 对话`
}

function sideItemTime(item) {
  return item.updated_at || item.last_message?.created_at || item.created_at
}

async function openSideItem(item) {
  if (!item.conversation_id) return
  if (im.currentRoom?.type === 'group') {
    await im.selectGroupConversation(item.conversation_id)
  } else {
    await im.selectConversation(item.conversation_id)
  }
}

async function newConversation() {
  if (im.currentRoom?.type === 'group') {
    await im.createGroupConversation()
  } else {
    if (!im.currentAgentId) return
    await im.createConversation(im.currentAgentId)
  }
  await scrollToBottom()
}

// 群聊/单聊统一的对话激活判断：群聊比对 currentGroupConversation，单聊比对 currentConversation。
function isActiveConversation(item) {
  if (im.currentRoom?.type === 'group') {
    return item.conversation_id === im.currentGroupConversation?.conversation_id
  }
  return item.conversation_id === im.currentConversation?.conversation_id
}

async function createRoom() {
  if (!roomForm.member_agent_ids.length) {
    message.warning('请选择 agent')
    return
  }
  creatingRoom.value = true
  try {
    await im.createRoom({
      type: 'group',
      title: roomForm.title || 'Agent 群聊',
      member_agent_ids: [...roomForm.member_agent_ids],
      metadata: { source: 'IM_front' },
    })
    createOpen.value = false
    roomForm.title = ''
    roomForm.type = 'group'
    roomForm.member_agent_ids = []
    message.success('房间已创建')
  } finally {
    creatingRoom.value = false
  }
}

function resetAgentForm() {
  agentForm.name = ''
  agentForm.agent_kind = 'native'
  agentForm.agent_type = 'executor'
  agentForm.description = ''
  agentForm.role_prompt = ''
  agentForm.workdir = ''
  agentForm.permission_profile = 'human_confirm'
  agentForm.avatar_url = ''
  agentForm.tool_names = []
  agentForm.tool_fields = []
  agentForm.tags = []
}

// 创建/编辑共用同一弹窗与表单：editingAgentId 为空 = 创建，非空 = 编辑当前 Agent。
async function submitAgent() {
  if (isEditingAgent.value) await saveAgentEdit()
  else await createAgent()
}

async function createAgent() {
  if (!agentForm.name.trim()) {
    message.warning('请输入 Agent 名称')
    return
  }
  creatingAgent.value = true
  try {
    const agentKindValue = agentForm.agent_kind || 'native'
    const agentTypeValue = agentKindValue === 'native' ? agentForm.agent_type : 'executor'
    const useToolPicker = isNativeAgentForm.value && agentTypeValue === 'executor'
    // 后端会按模版为新 Agent 自动新建一份独立的上下文/记忆，前端不再选择或传 context_id。
    await im.createAgent({
      name: agentForm.name.trim(),
      agent_type: agentTypeValue,
      role_prompt: isNativeAgentForm.value ? agentForm.role_prompt : '',
      tool_names: useToolPicker ? [...agentForm.tool_names] : [],
      tool_fields: useToolPicker ? [...agentForm.tool_fields] : [],
      metadata: {
        agent_kind: agentKindValue,
        description: agentForm.description,
        capabilities: agentForm.description ? [agentForm.description] : [],
        tags: [...agentForm.tags],
        workdir: agentForm.workdir,
        permission_profile: agentForm.permission_profile || 'human_confirm',
        avatar_url: agentForm.avatar_url || '',
      },
    })
    agentCreateOpen.value = false
    resetAgentForm()
    message.success('Agent 已创建')
  } finally {
    creatingAgent.value = false
  }
}

// 仅 owner 且非默认 Agent 可编辑（与后端 PROTECTED + 归属校验对齐），避免给出注定 4xx 的入口。
function canEditAgent(agent) {
  if (!agent) return false
  if (['default_executor', 'default_planner'].includes(agent.agent_id)) return false
  const owner = agent.metadata?.owner_user_id || agent.metadata?.created_by || ''
  return !owner || owner === auth.user?.user_id
}

// 从该 Agent 绑定的上下文里反查已选工具：已创建 agent 的 available_tools.params 是
// {available_tools, available_fields} 字典；默认模版里 params 可能是参数名数组，按空选区处理。
function agentToolSelection(agent) {
  const ctx = (im.contexts || []).find((item) => item.context_id === agent?.context_id)
  const provider = (ctx?.provider_config || []).find((item) => item.provider_id === 'available_tools')
  const params = provider?.params
  if (params && !Array.isArray(params)) {
    return {
      tool_names: [...(params.available_tools || [])],
      tool_fields: [...(params.available_fields || [])],
    }
  }
  return { tool_names: [], tool_fields: [] }
}

async function openEditAgent(agent) {
  if (!agent || !canEditAgent(agent)) return
  if (!(im.contexts || []).length) await im.fetchContexts()
  const meta = agent.metadata || {}
  editingAgentId.value = agent.agent_id
  agentForm.name = agent.name || ''
  agentForm.agent_kind = meta.agent_kind || 'native'
  agentForm.agent_type = agent.agent_type || 'executor'
  agentForm.description = meta.description || ''
  agentForm.role_prompt = agent.role_prompt || ''
  agentForm.workdir = meta.workdir || ''
  agentForm.permission_profile = meta.permission_profile || 'human_confirm'
  agentForm.avatar_url = meta.avatar_url || ''
  agentForm.tags = [...(meta.tags || [])]
  const sel = agentToolSelection(agent)
  agentForm.tool_names = sel.tool_names
  agentForm.tool_fields = sel.tool_fields
  agentCreateTab.value = 'form'
  agentCreateOpen.value = true
  if (!(im.tools || []).length) im.fetchTools()
}

async function saveAgentEdit() {
  if (!agentForm.name.trim()) {
    message.warning('请输入 Agent 名称')
    return
  }
  creatingAgent.value = true
  try {
    const useToolPicker = isNativeAgentForm.value && agentForm.agent_type === 'executor'
    // agent_kind / agent_type 不参与编辑（会牵动上下文重置）；metadata 由后端与旧值合并。
    await im.updateAgent(editingAgentId.value, {
      name: agentForm.name.trim(),
      role_prompt: isNativeAgentForm.value ? agentForm.role_prompt : '',
      tool_names: useToolPicker ? [...agentForm.tool_names] : [],
      tool_fields: useToolPicker ? [...agentForm.tool_fields] : [],
      metadata: {
        description: agentForm.description,
        capabilities: agentForm.description ? [agentForm.description] : [],
        tags: [...agentForm.tags],
        workdir: agentForm.workdir,
        permission_profile: agentForm.permission_profile || 'human_confirm',
        avatar_url: agentForm.avatar_url || '',
      },
    })
    agentCreateOpen.value = false
    message.success('Agent 已更新')
  } finally {
    creatingAgent.value = false
  }
}

async function sendBuilderMessage() {
  const text = builderInput.value.trim()
  if (!text || builderSending.value) return
  builderMessages.value.push({ role: 'user', content: text })
  builderInput.value = ''
  builderSending.value = true
  try {
    const r = await im.builderChat({ messages: builderMessages.value, draft: builderDraft.value })
    builderMessages.value.push({ role: 'assistant', content: r.reply || '' })
    if (r.draft) builderDraft.value = r.draft
    builderReady.value = !!r.ready
  } catch (error) {
    message.error('创建助手暂时不可用')
    builderMessages.value.push({ role: 'assistant', content: '（助手调用失败，请稍后再试或改用表单创建）' })
  } finally {
    builderSending.value = false
  }
}

function applyDraftToForm() {
  const d = builderDraft.value || {}
  agentForm.name = d.name || ''
  agentForm.agent_kind = d.agent_kind || 'native'
  agentForm.agent_type = d.agent_type || 'executor'
  agentForm.description = d.description || ''
  agentForm.role_prompt = d.role_prompt || ''
  agentForm.workdir = d.workdir || ''
  agentForm.permission_profile = d.permission_profile || 'human_confirm'
  agentForm.tool_names = [...(d.tool_names || [])]
  agentForm.tool_fields = [...(d.tool_fields || [])]
  agentForm.tags = [...(d.tags || [])]
  if (!(im.tools || []).length) im.fetchTools()
  agentCreateTab.value = 'form'
  message.success('已填入表单，请复核后点击创建')
}

function confirmDeleteAgent(agent) {
  Modal.confirm({
    title: `删除 Agent「${agent.name}」？`,
    content: '将同步删除该 Agent 的单聊对话、相关消息、事件，并从 Agent 群中移除。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await im.deleteAgent(agent.agent_id)
      message.success('Agent 已删除')
    },
  })
}

function confirmDeleteConversation(item) {
  Modal.confirm({
    title: `删除对话「${item.title || '未命名对话'}」？`,
    content: '该对话的消息历史和运行事件会被硬删除。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await im.deleteConversation(item.conversation_id)
      message.success('对话已删除')
    },
  })
}

function confirmDeleteRoom(room) {
  Modal.confirm({
    title: `解散 Agent 群「${room.title}」？`,
    content: '群消息、IM 事件、关联 run 和 runtime events 会同步删除。',
    okText: '解散',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      await im.deleteRoom(room.room_id)
      message.success('Agent 群已解散')
    },
  })
}

async function saveGroupPlanner() {
  if (!im.currentRoom?.room_id) return
  savingPlanner.value = true
  try {
    await im.updateRoom(im.currentRoom.room_id, {
      metadata: {
        ...(im.currentRoom.metadata || {}),
        planner_agent_id: drawerPlannerId.value || 'default_planner',
      },
    })
    message.success('Planner 已更新')
  } finally {
    savingPlanner.value = false
  }
}

function handleInput(event) {
  const value = event.target.value
  composer.value = value
  const lastAt = value.lastIndexOf('@')
  mentionOpen.value = im.currentRoom?.type === 'group' && lastAt >= 0 && !value.slice(lastAt + 1).includes(' ')
}

function insertMention(agent) {
  const lastAt = composer.value.lastIndexOf('@')
  const before = lastAt >= 0 ? composer.value.slice(0, lastAt) : composer.value
  composer.value = `${before}@${agent.name} `
  if (!mentions.value.includes(agent.agent_id)) mentions.value.push(agent.agent_id)
  mentionOpen.value = false
  nextTick(() => composerRef.value?.focus?.())
}

async function send() {
  if (sending.value) return
  if (!im.currentRoom && !im.currentAgentId) {
    message.warning('请选择 agent 或群聊')
    return
  }
  if (!composer.value.trim() && !currentDraft.value.items.length) return
  if (chatFiles.busy.value) { message.warning('请等待上传完成，或重试／移除失败的附件'); return }
  const submittedDraft = currentDraft.value
  sending.value = true
  try {
    const userText = composer.value.trim()
    let text = userText
    const metadata = { client: 'IM_front', client_message_id: submittedDraft.requestId }
    if (selectionEditTarget.value) {
      const t = selectionEditTarget.value
      const snippet = String(t.selection?.text || '').slice(0, 2000)
      text =
        `请针对文件 ${t.file_path || '(当前文档)'} 中的下面这段选区进行修改：\n\n` +
        '```\n' + snippet + '\n```\n\n' +
        `修改要求：${userText}\n\n` +
        '请使用 diff_editor 工具的 propose_edit（基于完整文件内容）生成 Diff 供我确认。'
      metadata.file_edit = {
        file_path: t.file_path,
        edit_id: t.edit_id,
        artifact_type: t.artifact_type,
        selection: t.selection,
      }
    }
    const payload = {
      sender_type: 'user',
      content_parts: [...(text ? [{ type: 'text', text }] : []), ...submittedDraft.items.map(item => ({ type: 'file', file_id: item.file.file_id }))],
      mentions: [...mentions.value],
      metadata,
    }
    if (replyTarget.value?.message_id) payload.reply_to = replyTarget.value.message_id
    if (quoteTarget.value?.message_id) payload.quote_of = quoteTarget.value.message_id
    await im.sendMessage(
      payload,
      {
        auto_start: im.currentRoom?.type === 'group' ? dispatchOptions.auto_start : true,
        planner_agent_id: im.currentRoom?.metadata?.planner_agent_id || drawerPlannerId.value || 'default_planner',
        context_id: dispatchOptions.context_id,
        max_replan_rounds: dispatchOptions.max_replan_rounds,
        onConversationCreated: (conversationId) => {
          chatFiles.moveDraft(submittedDraft, `conversation:${conversationId}`)
        },
        onCommitted: () => {
          submittedDraft.requestId = crypto.randomUUID()
          submittedDraft.text = ''
          submittedDraft.items.splice(0)
          submittedDraft.mentions.splice(0)
          submittedDraft.reply = null
          submittedDraft.quote = null
          submittedDraft.selection = null
          // New conversations may now use a different draft object.
          if (currentDraft.value.items === submittedDraft.items) {
            currentDraft.value.text = ''
            currentDraft.value.reply = currentDraft.value.quote = currentDraft.value.selection = null
          }
          mentionOpen.value = false
        },
      },
    )
    await scrollToBottom()
    return true
  } catch {
    // HTTP interceptor displays the error; the submitted draft remains available.
    return false
  } finally {
    sending.value = false
  }
}

// 从展开弹窗发送：复用底层 send()，成功（composer 被清空）后关闭弹窗；失败则保留文本继续编辑。
function handleExpandedKeydown(event) {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault()
    sendFromExpanded()
  }
}

async function sendFromExpanded() {
  if (await send()) composerExpanded.value = false
}

// 展开弹窗打开后聚焦到大编辑区，光标落到文末便于继续输入。
watch(composerExpanded, (open) => {
  if (!open) return
  nextTick(() => expandedComposerRef.value?.focus?.({ cursor: 'end' }))
})

async function approveConfirmation(part, confirmationMessage) {
  const sourceMessageId = part.metadata?.source_message_id || part.metadata?.message_id
  const confirmationMessageId = part.metadata?.confirmation_message_id || confirmationMessage?.message_id
  if (!sourceMessageId || !confirmationMessageId) return
  await im.recordAction(confirmationMessageId, {
    action_type: 'approve',
    payload: part.metadata,
  })
  await im.dispatch(sourceMessageId, {
    approved: true,
    auto_start: true,
    planner_agent_id: im.currentRoom?.metadata?.planner_agent_id || drawerPlannerId.value || 'default_planner',
    context_id: dispatchOptions.context_id,
    max_replan_rounds: dispatchOptions.max_replan_rounds,
  })
  im.messages = im.messages.map((messageItem) => (
    messageItem.message_id === confirmationMessageId ? { ...messageItem, status: 'finished' } : messageItem
  ))
  await Promise.all([im.refreshMessages(), im.fetchTasks()])
  message.success('已批准并启动外部 Agent')
}

function onFileDeleted(fileId) {
  im.markFileDeleted(fileId)
  chatFiles.markDeleted(fileId)
}
watch(() => im.deletedFileIds, (ids) => { for (const id of ids) chatFiles.markDeleted(id) }, { deep: true })

function copyText(text) {
  navigator.clipboard?.writeText(text)
  message.success('已复制')
}

function messagePlainText(item) {
  if (!item) return ''
  return (item.content_parts || [])
    .map((part) => part.text || part.diff || part.title || part.description || part.name || part.url || part.metadata?.artifact?.content || '')
    .filter(Boolean)
    .join('\n')
    .trim()
}

function messageById(messageId) {
  if (!messageId) return null
  return im.messages.find((item) => item.message_id === messageId) || null
}

function quoteRefSummary(item) {
  const text = messagePlainText(item)
  return text.length > 80 ? `${text.slice(0, 80)}…` : (text || '（无文本内容）')
}

function isRegenerable(item) {
  if (im.currentRoom?.type === 'group') return item.sender_type === 'user' && ['sent', 'failed', 'cancelled'].includes(item.status)
  // agent 回复：随时可重新生成。
  if (item.sender_type === 'agent') return true
  // 用户消息：执行失败 / 被中断时没有 agent 回复可点，直接在用户消息上提供重试。
  return item.sender_type === 'user' && ['sent', 'failed', 'cancelled'].includes(item.status)
}

// 失败/取消的用户消息走「重试」，正常的 agent 回复走「重新生成」。
function regenerateLabel(item) {
  return item.sender_type === 'user' ? '重试' : '重新生成'
}

// 异常状态标签配色：失败标红、取消标橙，其余沿用默认。
function statusTagColor(status) {
  if (status === 'failed') return 'error'
  if (status === 'cancelled') return 'warning'
  return undefined
}

function artifactTypeLabel(artifact = {}) {
  return artifactTypeLabels[(artifact.type || 'message').toLowerCase()] || '产物'
}

function artifactTitle(artifact = {}) {
  return artifact.title || artifact.preview_title || artifact.file_path || artifact.url || artifactTypeLabel(artifact)
}

function artifactSummary(artifact = {}) {
  const text = artifact.content || artifact.alt || artifact.file_path || artifact.url || artifact.after || artifact.html || ''
  return text.length > 90 ? `${text.slice(0, 90)}…` : (text || '点击查看详情')
}

function openArtifactPreview(item) {
  const artifact = item?.artifact || item || {}
  previewMessage.value = {
    message_id: `artifact-${item?.event?.event_id || artifact.title || Date.now()}`,
    sender_type: 'system',
    sender_id: 'system',
    created_at: item?.created_at || item?.event?.created_at,
    content_parts: [{ type: 'artifact', metadata: { artifact } }],
    metadata: {
      display_title: `内联产物 · ${artifactTitle(artifact)}`,
      virtual_artifact: true,
    },
  }
}

// 内联产物卡片「放大」：复用全屏预览弹窗，把单个 artifact 包成虚拟消息全屏查看（保留应用/保存/历史等能力）。
function expandArtifact(artifact) {
  if (!artifact) return
  openArtifactPreview({ artifact })
}

function copyMessage(item) {
  copyText(messagePlainText(item))
}

// 群聊里 agent 执行者 / planner 的最终回复以 agent.final / planner.final 运行事件呈现，
// 原生 agent 无后端 message_id，无法做服务端的回复/引用/收藏，这里只提供纯前端的复制与展开预览。
function isFinalReplyEvent(event) {
  return event?.name === 'agent.final' || event?.name === 'planner.final'
}

function copyEvent(event) {
  copyText(eventContent(event, agentName))
}

function openEventPreview(event) {
  previewMessage.value = {
    message_id: `event-${event?.event_id || event?.created_at || Date.now()}`,
    sender_type: 'agent',
    sender_id: 'system',
    created_at: event?.created_at,
    content_parts: [{ type: 'text', text: eventContent(event, agentName) }],
    metadata: {
      display_title: `${eventActor(event, agentName)} · ${eventTitle(event)}`,
      virtual_event: true,
    },
  }
}

function setReplyTarget(item) {
  replyTarget.value = item
  nextTick(() => composerRef.value?.focus?.())
}

function setQuoteTarget(item) {
  quoteTarget.value = item
  nextTick(() => composerRef.value?.focus?.())
}

function onArtifactSelectionEdit(payload) {
  selectionEditTarget.value = payload
  nextTick(() => composerRef.value?.focus?.())
}

function clearSelectionEditTarget() {
  selectionEditTarget.value = null
}

// —— 危险命令人工确认（弹窗）——
const pendingConfirmation = computed(() => im.humanConfirmations[0] || null)
const resolvingConfirmation = ref(false)
async function resolveDangerCommand(approved) {
  const c = pendingConfirmation.value
  if (!c || resolvingConfirmation.value) return
  resolvingConfirmation.value = true
  try {
    await im.resolveHumanConfirmation(
      c.run_id,
      c.confirmation_id,
      approved,
      approved ? '用户允许执行' : '用户拒绝执行',
    )
  } catch {
    // 全局拦截器已提示
  } finally {
    resolvingConfirmation.value = false
  }
}

// —— 消息操作：编辑文件产物并回写原文件 ——
const editArtifactOpen = ref(false)
const editArtifactSaving = ref(false)
const editArtifactContent = ref('')
const editArtifactTarget = ref(null) // {agent_id, file_path, base_sha, title, format}
const editArtifactIsMarkdown = computed(() =>
  editArtifactTarget.value ? looksLikeMarkdownDoc(editArtifactTarget.value) : false,
)

function collectEditableArtifacts(entry) {
  const parts = entry?.message?.content_parts || []
  const out = []
  for (const p of parts) {
    if (p.type !== 'artifact') continue
    const a = p.metadata?.artifact || p
    const type = (a.type || '').toLowerCase()
    if (type !== 'document' && type !== 'diff') continue
    const filePath = a.file_path || a.metadata?.file_path || ''
    const agentId = a.metadata?.agent_id || ''
    if (!filePath || !agentId) continue
    out.push({
      agent_id: agentId,
      file_path: filePath,
      base_sha: a.metadata?.base_sha || '',
      title: a.title || filePath,
      format: a.format || '',
      content: type === 'diff' ? (a.after ?? '') : (a.content ?? ''),
    })
  }
  return out
}
function hasEditableArtifact(entry) {
  return collectEditableArtifacts(entry).length > 0
}
function openEditArtifact(entry) {
  const items = collectEditableArtifacts(entry)
  if (!items.length) return
  const target = items[0] // v1：一条消息含多个可编辑文件时取第一个
  editArtifactTarget.value = target
  editArtifactContent.value = target.content
  editArtifactOpen.value = true
}
async function saveEditArtifact() {
  const t = editArtifactTarget.value
  if (!t || editArtifactSaving.value) return
  editArtifactSaving.value = true
  try {
    const res = await imApi.saveArtifactFile({
      agent_id: t.agent_id,
      file_path: t.file_path,
      content: editArtifactContent.value,
      base_sha: t.base_sha || undefined,
    })
    message.success(res?.version ? `已回写原文件（v${res.version}）` : '已回写原文件')
    editArtifactOpen.value = false
  } catch {
    // 全局拦截器已提示
  } finally {
    editArtifactSaving.value = false
  }
}

function clearReplyTarget() {
  replyTarget.value = null
}

function clearQuoteTarget() {
  quoteTarget.value = null
}

function openPreview(item) {
  previewMessage.value = item
}

function closePreview() {
  previewMessage.value = null
}

function emitSidebarCollapseState(collapsed) {
  window.dispatchEvent(new CustomEvent('im-sidebar-collapse', {
    detail: { collapsed },
  }))
}

async function regenerateMessage(item) {
  const label = regenerateLabel(item)
  try {
    if (im.currentRoom?.type === 'group') {
      await im.dispatch(item.message_id, { auto_start: dispatchOptions.auto_start, planner_agent_id: im.currentRoom.metadata?.planner_agent_id || 'default_planner', ...dispatchOptions })
    } else await im.regenerateReply(item)
    message.success(item.sender_type === 'user' ? '已重新发起' : '已请求重新生成')
  } catch (error) {
    message.error(`${label}失败`)
  }
}

function favoriteForMessage(messageId) {
  return im.favorites.find((fav) => fav.source_message_id === messageId) || null
}

function isMessageFavorited(messageId) {
  return Boolean(favoriteForMessage(messageId))
}

async function favoriteMessageAction(item) {
  try {
    const existing = favoriteForMessage(item.message_id)
    if (existing) {
      await im.removeFavorite(existing.favorite_id)
      message.success('已取消收藏')
    } else {
      await im.favoriteMessage(item.message_id)
      message.success('已加入固定上下文')
    }
  } catch (error) {
    message.error('操作失败')
  }
}

function favoriteSummary(item) {
  const text = (item.content || '').trim()
  return text.length > 90 ? text.slice(0, 90) + '…' : (text || '（无文本内容）')
}

function sanitizeFilename(name) {
  const cleaned = (name || '').replace(/[\\/:*?"<>|]/g, '_').trim()
  return cleaned || 'artifact'
}

function collectMessageArtifacts(entry) {
  const out = []
  const parts = entry?.message?.content_parts || []
  for (const part of parts) {
    if (part.type === 'artifact') out.push(part.metadata?.artifact || part)
  }
  for (const item of entry?.run_artifacts || []) {
    if (item?.artifact) out.push(item.artifact)
  }
  return out
}

function hasDownloadableArtifacts(entry) {
  return collectMessageArtifacts(entry).length > 0
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function triggerUrlDownload(fileUrl, filename) {
  const anchor = document.createElement('a')
  anchor.href = fileUrl
  anchor.download = filename
  anchor.target = '_blank'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function artifactDownloadName(artifact) {
  const type = (artifact?.type || 'message').toLowerCase()
  const base = sanitizeFilename(artifact?.title || artifact?.file_path || type) || type
  const extByType = { document: artifact?.format || 'txt', message: 'txt', diff: 'diff', web: 'html', image: '' }
  const ext = extByType[type] ?? 'txt'
  if (!ext) return base
  return base.includes('.') ? base : base + '.' + ext
}

function downloadSingleArtifact(artifact) {
  const type = (artifact?.type || 'message').toLowerCase()
  if ((type === 'image' || type === 'web') && !artifact?.html && artifact?.url) {
    triggerUrlDownload(artifact.url, artifactDownloadName(artifact))
    return
  }
  let text = ''
  if (type === 'document' || type === 'message') text = artifact?.content || ''
  else if (type === 'diff') text = artifact?.after || artifact?.content || ''
  else if (type === 'web') text = artifact?.html || artifact?.url || ''
  else text = artifact?.content || artifact?.url || ''
  const blob = new Blob([text], { type: artifact?.mime_type || 'text/plain;charset=utf-8' })
  triggerBlobDownload(blob, artifactDownloadName(artifact))
}

async function downloadMessageArtifacts(entry) {
  const artifacts = collectMessageArtifacts(entry)
  if (!artifacts.length) return
  try {
    if (artifacts.length === 1) {
      downloadSingleArtifact(artifacts[0])
    } else {
      const blob = await im.bundleArtifacts(artifacts, '消息产物.zip')
      triggerBlobDownload(blob, '消息产物.zip')
    }
  } catch (error) {
    message.error('下载失败')
  }
}

async function downloadRunArtifacts(entry) {
  const artifacts = (entry?.run_artifacts || []).map((item) => item.artifact).filter(Boolean)
  if (!artifacts.length) return
  try {
    const blob = await im.bundleArtifacts(artifacts, '群聊产物.zip')
    triggerBlobDownload(blob, '群聊产物.zip')
  } catch (error) {
    message.error('打包下载失败')
  }
}

async function toggleSidePinned(item) {
  await im.toggleConversationPinned(item.conversation_id, !item.pinned)
}

async function toggleSideArchived(item) {
  await im.toggleConversationArchived(item.conversation_id, !item.archived)
}

async function handleAvatarUpload(file) {
  uploadingAvatar.value = true
  try {
    const url = await im.uploadAvatar(file)
    if (url) {
      agentForm.avatar_url = url
      message.success('头像已上传')
    }
  } catch (error) {
    message.error('头像上传失败')
  } finally {
    uploadingAvatar.value = false
  }
  return false
}

function toggleToolField(field, tools) {
  const names = tools.map((tool) => tool.name)
  const allSelected = names.every((name) => agentForm.tool_names.includes(name))
  if (allSelected) {
    agentForm.tool_names = agentForm.tool_names.filter((name) => !names.includes(name))
  } else {
    const next = new Set(agentForm.tool_names)
    names.forEach((name) => next.add(name))
    agentForm.tool_names = [...next]
  }
}

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

// 懒加载：滚动接近顶部时按页补拉更早聊天记录；前插后按内容高度差回补 scrollTop，保持视口不跳。
const OLDER_LOAD_THRESHOLD = 120
async function handleListScroll() {
  const el = listRef.value
  if (!el || el.scrollTop > OLDER_LOAD_THRESHOLD) return
  if (!im.hasMoreMessages || im.loadingOlderMessages) return
  const prevHeight = el.scrollHeight
  const prevTop = el.scrollTop
  const added = await im.loadOlderMessages()
  if (!added) return
  await nextTick()
  el.scrollTop = el.scrollHeight - prevHeight + prevTop
}

// watch(
//   () => chatScrollSignature.value,
//   () => scrollToBottom(),
// )

watch(
  () => agentForm.agent_kind,
  (value) => {
    if (value !== 'native') {
      agentForm.agent_type = 'executor'
      agentForm.role_prompt = ''
    }
  },
)

watch(
  () => im.currentRoom?.metadata?.planner_agent_id,
  (value) => {
    drawerPlannerId.value = value || 'default_planner'
  },
  { immediate: true },
)

watch(
  () => agentCreateOpen.value,
  (open) => {
    if (open && !(im.tools || []).length) im.fetchTools()
    if (!open) {
      agentCreateTab.value = 'form'
      builderMessages.value = []
      builderInput.value = ''
      builderDraft.value = null
      builderReady.value = false
      // 关闭即退出编辑态并清表单，避免编辑残留泄漏到下一次「创建」。
      if (editingAgentId.value) {
        resetAgentForm()
        editingAgentId.value = ''
      }
    }
  },
)

watch(
  () => sidebarCollapsed.value,
  (collapsed) => emitSidebarCollapseState(collapsed),
  { immediate: true },
)

watch(
  () => [im.currentConversation?.conversation_id, im.currentRoom?.room_id, im.currentGroupConversation?.conversation_id],
  () => {
    previewMessage.value = null
    replyTarget.value = null
    quoteTarget.value = null
    selectionEditTarget.value = null
    conversationQuery.value = ''
  },
)

// 切换对话（store 加载完最新窗口后自增 messagesEpoch）→ 回到底部展示最新几条聊天记录。
// 上滑加载更早记录走 loadOlderMessages，不动 epoch，因此不会被强制拉回底部。
watch(
  () => im.messagesEpoch,
  () => scrollToBottom(),
)

onMounted(async () => {
  await im.bootstrap()
  const defaultPlanner = im.plannerAgents.find((agent) => agent.agent_id === 'default_planner') || im.plannerAgents[0]
  if (defaultPlanner && !drawerPlannerId.value) drawerPlannerId.value = defaultPlanner.agent_id
  im.startActivityPolling()
  await scrollToBottom()
})

onUnmounted(() => {
  im.stopActivityPolling()
  emitSidebarCollapseState(false)
})
</script>

<template>
  <main class="im-workspace" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <aside class="im-sidebar">
      <div class="sidebar-head">
        <div>
          <h2>会话</h2>
          <span>{{ im.executorAgents.length }} agents · {{ im.groupRooms.length }} groups</span>
        </div>
        <div class="sidebar-create-actions">
          <a-tooltip title="创建 Agent">
            <a-button type="primary" shape="circle" @click="agentCreateOpen = true">
              <template #icon><UserAddOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="创建 Agent 群聊">
            <a-button shape="circle" @click="createOpen = true">
              <template #icon><PlusOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="收起侧栏">
            <a-button class="sidebar-toggle" shape="circle" @click="sidebarCollapsed = true">
              <template #icon><MenuFoldOutlined /></template>
            </a-button>
          </a-tooltip>
        </div>
      </div>

      <div class="sidebar-scroll">
        <section class="nav-section">
          <button class="section-title section-title-toggle" @click="agentsSectionOpen = !agentsSectionOpen">
            <span>Agents</span>
            <RightOutlined class="feed-group-chevron" :class="{ open: agentsSectionOpen }" />
          </button>
          <template v-if="agentsSectionOpen">
          <button
            v-for="agent in im.executorAgents"
            :key="agent.agent_id"
            class="nav-card"
            :class="{ active: im.currentAgentId === agent.agent_id && im.currentRoom?.type !== 'group' }"
            @click="im.selectAgent(agent.agent_id)"
          >
            <a-badge :count="im.unreadForAgent(agent.agent_id)" :overflow-count="99">
              <a-avatar :src="agent.metadata?.avatar_url">{{ itemAvatar(agent) }}</a-avatar>
            </a-badge>
            <span class="presence"></span>
            <div>
              <strong>{{ agent.name }}</strong>
              <small>{{ agentKind(agent.agent_id) }}</small>
              <div v-if="agent.metadata?.tags?.length" class="agent-tags">
                <span v-for="tag in agent.metadata.tags.slice(0, 2)" :key="tag" class="agent-tag">{{ tag }}</span>
              </div>
            </div>
            <div class="nav-actions" @click.stop>
              <a-tooltip title="编辑">
                <a-button
                  v-if="canEditAgent(agent)"
                  class="nav-edit"
                  type="text"
                  size="small"
                  @click.stop="openEditAgent(agent)"
                >
                  <template #icon><EditOutlined /></template>
                </a-button>
              </a-tooltip>
              <a-button
                v-if="!['default_executor', 'default_planner'].includes(agent.agent_id)"
                class="nav-delete"
                type="text"
                danger
                size="small"
                @click.stop="confirmDeleteAgent(agent)"
              >
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </div>
          </button>
          </template>
        </section>

        <section class="nav-section">
          <button class="section-title section-title-toggle" @click="groupsSectionOpen = !groupsSectionOpen">
            <span>Agent 群</span>
            <RightOutlined class="feed-group-chevron" :class="{ open: groupsSectionOpen }" />
          </button>
          <template v-if="groupsSectionOpen">
          <button
            v-for="room in im.groupRooms"
            :key="room.room_id"
            class="nav-card"
            :class="{ active: im.currentRoom?.room_id === room.room_id }"
            @click="im.selectGroupRoom(room.room_id)"
          >
            <a-badge :count="im.unreadForRoom(room.room_id)" :overflow-count="99">
              <a-avatar :src="room.avatar_url"><TeamOutlined /></a-avatar>
            </a-badge>
            <div>
              <strong>{{ room.title }}</strong>
              <small>{{ room.member_agent_ids.length }} members</small>
            </div>
            <a-button class="nav-delete" type="text" danger size="small" @click.stop="confirmDeleteRoom(room)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </button>
          </template>
        </section>

        <section class="side-feed">
          <div class="side-feed-head">
            <button class="section-title section-title-toggle" @click="feedSectionOpen = !feedSectionOpen">
              <span>对话列表</span>
              <RightOutlined class="feed-group-chevron" :class="{ open: feedSectionOpen }" />
            </button>
            <a-button
              v-if="im.currentRoom?.type === 'group' || im.currentAgentId"
              type="text"
              size="small"
              @click.stop="newConversation"
            >
              <template #icon><PlusOutlined /></template>
              新对话
            </a-button>
          </div>
          <template v-if="feedSectionOpen">
            <a-input
              v-model:value="conversationQuery"
              class="side-search"
              allow-clear
              size="small"
              placeholder="搜索对话标题或内容"
            >
              <template #prefix><SearchOutlined /></template>
            </a-input>

            <a-empty
              v-if="!conversationGroups.pinned.length && !conversationGroups.normal.length && !conversationGroups.archived.length"
              description="暂无记录"
            />

          <template v-if="conversationGroups.pinned.length">
            <div class="feed-group-title">
              <PushpinOutlined />
              <span>置顶</span>
            </div>
            <button
              v-for="item in conversationGroups.pinned"
              :key="item.conversation_id"
              class="feed-item"
              :class="{ active: isActiveConversation(item) }"
              @click="openSideItem(item)"
            >
              <strong>{{ sideItemTitle(item) }}</strong>
              <span>{{ latestMessageText(item) }}</span>
              <small>{{ formatTime(sideItemTime(item)) }}</small>
              <a-badge class="feed-unread" :count="im.unreadForConversation(item.conversation_id)" :overflow-count="99" />
              <div class="feed-actions" @click.stop>
                <a-dropdown :trigger="['click']" placement="bottomRight">
                  <a-button class="feed-menu" type="text" size="small">
                    <template #icon><EllipsisOutlined /></template>
                  </a-button>
                  <template #overlay>
                    <a-menu>
                      <a-menu-item @click="toggleSidePinned(item)">取消置顶</a-menu-item>
                      <a-menu-item @click="toggleSideArchived(item)">归档</a-menu-item>
                      <a-menu-item danger @click="confirmDeleteConversation(item)">删除</a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>
              </div>
            </button>
          </template>

          <template v-if="conversationGroups.normal.length">
            <div v-if="conversationGroups.pinned.length || conversationGroups.archived.length" class="feed-group-title">
              <MessageOutlined />
              <span>全部对话</span>
            </div>
            <button
              v-for="item in conversationGroups.normal"
              :key="item.conversation_id"
              class="feed-item"
              :class="{ active: isActiveConversation(item) }"
              @click="openSideItem(item)"
            >
              <strong>{{ sideItemTitle(item) }}</strong>
              <span>{{ latestMessageText(item) }}</span>
              <small>{{ formatTime(sideItemTime(item)) }}</small>
              <a-badge class="feed-unread" :count="im.unreadForConversation(item.conversation_id)" :overflow-count="99" />
              <div class="feed-actions" @click.stop>
                <a-dropdown :trigger="['click']" placement="bottomRight">
                  <a-button class="feed-menu" type="text" size="small">
                    <template #icon><EllipsisOutlined /></template>
                  </a-button>
                  <template #overlay>
                    <a-menu>
                      <a-menu-item @click="toggleSidePinned(item)">置顶</a-menu-item>
                      <a-menu-item @click="toggleSideArchived(item)">归档</a-menu-item>
                      <a-menu-item danger @click="confirmDeleteConversation(item)">删除</a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>
              </div>
            </button>
          </template>

          <template v-if="conversationGroups.archived.length">
            <button class="feed-group-title feed-group-toggle" @click="archivedOpen = !archivedOpen">
              <InboxOutlined />
              <span>已归档 ({{ conversationGroups.archived.length }})</span>
              <RightOutlined class="feed-group-chevron" :class="{ open: archivedOpen }" />
            </button>
            <template v-if="archivedOpen">
              <button
                v-for="item in conversationGroups.archived"
                :key="item.conversation_id"
                class="feed-item feed-item-archived"
                :class="{ active: isActiveConversation(item) }"
                @click="openSideItem(item)"
              >
                <strong>{{ sideItemTitle(item) }}</strong>
                <span>{{ latestMessageText(item) }}</span>
                <small>{{ formatTime(sideItemTime(item)) }}</small>
                <a-badge class="feed-unread" :count="im.unreadForConversation(item.conversation_id)" :overflow-count="99" />
                <div class="feed-actions" @click.stop>
                  <a-dropdown :trigger="['click']" placement="bottomRight">
                    <a-button class="feed-menu" type="text" size="small">
                      <template #icon><EllipsisOutlined /></template>
                    </a-button>
                    <template #overlay>
                      <a-menu>
                        <a-menu-item @click="toggleSideArchived(item)">取消归档</a-menu-item>
                        <a-menu-item danger @click="confirmDeleteConversation(item)">删除</a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </div>
              </button>
            </template>
          </template>
          </template>
        </section>
      </div>
    </aside>

    <section class="chat-main">
      <div v-if="sidebarCollapsed" class="chat-sticky-tools">
        <a-tooltip title="展开侧栏" placement="right">
          <a-button class="sidebar-expand" shape="circle" @click="sidebarCollapsed = false">
            <template #icon><MenuUnfoldOutlined /></template>
          </a-button>
        </a-tooltip>
      </div>
      <header class="chat-hero" @click="drawerOpen = true" :hidden="sidebarCollapsed">
        <div class="hero-title">
          <a-avatar :size="44" :src="heroAvatar">
            <TeamOutlined v-if="im.currentRoom?.type === 'group'" />
            <RobotOutlined v-else />
          </a-avatar>
          <div>
            <h1>{{ activeTitle }}</h1>
            <p>{{ activeSubtitle }}</p>
          </div>
        </div>
        <div class="hero-actions">
          <a-button
            v-if="canInterrupt"
            danger
            class="hero-stop"
            @click.stop="confirmInterruptActive"
          >
            <template #icon><StopOutlined /></template>
            中断运行
          </a-button>
          <!-- <a-space v-if="im.currentRoom?.type === 'group'" @click.stop>
            <a-switch v-model:checked="dispatchOptions.auto_start" />
            <span class="muted">自动启动</span>
          </a-space> -->
          <span class="hero-info-chip">
            <InfoCircleOutlined />
            查看信息
            <RightOutlined />
          </span>
        </div>
      </header>

      <div class="chat-body" style="padding-bottom: 200px;">
      <div ref="listRef" class="message-list" @scroll.passive="handleListScroll">
        <div v-if="im.loadingOlderMessages" class="history-loading">
          <LoadingOutlined spin />
          <span>正在加载更早的聊天记录…</span>
        </div>
        <div v-else-if="showHistoryStart" class="history-start">没有更早的聊天记录了</div>
        <a-empty v-if="!chatItems.length" description="暂无消息" />
        <template v-for="entry in chatItems" :key="entry.key">
          <article
            v-if="entry.kind === 'message'"
            class="message-row"
            :class="messageClass(entry.message)"
          >
            <a-avatar class="message-avatar" :src="messageAvatar(entry.message)">{{ avatarText(messageTitle(entry.message)) }}</a-avatar>
            <div class="message-bubble">
              <div
                v-if="messageById(entry.message.reply_to) || messageById(entry.message.quote_of)"
                class="message-quote-ref"
              >
                <span class="quote-ref-tag">{{ entry.message.reply_to ? '回复' : '引用' }}</span>
                <strong>{{ messageTitle(messageById(entry.message.reply_to || entry.message.quote_of)) }}</strong>
                <span class="quote-ref-text">{{ quoteRefSummary(messageById(entry.message.reply_to || entry.message.quote_of)) }}</span>
              </div>
              <div class="message-meta">
                <strong>{{ messageTitle(entry.message) }}</strong>
                <span>{{ formatTime(entry.message.created_at) }}</span>
                <a-tag v-if="entry.message.status !== 'sent'" size="small" :color="statusTagColor(entry.message.status)">{{ entry.message.status }}</a-tag>
                <a-tag v-if="entry.message.run_id && im.currentRoom?.type === 'group'" size="small" color="blue">
                  run {{ shortId(entry.message.run_id) }}
                </a-tag>
                <div class="message-actions">
                  <a-tooltip title="复制">
                    <a-button type="text" size="small" @click.stop="copyMessage(entry.message)">
                      <template #icon><CopyOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="回复">
                    <a-button type="text" size="small" @click.stop="setReplyTarget(entry.message)">
                      <template #icon><MessageOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="引用">
                    <a-button type="text" size="small" @click.stop="setQuoteTarget(entry.message)">
                      <template #icon><BranchesOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip v-if="isRegenerable(entry.message)" :title="regenerateLabel(entry.message)">
                    <a-button type="text" size="small" @click.stop="regenerateMessage(entry.message)">
                      <template #icon><ReloadOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="展开预览">
                    <a-button type="text" size="small" @click.stop="openPreview(entry.message)">
                      <template #icon><ExpandAltOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip v-if="hasEditableArtifact(entry)" title="编辑文件并回写">
                    <a-button type="text" size="small" @click.stop="openEditArtifact(entry)">
                      <template #icon><EditOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip v-if="hasDownloadableArtifacts(entry)" title="下载产物">
                    <a-button type="text" size="small" @click.stop="downloadMessageArtifacts(entry)">
                      <template #icon><DownloadOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip :title="isMessageFavorited(entry.message.message_id) ? '取消收藏' : '收藏到固定上下文'">
                    <a-button type="text" size="small" :class="{ 'is-favorited': isMessageFavorited(entry.message.message_id) }" @click.stop="favoriteMessageAction(entry.message)">
                      <template #icon>
                        <StarFilled v-if="isMessageFavorited(entry.message.message_id)" />
                        <StarOutlined v-else />
                      </template>
                    </a-button>
                  </a-tooltip>
                </div>
              </div>

              <div v-for="(part, index) in entry.message.content_parts" :key="`${entry.message.message_id}-${index}`" class="part">
                <!-- eslint-disable-next-line vue/no-v-html -->
                <div v-if="part.type === 'text'" class="text-part md-body" v-html="renderMarkdown(part.text)"></div>
                <pre v-else-if="part.type === 'code'" class="code-part"><code>{{ part.text }}</code></pre>
                <img v-else-if="part.type === 'image'" class="image-part" :src="part.url" :alt="part.name || 'image'" />
                <ChatFileCard v-else-if="part.type === 'file' && part.file_id" :part="part" @deleted="onFileDeleted" />
                <a-button v-else-if="part.type === 'file'" :href="part.url" target="_blank">
                  <template #icon><FileTextOutlined /></template>
                  {{ part.name || '文件' }}
                </a-button>
                <a :href="part.url" target="_blank" v-else-if="part.type === 'web_preview'" class="web-card">
                  <strong>{{ part.title || part.url }}</strong>
                  <span>{{ part.description }}</span>
                </a>
                <div v-else-if="part.type === 'diff'" class="diff-card">
                  <div class="card-title">
                    <BranchesOutlined />
                    <span>{{ part.title || 'Diff' }}</span>
                    <a-button size="small" type="text" @click="copyText(part.diff)">
                      <template #icon><CopyOutlined /></template>
                    </a-button>
                  </div>
                  <pre><code>{{ part.diff }}</code></pre>
                </div>
                <ArtifactCard
                  v-else-if="part.type === 'artifact'"
                  :artifact="part.metadata?.artifact || part"
                  @selection-edit="onArtifactSelectionEdit"
                  @expand="expandArtifact"
                />
                <div v-else-if="part.type === 'deploy'" class="deploy-card">
                  <div class="card-title">
                    <DeploymentUnitOutlined />
                    <span>{{ part.title || '操作卡片' }}</span>
                  </div>
                  <p>{{ part.description }}</p>
                  <a-space v-if="part.metadata?.message_id">
                    <a-button type="primary" size="small" @click="approveConfirmation(part, entry.message)">
                      <template #icon><CheckOutlined /></template>
                      批准
                    </a-button>
                    <a-tag color="orange">
                      <SafetyCertificateOutlined />
                      人工确认
                    </a-tag>
                  </a-space>
                </div>
                <a-tag v-else color="default">{{ part.type }}</a-tag>
              </div>

              <div v-if="entry.run_artifacts?.length" class="run-artifacts">
                <div class="run-artifacts-head">
                  <FileTextOutlined />
                  <strong>本次 run 内联产物</strong>
                  <a-tag size="small" color="purple">{{ entry.run_artifacts.length }}</a-tag>
                  <a-button type="text" size="small" class="run-artifacts-download" @click.stop="downloadRunArtifacts(entry)">
                    <template #icon><DownloadOutlined /></template>
                    打包下载
                  </a-button>
                </div>
                <div class="run-artifact-strip">
                  <button
                    v-for="artifactItem in entry.run_artifacts"
                    :key="artifactItem.key"
                    class="run-artifact-chip"
                    @click.stop="openArtifactPreview(artifactItem)"
                  >
                    <span class="run-artifact-type">{{ artifactTypeLabel(artifactItem.artifact) }}</span>
                    <strong>{{ artifactTitle(artifactItem.artifact) }}</strong>
                    <small>{{ artifactSummary(artifactItem.artifact) }}</small>
                  </button>
                </div>
              </div>
            </div>
          </article>

          <article v-else-if="entry.kind === 'trace'" class="trace-card" :class="{ open: isTraceOpen(entry.trace) }">
            <button class="trace-summary" @click="toggleTrace(entry.trace)">
              <span class="trace-rail"></span>
              <a-avatar class="trace-avatar" :src="traceAvatar(entry.trace)">{{ avatarText(traceActorName(entry.trace)) }}</a-avatar>
              <div class="trace-main">
                <strong>{{ traceActorName(entry.trace) }}</strong>
                <span>{{ traceTitle(entry.trace) }}</span>
              </div>
              <div class="trace-stats">
                <a-tag :color="eventColor(entry.trace.latest_event?.name)" size="small">
                  {{ entry.trace.event_count || entry.trace.events.length }} events
                </a-tag>
                <small>{{ traceStatusText(entry.trace) }}</small>
              </div>
              <a-button
                v-if="traceCanStop(entry.trace)"
                class="trace-stop"
                danger
                type="text"
                size="small"
                @click.stop="confirmInterruptTrace(entry.trace)"
              >
                <template #icon><StopOutlined /></template>
              </a-button>
              <RightOutlined class="trace-chevron" />
            </button>

            <div v-if="isTraceOpen(entry.trace)" class="trace-expanded">
              <div v-if="entry.trace.step?.result_observation" class="trace-observation">
                {{ entry.trace.step.result_observation }}
              </div>
              <div v-if="traceEventsLoading(entry.trace)" class="trace-loading">
                <LoadingOutlined spin /> 正在加载事件详情…
              </div>
              <div class="trace-timeline">
                <div
                  v-for="event in traceEvents(entry.trace)"
                  :key="event.event_id || `${event.name}-${event.created_at}`"
                  class="trace-node"
                  :class="eventTone(event.name)"
                >
                  <span class="trace-dot"></span>
                  <div class="trace-node-body">
                    <div class="trace-node-head">
                      <strong>{{ eventTitle(event) }}</strong>
                      <a-tag :color="eventColor(event.name)" size="small">{{ event.name }}</a-tag>
                      <span>{{ formatTime(event.created_at) }}</span>
                    </div>
                    <pre>{{ eventContent(event, agentName) }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </article>

          <article v-else-if="entry.kind === 'artifact'" class="message-row artifact-row">
            <a-avatar class="message-avatar" :src="eventAvatar(entry.event)">{{ avatarText(eventActor(entry.event, agentName)) }}</a-avatar>
            <div class="message-bubble artifact-bubble">
              <div class="message-meta">
                <strong>{{ eventActor(entry.event, agentName) }}</strong>
                <a-tag color="purple" size="small">内联产物</a-tag>
                <span>{{ formatTime(entry.event.created_at) }}</span>
              </div>
              <ArtifactCard :artifact="entry.artifact" @selection-edit="onArtifactSelectionEdit" @expand="expandArtifact" />
            </div>
          </article>
          
<!-- 
          <article v-else class="message-row event-row">
            <a-avatar class="message-avatar" :src="eventAvatar(entry.event)">{{ avatarText(eventActor(entry.event, agentName)) }}</a-avatar>
            <div class="message-bubble event-bubble" :class="eventTone(entry.event.name)">
              <div class="message-meta">
                <strong>{{ eventActor(entry.event, agentName) }}</strong>
                <a-tag :color="eventColor(entry.event.name)" size="small">{{ eventTitle(entry.event) }}</a-tag>
                <span>{{ formatTime(entry.event.created_at) }}</span>
                <div v-if="isFinalReplyEvent(entry.event)" class="message-actions">
                  <a-tooltip title="复制">
                    <a-button type="text" size="small" @click.stop="copyEvent(entry.event)">
                      <template #icon><CopyOutlined /></template>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="展开预览">
                    <a-button type="text" size="small" @click.stop="openEventPreview(entry.event)">
                      <template #icon><ExpandAltOutlined /></template>
                    </a-button>
                  </a-tooltip>
                </div>
              </div>
              <pre class="event-content">{{ eventContent(entry.event, agentName) }}</pre>
              <div v-if="entry.run_artifacts?.length" class="run-artifacts">
                <div class="run-artifacts-head">
                  <FileTextOutlined />
                  <strong>本次 run 内联产物</strong>
                  <a-tag size="small" color="purple">{{ entry.run_artifacts.length }}</a-tag>
                  <a-button type="text" size="small" class="run-artifacts-download" @click.stop="downloadRunArtifacts(entry)">
                    <template #icon><DownloadOutlined /></template>
                    打包下载
                  </a-button>
                </div>
                <div class="run-artifact-strip">
                  <button
                    v-for="artifactItem in entry.run_artifacts"
                    :key="artifactItem.key"
                    class="run-artifact-chip"
                    @click.stop="openArtifactPreview(artifactItem)"
                  >
                    <span class="run-artifact-type">{{ artifactTypeLabel(artifactItem.artifact) }}</span>
                    <strong>{{ artifactTitle(artifactItem.artifact) }}</strong>
                    <small>{{ artifactSummary(artifactItem.artifact) }}</small>
                  </button>
                </div>
              </div>
            </div>
          </article> -->
        </template>
      </div>

      </div>

      <footer class="composer">
        <ChatAttachments :items="currentDraft.items" :target-key="draftKey" :disabled="sending || (!im.currentAgentId && !im.currentRoom)"
          @upload="chatFiles.uploadFiles" @reuse="chatFiles.addExisting" @remove="chatFiles.removeItem" @retry="chatFiles.retry" />
        <div v-if="selectionEditTarget" class="composer-refs">
          <div class="composer-ref">
            <EditOutlined />
            <span class="composer-ref-label">针对选区修改 {{ selectionEditTarget.file_path || selectionEditTarget.title || '当前文档' }}：</span>
            <span class="composer-ref-text">{{ (selectionEditTarget.selection?.text || '').slice(0, 80) }}</span>
            <a-button type="text" size="small" @click="clearSelectionEditTarget">
              <template #icon><CloseOutlined /></template>
            </a-button>
          </div>
        </div>
        <div v-if="replyTarget || quoteTarget" class="composer-refs">
          <div v-if="replyTarget" class="composer-ref">
            <MessageOutlined />
            <span class="composer-ref-label">回复 @{{ messageTitle(replyTarget) }}：</span>
            <span class="composer-ref-text">{{ quoteRefSummary(replyTarget) }}</span>
            <a-button type="text" size="small" @click="clearReplyTarget">
              <template #icon><CloseOutlined /></template>
            </a-button>
          </div>
          <div v-if="quoteTarget" class="composer-ref">
            <BranchesOutlined />
            <span class="composer-ref-label">引用 @{{ messageTitle(quoteTarget) }}：</span>
            <span class="composer-ref-text">{{ quoteRefSummary(quoteTarget) }}</span>
            <a-button type="text" size="small" @click="clearQuoteTarget">
              <template #icon><CloseOutlined /></template>
            </a-button>
          </div>
        </div>
        <div class="mention-wrap">
          <div v-if="mentionCandidates.length" class="mention-popover">
            <button v-for="agent in mentionCandidates" :key="agent.agent_id" @click="insertMention(agent)">
              <a-avatar :size="24" :src="agentAvatar(agent.agent_id)">{{ avatarText(agent.name) }}</a-avatar>
              <span>{{ agent.name }}</span>
              <small>{{ agentKind(agent.agent_id) }}</small>
            </button>
          </div>
          <a-textarea
            ref="composerRef"
            :disabled="sending"
            :value="composer"
            :auto-size="{ minRows: 2, maxRows: 6 }"
            :placeholder="im.currentRoom?.type === 'group' ? '输入消息。输入 @ 可选择群内 agent' : '输入消息，当前会话历史会注入给这个 agent'"
            @input="handleInput"
            @pressEnter.ctrl.prevent="send"
          />
          <a-tooltip title="展开输入" placement="topRight">
            <a-button class="composer-expand-btn" type="text" size="small" @click="composerExpanded = true">
              <template #icon><ExpandAltOutlined /></template>
            </a-button>
          </a-tooltip>
        </div>
        <div class="composer-actions">
          <a-space v-if="im.currentRoom?.type === 'group'">
            <a-input-number v-model:value="dispatchOptions.max_replan_rounds" :min="0" :max="10" />
          </a-space>
          <a-button type="primary" :loading="sending" :disabled="chatFiles.busy.value" @click="send">
            <template #icon><SendOutlined /></template>
            发送
          </a-button>
        </div>
      </footer>

    </section>

    <a-modal
      :open="Boolean(previewMessage)"
      wrap-class-name="message-preview-modal"
      :footer="null"
      :closable="false"
      :width="'100vw'"
      centered
      destroy-on-close
      @cancel="closePreview"
    >
      <template v-if="previewMessage">
        <div class="preview-head">
          <div class="preview-title">
            <ExpandAltOutlined />
            <div>
              <strong>{{ messageTitle(previewMessage) }}</strong>
              <small>{{ formatTime(previewMessage.created_at) }}</small>
            </div>
          </div>
          <a-button type="text" size="small" @click="closePreview">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
        <div class="preview-body">
          <div
            v-for="(part, index) in previewMessage.content_parts"
            :key="`preview-${previewMessage.message_id}-${index}`"
            class="part"
          >
            <!-- eslint-disable-next-line vue/no-v-html -->
            <div v-if="part.type === 'text'" class="text-part md-body" v-html="renderMarkdown(part.text)"></div>
            <pre v-else-if="part.type === 'code'" class="code-part"><code>{{ part.text }}</code></pre>
            <img v-else-if="part.type === 'image'" class="image-part" :src="part.url" :alt="part.name || 'image'" />
            <ChatFileCard v-else-if="part.type === 'file' && part.file_id" :part="part" @deleted="onFileDeleted" />
            <a-button v-else-if="part.type === 'file'" :href="part.url" target="_blank">
              <template #icon><FileTextOutlined /></template>
              {{ part.name || '文件' }}
            </a-button>
            <a :href="part.url" target="_blank" v-else-if="part.type === 'web_preview'" class="web-card">
              <strong>{{ part.title || part.url }}</strong>
              <span>{{ part.description }}</span>
            </a>
            <div v-else-if="part.type === 'diff'" class="diff-card">
              <div class="card-title">
                <BranchesOutlined />
                <span>{{ part.title || 'Diff' }}</span>
                <a-button size="small" type="text" @click="copyText(part.diff)">
                  <template #icon><CopyOutlined /></template>
                </a-button>
              </div>
              <pre><code>{{ part.diff }}</code></pre>
            </div>
            <ArtifactCard
              v-else-if="part.type === 'artifact'"
              :artifact="part.metadata?.artifact || part"
              :expandable="false"
              @selection-edit="onArtifactSelectionEdit"
            />
            <div v-else-if="part.type === 'deploy'" class="deploy-card">
              <div class="card-title">
                <DeploymentUnitOutlined />
                <span>{{ part.title || '操作卡片' }}</span>
              </div>
              <p>{{ part.description }}</p>
            </div>
            <a-tag v-else color="default">{{ part.type }}</a-tag>
          </div>
        </div>
      </template>
    </a-modal>

    <a-modal v-model:open="createOpen" title="创建 Agent 群聊" :footer="null">
      <a-form layout="vertical">
        <a-form-item label="名称">
          <a-input v-model:value="roomForm.title" placeholder="留空则自动命名为 Agent 群聊" />
        </a-form-item>
        <a-form-item label="群成员 Agent">
          <a-select
            v-model:value="roomForm.member_agent_ids"
            mode="multiple"
            :options="roomAgentOptions"
            placeholder="选择 agent"
            show-search
          />
        </a-form-item>
        <a-button type="primary" html-type="submit" block :loading="creatingRoom" @click="createRoom">创建</a-button>
      </a-form>
    </a-modal>

    <a-modal v-model:open="agentCreateOpen" :title="isEditingAgent ? '编辑 Agent' : '创建 Agent'" :footer="null">
      <a-tabs v-model:activeKey="agentCreateTab">
        <a-tab-pane key="form" :tab="isEditingAgent ? '编辑表单' : '表单创建'">
      <a-form layout="vertical">
        <a-form-item label="头像">
          <div class="agent-avatar-upload">
            <a-avatar :size="56" :src="agentForm.avatar_url">{{ avatarText(agentForm.name) }}</a-avatar>
            <a-upload
              :show-upload-list="false"
              :before-upload="handleAvatarUpload"
              accept="image/*"
            >
              <a-button :loading="uploadingAvatar">
                <template #icon><CloudUploadOutlined /></template>
                上传头像
              </a-button>
            </a-upload>
            <a-button
              v-if="agentForm.avatar_url"
              type="text"
              size="small"
              @click="agentForm.avatar_url = ''"
            >
              清除
            </a-button>
          </div>
        </a-form-item>
        <a-form-item label="名称">
          <a-input v-model:value="agentForm.name" placeholder="例如：前端组件工程师" />
        </a-form-item>
        <a-form-item label="运行类型">
          <a-segmented
            v-model:value="agentForm.agent_kind"
            :options="agentKindOptions"
            :disabled="isEditingAgent"
          />
          <small v-if="isEditingAgent" class="form-lock-hint">编辑时不可更改运行类型</small>
        </a-form-item>
        <a-form-item v-if="isNativeAgentForm" label="类型">
          <a-segmented
            v-model:value="agentForm.agent_type"
            :options="[
              { label: 'Executor', value: 'executor' },
              { label: 'Planner', value: 'planner' },
            ]"
            :disabled="isEditingAgent"
          />
          <small v-if="isEditingAgent" class="form-lock-hint">编辑时不可更改类型</small>
        </a-form-item>
        <a-form-item label="工作目录">
          <a-input v-model:value="agentForm.workdir" placeholder="留空则使用后端默认工作目录" />
        </a-form-item>
        <a-form-item v-if="!isNativeAgentForm" label="权限策略">
          <a-select
            v-model:value="agentForm.permission_profile"
            :options="permissionProfileOptions"
          />
        </a-form-item>
        <a-form-item label="能力描述">
          <a-input v-model:value="agentForm.description" placeholder="擅长方向、可承担任务或工具范围" />
        </a-form-item>
        <a-form-item label="能力标签">
          <a-select v-model:value="agentForm.tags" mode="tags" :token-separators="[',', '，']" placeholder="1~2 个能力标签，如 前端、测试" />
        </a-form-item>
        <a-form-item v-if="isNativeAgentForm" label="Role Prompt">
          <a-textarea v-model:value="agentForm.role_prompt" :auto-size="{ minRows: 4, maxRows: 8 }" />
        </a-form-item>
        <a-form-item v-if="showToolPicker" label="可用工具">
          <a-empty v-if="!groupedTools.length" description="暂无可用工具" :image="false" />
          <a-collapse v-else class="tool-collapse" ghost>
            <a-collapse-panel
              v-for="group in groupedTools"
              :key="group.field"
              :header="`${group.label} (${group.tools.length})`"
            >
              <template #extra>
                <a-button
                  type="link"
                  size="small"
                  @click.stop="toggleToolField(group.field, group.tools)"
                >
                  全选/反选
                </a-button>
              </template>
              <a-checkbox-group v-model:value="agentForm.tool_names" class="tool-checkbox-group">
                <a-checkbox v-for="tool in group.tools" :key="tool.name" :value="tool.name">
                  <span class="tool-name">{{ tool.name }}</span>
                  <small v-if="tool.description" class="tool-desc">{{ tool.description }}</small>
                </a-checkbox>
              </a-checkbox-group>
            </a-collapse-panel>
          </a-collapse>
        </a-form-item>
        <a-button type="primary" html-type="submit" block :loading="creatingAgent" @click="submitAgent">
          {{ isEditingAgent ? '保存修改' : '创建 Agent' }}
        </a-button>
      </a-form>
        </a-tab-pane>
        <a-tab-pane v-if="!isEditingAgent" key="chat" tab="对话式创建">
          <div class="builder-chat">
            <div class="builder-messages">
              <a-empty
                v-if="!builderMessages.length"
                description="用一句话描述你想要的 Agent，我来引导你完成配置"
                :image="false"
              />
              <div
                v-for="(msg, index) in builderMessages"
                :key="`builder-${index}`"
                class="builder-msg"
                :class="msg.role"
              >
                <span class="builder-role">{{ msg.role === 'user' ? '你' : '助手' }}</span>
                <p>{{ msg.content }}</p>
              </div>
            </div>
            <div v-if="builderDraft" class="builder-draft">
              <div class="builder-draft-head">
                <strong>当前草稿</strong>
                <a-tag v-if="builderReady" color="green">可创建</a-tag>
                <a-tag v-else color="orange">完善中</a-tag>
              </div>
              <ul>
                <li>名称：{{ builderDraft.name || '（待定）' }}</li>
                <li>运行类型：{{ builderDraft.agent_kind }} · {{ builderDraft.agent_type }}</li>
                <li v-if="builderDraft.description">能力：{{ builderDraft.description }}</li>
                <li v-if="builderDraft.tags?.length">标签：{{ builderDraft.tags.join('、') }}</li>
                <li v-if="builderDraft.tool_names?.length">工具：{{ builderDraft.tool_names.join('、') }}</li>
              </ul>
              <a-button type="primary" block @click="applyDraftToForm">应用到表单</a-button>
            </div>
            <div class="builder-input">
              <a-textarea
                v-model:value="builderInput"
                :auto-size="{ minRows: 1, maxRows: 4 }"
                placeholder="例如：我想要一个会写 Python 单测的 native executor"
                @pressEnter.prevent="sendBuilderMessage"
              />
              <a-button type="primary" :loading="builderSending" @click="sendBuilderMessage">
                <template #icon><SendOutlined /></template>
              </a-button>
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-modal>

    <a-drawer v-model:open="drawerOpen" title="上下文信息" width="520">
      <section class="drawer-section">
        <h3>{{ im.currentRoom?.type === 'group' ? '群内 Agent' : 'Agent 信息' }}</h3>
        <div v-if="im.currentRoom?.type === 'group'" class="orchestrator-card">
          <strong>{{ im.currentRoom?.metadata?.orchestrator || 'PlanOrchestrator' }}</strong>
          <span>Planner · {{ agentName(im.currentRoom?.metadata?.planner_agent_id || 'default_planner') }}</span>
          <small>{{ currentMembers.length }} executor agents</small>
        </div>
        <div v-if="im.currentRoom?.type === 'group'" class="planner-config">
          <label>Plan Agent</label>
          <a-space-compact block>
            <a-select v-model:value="drawerPlannerId" class="drawer-planner-select">
              <a-select-option v-for="agent in im.plannerAgents" :key="agent.agent_id" :value="agent.agent_id">
                {{ agent.name }}
              </a-select-option>
            </a-select>
            <a-button type="primary" :loading="savingPlanner" @click="saveGroupPlanner">更换</a-button>
          </a-space-compact>
          <small>群聊 dispatch 会读取这里配置的 planner。</small>
        </div>
        <a-list :data-source="im.currentRoom?.type === 'group' ? currentMembers : [im.currentAgent].filter(Boolean)">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #avatar><a-avatar :src="item.metadata?.avatar_url">{{ avatarText(item.name) }}</a-avatar></template>
                <template #title>{{ item.name }}</template>
                <template #description>
                  {{ item.agent_id }} · {{ item.metadata?.agent_kind || 'native' }}
                </template>
              </a-list-item-meta>
              <template v-if="canEditAgent(item)" #actions>
                <a-button type="text" size="small" @click="drawerOpen = false; openEditAgent(item)">
                  <template #icon><EditOutlined /></template>
                  编辑
                </a-button>
              </template>
            </a-list-item>
          </template>
        </a-list>
      </section>

      <section class="drawer-section">
        <h3>本对话收藏的消息</h3>
        <p class="drawer-hint">收藏的内容会作为固定上下文注入给 Agent。</p>
        <a-empty v-if="!im.favorites.length" description="暂无收藏" />
        <a-list v-else :data-source="im.favorites">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>{{ item.title || '收藏消息' }}</template>
                <template #description>{{ favoriteSummary(item) }}</template>
              </a-list-item-meta>
              <template #actions>
                <a-tooltip :title="item.enabled === false ? '已停用，点击启用' : '生效中，点击停用'">
                  <a-switch size="small" :checked="item.enabled !== false" @change="(checked) => im.toggleFavoriteEnabled(item.favorite_id, checked)" />
                </a-tooltip>
                <a-button type="text" size="small" danger @click="im.removeFavorite(item.favorite_id)">
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </template>
            </a-list-item>
          </template>
        </a-list>
      </section>

      <section class="drawer-section">
        <h3>上传文件</h3>
        <a-empty v-if="!im.artifacts.length" description="暂无文件" />
        <a-list :data-source="im.artifacts">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #avatar><FileTextOutlined /></template>
                <template #title>{{ item.filename }}</template>
                <template #description>{{ item.content_type || 'file' }} · {{ item.size }} bytes</template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
      </section>

      <section v-if="im.currentRoom?.type === 'group'" class="drawer-section">
        <h3>最近任务</h3>
        <a-list :data-source="im.tasks.slice(0, 6)">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #avatar><CloudUploadOutlined /></template>
                <template #title>{{ item.status || item.mode }}</template>
                <template #description>{{ item.prompt }}</template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
      </section>
    </a-drawer>

    <!-- 危险命令人工确认 -->
    <a-modal
      :open="Boolean(pendingConfirmation)"
      title="危险命令需要确认"
      :closable="false"
      :mask-closable="false"
      :footer="null"
      :width="560"
    >
      <template v-if="pendingConfirmation">
        <p class="danger-confirm-tip">
          Agent 请求执行一条高危命令，确认后才会执行：
        </p>
        <pre class="danger-confirm-cmd">{{ pendingConfirmation.arguments?.command }}</pre>
        <div class="danger-confirm-actions">
          <a-button :loading="resolvingConfirmation" @click="resolveDangerCommand(false)">拒绝</a-button>
          <a-button type="primary" danger :loading="resolvingConfirmation" @click="resolveDangerCommand(true)">
            允许执行
          </a-button>
        </div>
      </template>
    </a-modal>

    <!-- 消息操作：编辑文件并回写原文件 -->
    <a-modal
      v-model:open="editArtifactOpen"
      :title="`编辑文件 · ${editArtifactTarget?.file_path || ''}`"
      :width="760"
      :confirm-loading="editArtifactSaving"
      ok-text="保存到原文件"
      cancel-text="取消"
      @ok="saveEditArtifact"
    >
      <a-textarea
        v-model:value="editArtifactContent"
        :auto-size="{ minRows: 12, maxRows: 24 }"
        class="edit-artifact-editor"
      />
      <p v-if="editArtifactIsMarkdown" class="edit-artifact-hint">提示：该文件为 Markdown，保存后在文档卡片中会按 Markdown 渲染。</p>
    </a-modal>

    <!-- 输入框展开：全屏编辑器（与底部输入框共享 composer） -->
    <a-modal
      v-model:open="composerExpanded"
      title="编辑消息"
      wrap-class-name="composer-expand-modal"
      :footer="null"
      destroy-on-close
    >
      <div v-if="selectionEditTarget || replyTarget || quoteTarget" class="composer-refs">
        <div v-if="selectionEditTarget" class="composer-ref">
          <EditOutlined />
          <span class="composer-ref-label">针对选区修改 {{ selectionEditTarget.file_path || selectionEditTarget.title || '当前文档' }}：</span>
          <span class="composer-ref-text">{{ (selectionEditTarget.selection?.text || '').slice(0, 80) }}</span>
          <a-button type="text" size="small" @click="clearSelectionEditTarget">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
        <div v-if="replyTarget" class="composer-ref">
          <MessageOutlined />
          <span class="composer-ref-label">回复 @{{ messageTitle(replyTarget) }}：</span>
          <span class="composer-ref-text">{{ quoteRefSummary(replyTarget) }}</span>
          <a-button type="text" size="small" @click="clearReplyTarget">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
        <div v-if="quoteTarget" class="composer-ref">
          <BranchesOutlined />
          <span class="composer-ref-label">引用 @{{ messageTitle(quoteTarget) }}：</span>
          <span class="composer-ref-text">{{ quoteRefSummary(quoteTarget) }}</span>
          <a-button type="text" size="small" @click="clearQuoteTarget">
            <template #icon><CloseOutlined /></template>
          </a-button>
        </div>
      </div>
      <a-textarea
        ref="expandedComposerRef"
        :disabled="sending"
        v-model:value="composer"
        class="composer-expand-editor"
        :placeholder="im.currentRoom?.type === 'group' ? '输入消息。输入 @ 可选择群内 agent' : '输入消息，当前会话历史会注入给这个 agent'"
        @keydown="handleExpandedKeydown"
      />
      <ChatAttachments :items="currentDraft.items" :target-key="draftKey" :disabled="sending || (!im.currentAgentId && !im.currentRoom)"
          @upload="chatFiles.uploadFiles" @reuse="chatFiles.addExisting" @remove="chatFiles.removeItem" @retry="chatFiles.retry" />
      <div class="composer-expand-foot">
        <span class="composer-expand-hint">Ctrl / ⌘ + Enter 发送</span>
        <a-space>
          <a-button @click="composerExpanded = false">取消</a-button>
          <a-button type="primary" :loading="sending" :disabled="chatFiles.busy.value" @click="sendFromExpanded">
            <template #icon><SendOutlined /></template>
            发送
          </a-button>
        </a-space>
      </div>
    </a-modal>
  </main>
</template>

<style scoped>
.danger-confirm-tip {
  margin: 0 0 8px;
  color: #6b7280;
  font-size: 13px;
}
.danger-confirm-cmd {
  margin: 0 0 16px;
  padding: 12px 14px;
  background: rgba(239, 68, 68, 0.06);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow: auto;
}
.danger-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.edit-artifact-editor :deep(textarea) {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.6;
}
.edit-artifact-hint {
  margin: 8px 0 0;
  color: #6b7280;
  font-size: 12px;
}
/* 文本气泡现在渲染 markdown（块级结构），去掉 .text-part 的 white-space: pre-wrap，
   否则 markdown-it 在块之间/结尾输出的换行符会被当作真实空行渲染，导致一句话下方多出空行。
   复合选择器 .text-part.md-body 用于确定性地压过全局 .text-part 规则。 */
.text-part.md-body {
  white-space: normal;
}
.message-actions .is-favorited {
  color: #faad14;
}

.agent-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}

.agent-tag {
  font-size: 11px;
  line-height: 1.4;
  padding: 0 6px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.12);
  color: #4f46e5;
  white-space: nowrap;
}

.drawer-hint {
  color: #8c8c8c;
  font-size: 12px;
  margin: -4px 0 8px;
}

.form-lock-hint {
  display: block;
  margin-top: 4px;
  color: #9ca3af;
  font-size: 12px;
}

.run-artifacts-head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.run-artifacts-download {
  margin-left: auto;
}

/* 会话列表未读红点：绝对定位到右上角，不占用 grid 行、不影响既有布局；
   悬停菜单也在右上角，留出间距避免遮挡。 */
.feed-unread {
  position: absolute;
  top: 8px;
  right: 30px;
  pointer-events: none;
}

/* 懒加载历史记录的顶部提示：加载中 / 已到最早。 */
.history-loading,
.history-start {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 0;
  color: #9ca3af;
  font-size: 12px;
}
</style>
