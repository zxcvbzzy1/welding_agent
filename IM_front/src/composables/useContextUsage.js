import { onUnmounted, ref, watch } from 'vue'
import { imApi } from '@/api/im'
import { mergeContextUsage } from '@/utils/contextUsage'

export function useContextUsage(im, run) {
  const items = ref([])
  const loading = ref(false)
  let revision = 0
  let disposed = false
  let scope = ''
  function selection() {
    return `${im.currentRoom?.room_id || ''}:${im.currentGroupConversation?.conversation_id || ''}:${im.currentConversation?.conversation_id || ''}:${run.value?.runId || ''}`
  }
  function applyEvents(values) {
    for (const event of run.value?.events || []) {
      if (event.name?.startsWith('context.')) values = mergeContextUsage(values, event)
    }
    return values
  }
  async function refresh() {
    const request = ++revision
    const selected = selection()
    const runId = run.value?.runId
    if (!runId) { items.value = []; loading.value = false; return }
    loading.value = true
    try {
      const result = await imApi.contextUsage(runId)
      if (!disposed && request === revision && selected === selection()) {
        items.value = applyEvents(result.items || [])
      }
    } finally {
      if (request === revision) loading.value = false
    }
  }
  watch(selection, value => {
    if (value !== scope) { scope = value; items.value = [] }
    refresh().catch(() => {})
  }, { immediate: true, flush: 'sync' })
  watch(() => im.streamReadyRevision, () => refresh().catch(() => {}))
  watch(() => run.value?.events.filter(e => ['workflow.finished', 'workflow.failed', 'run.cancelled', 'agent.reply.finished'].includes(e.name)).at(-1)?.event_id,
    () => refresh().catch(() => {}))
  watch(() => run.value?.events.filter(e => e.name?.startsWith('context.')).at(-1)?.event_id,
    () => { items.value = applyEvents(items.value) })
  onUnmounted(() => { disposed = true; revision++ })
  return { items, loading, refresh }
}
