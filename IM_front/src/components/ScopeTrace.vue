<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { LoadingOutlined, RightOutlined, StopOutlined } from '@ant-design/icons-vue'
import { eventTitle, eventContent, eventColor, eventTone } from '@/utils/runtimeEvents'
import { scopeTraceView } from '@/utils/scopeTraces'
import { gatewayCallLabel } from '@/utils/gatewayStatus'
const props = defineProps({
  trace: Object, client: Object, revision: Number,
  agentName: { type: Function, default: value => value || '智能体' },
  agentAvatar: { type: Function, default: () => '' },
  canStop: Boolean,
  nested: Boolean,
  gatewayCalls: { type: Array, default: () => [] },
})
defineEmits(['stop'])
const open = ref(false)
const opened = ref(new Set())
const view = computed(() => scopeTraceView(props.trace))
const state = computed(() => {
  void props.revision
  return view.value.query ? props.client.state(view.value.query) : { items: [] }
})
const actorName = computed(() => view.value.execution
  ? view.value.execution.agent_name || props.agentName(view.value.execution.agent_id) : '系统流程')
const latest = computed(() => view.value.events.at(-1))
const activeCalls = computed(() => props.gatewayCalls.filter(call =>
  call.run_id === props.trace.run_id
  && (view.value.children.length
    || (call.execution_id || '') === (view.value.execution?.execution_id || '')),
))
const title = computed(() => activeCalls.value.length
  ? activeCalls.value.map(call => {
    const label = gatewayCallLabel(call)
    return call.agent_id === view.value.execution?.agent_id
      ? label : `${call.agent_name || props.agentName(call.agent_id)} · ${label}`
  }).join('；')
  : view.value.children.length
  ? `流程轨迹 · ${view.value.children.length} 次智能体调用`
  : latest.value ? eventTitle({ ...latest.value, agent_name: '' }) : '运行轨迹')
const rows = computed(() => [
  ...view.value.events.map(event => ({ key: `scope:${event.event_id}`, event, created_at: event.created_at })),
  ...state.value.items.map(event => ({ key: `run:${event.event_id}`, event, created_at: event.created_at })),
  ...view.value.children.map(trace => ({ key: trace.key, trace, created_at: trace.created_at })),
].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) || a.key.localeCompare(b.key)))
const time = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleTimeString() : ''
function body(event) { void props.revision; return props.client.body(event) }
let activeQuery = null
watch([open, () => view.value.query?.event_id], () => {
  if (activeQuery) props.client.collapse(activeQuery)
  activeQuery = open.value ? view.value.query : null
  if (activeQuery) void props.client.expand(activeQuery)
})
async function toggleEvent(event) {
  const next = new Set(opened.value)
  if (next.has(event.event_id)) next.delete(event.event_id)
  else next.add(event.event_id)
  opened.value = next
  if (next.has(event.event_id)) await props.client.detail(event)
}
onUnmounted(() => { if (activeQuery) props.client.collapse(activeQuery) })
</script>

<template>
  <article class="trace-card" :class="{ open, 'trace-nested': nested }" :data-trace-key="trace.key">
    <div class="trace-summary-wrap">
      <button class="trace-summary" :aria-expanded="open" @click="open = !open">
        <span class="trace-rail"></span>
        <a-avatar class="trace-avatar" :src="agentAvatar(view.execution?.agent_id)">{{ actorName.slice(0, 1).toUpperCase() }}</a-avatar>
        <div class="trace-main">
          <strong>{{ actorName }}</strong>
          <span role="status" aria-live="polite" :title="activeCalls.map(call => call.reason).filter(Boolean).join('；')">{{ title }}</span>
        </div>
        <div class="trace-stats">
          <a-tag :color="eventColor(latest?.name)" size="small">{{ trace.event_count || view.events.length }} 个业务事件</a-tag>
          <small>{{ time(trace.created_at) }}</small>
        </div>
        <span aria-hidden="true"></span>
        <RightOutlined class="trace-chevron" />
      </button>
      <a-button v-if="canStop" class="trace-stop" danger type="text" size="small" aria-label="中断运行" @click="$emit('stop')">
        <template #icon><StopOutlined /></template>
      </a-button>
    </div>

    <div v-if="open" class="trace-expanded">
      <div v-if="state.loading" class="trace-loading" role="status"><LoadingOutlined spin /> 正在加载事件名称…</div>
      <a-button v-if="state.streamError" type="link" size="small" @click="client.retryStream(view.query)">实时连接已断开，重新连接</a-button>
      <a-button v-if="state.error" type="link" size="small" @click="client.load(view.query)">加载失败，重试</a-button>
      <div class="trace-timeline">
        <div v-for="row in rows" :key="row.key" class="trace-node" :class="eventTone(row.event?.name)">
          <span class="trace-dot"></span>
          <ScopeTrace v-if="row.trace" :trace="row.trace" :client="client" :revision="revision"
            :agent-name="agentName" :agent-avatar="agentAvatar" :gateway-calls="gatewayCalls" nested />
          <div v-else class="trace-node-body">
            <button class="trace-node-head trace-event-toggle" :aria-expanded="opened.has(row.event.event_id)" @click="toggleEvent(row.event)">
              <strong>{{ eventTitle(row.event) }}</strong>
              <a-tag :color="eventColor(row.event.name)" size="small">{{ row.event.category === 'scope' ? '业务事件' : row.event.name }}</a-tag>
              <span>{{ time(row.event.created_at) }}</span>
              <RightOutlined class="trace-event-chevron" :class="{ expanded: opened.has(row.event.event_id) }" />
            </button>
            <template v-if="opened.has(row.event.event_id)">
              <div v-if="body(row.event).loading" class="trace-loading" role="status"><LoadingOutlined spin /> 正在加载内容…</div>
              <a-button v-else-if="body(row.event).error" type="link" size="small" @click="client.detail(row.event)">加载失败，重试</a-button>
              <pre v-else>{{ eventContent(body(row.event).item || row.event) }}</pre>
            </template>
          </div>
        </div>
      </div>
      <div v-if="!state.loading && !state.error && !rows.length" class="trace-loading">暂无事件</div>
      <a-button v-if="state.more" type="link" size="small" :loading="state.loading" @click="client.more(view.query)">加载更多</a-button>
    </div>
  </article>
</template>

<style scoped>
/* Reuse the original card, avatar and timeline visuals at both levels. */
.trace-nested { width: 100%; min-width: 0; margin: 0; }
.trace-summary-wrap { position: relative; }
.trace-stop { position: absolute; right: 40px; top: 50%; transform: translateY(-50%); }
.trace-event-toggle { width: 100%; padding: 0; border: 0; background: transparent; text-align: left; cursor: pointer; font: inherit; }
.trace-event-toggle[aria-expanded="false"] { margin-bottom: 0; }
.trace-event-chevron { margin-left: auto; transition: transform 0.18s ease; }
.trace-event-chevron.expanded { transform: rotate(90deg); }
@media (max-width: 760px) {
  .trace-stop { right: 38px; top: 27px; }
}
</style>
