import { computed, onUnmounted, ref, watch } from 'vue'
import { imApi } from '@/api/im'
import { buildComposerPlan, selectComposerRun } from '@/utils/composerPlan'
import { PlanEventLoader } from '@/utils/planEventLoader'

export function useComposerPlan(im) {
  const revision = ref(0)
  const loader = new PlanEventLoader({ api: imApi, changed: () => { revision.value++ } })
  const conversationId = computed(() => im.currentRoom?.type === 'group'
    ? im.currentGroupConversation?.conversation_id : im.currentConversation?.conversation_id)
  const run = computed(() => selectComposerRun({
    conversationId: conversationId.value, messages: im.messages, tasks: im.tasks, events: im.events,
  }))
  // Full instructions are in task snapshots; scope details carry compact steps.
  // Refresh on plan changes, coalescing parallel step completions into one request.
  let refreshTimer
  let refreshing = false
  let refreshAgain = false
  let disposed = false
  const snapshotError = ref(false)
  async function refreshSnapshot() {
    if (disposed || im.currentRoom?.type !== 'group') return
    if (refreshing) { refreshAgain = true; return }
    refreshing = true
    const selected = conversationId.value
    try {
      await im.fetchTasks()
      if (selected === conversationId.value) snapshotError.value = false
    } catch {
      if (selected === conversationId.value) snapshotError.value = true
    } finally {
      refreshing = false
      if (refreshAgain) { refreshAgain = false; scheduleSnapshot() }
    }
  }
  function scheduleSnapshot() {
    clearTimeout(refreshTimer)
    if (!disposed) refreshTimer = setTimeout(refreshSnapshot, 150)
  }
  watch(() => run.value?.events.filter(event => [
    'plan.generated', 'plan.replanned', 'plan.step.observed', 'plan.step.failed', 'plan.wave.completed',
  ].includes(event.name)).at(-1)?.event_id, () => scheduleSnapshot())
  watch(() => `${conversationId.value || ''}:${run.value?.runId || ''}`, () => {
    loader.clear()
    snapshotError.value = false
    revision.value++
  }, { flush: 'sync' })
  watch(() => run.value?.events, events => loader.load(events || []), { immediate: true })
  const plan = computed(() => {
    void revision.value
    const value = buildComposerPlan(run.value, loader.details)
    return value ? { ...value, loading: loader.pending.size > 0, error: loader.errors.size > 0 || snapshotError.value } : null
  })
  onUnmounted(() => { disposed = true; clearTimeout(refreshTimer); loader.clear() })
  return { run, plan, retry: () => { loader.load(run.value?.events || [], true); refreshSnapshot() } }
}
