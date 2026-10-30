<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { PieChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import { contextModuleNames, preferredContextAgent } from '@/utils/contextUsage'

use([PieChart, BarChart, GridComponent, TooltipComponent, SVGRenderer])
const props = defineProps({ items: { type: Array, default: () => [] }, scopeKey: String, loading: Boolean })
const emit = defineEmits(['refresh'])
const open = ref(false)
const selected = ref('')
const explicitlySelected = ref(false)
const pieEl = ref(null)
const barEl = ref(null)
let pie, bar, observer
const colors = ['#6366f1', '#38bdf8', '#34d399', '#fbbf24', '#fb7185', '#a78bfa', '#2dd4bf', '#f97316', '#94a3b8']
const snapshot = computed(() => props.items.find(item => item.agent_id === selected.value))
const hasUsage = computed(() => Number.isFinite(snapshot.value?.input_tokens))
const total = computed(() => snapshot.value?.input_tokens || 0)
const capacity = computed(() => snapshot.value?.context_window || 0)
const percent = computed(() => capacity.value ? total.value / capacity.value * 100 : 0)
const compacting = computed(() => snapshot.value?.compaction?.status === 'started')
const modules = computed(() => (snapshot.value?.components || []).filter(item => item.tokens > 0).map((item, index) => ({
  ...item, label: contextModuleNames[item.name] || item.name, color: colors[index % colors.length],
})))
const number = value => new Intl.NumberFormat('zh-CN').format(value || 0)
const tooltip = computed(() => compacting.value ? '正在压缩上下文' : hasUsage.value
  ? `上下文约 ${number(total.value)} / ${capacity.value ? number(capacity.value) : '未知容量'} tokens` : '上下文：暂无统计')
watch(() => props.items, values => { selected.value = preferredContextAgent(values, explicitlySelected.value ? selected.value : '') }, { immediate: true })
watch(() => props.scopeKey, () => { explicitlySelected.value = false; selected.value = preferredContextAgent(props.items); open.value = false })
function renderPie() {
  if (!pieEl.value?.clientWidth || !pieEl.value?.clientHeight) return
  pie ||= init(pieEl.value, null, { renderer: 'svg' })
  const used = hasUsage.value ? Math.min(total.value, capacity.value || total.value) : 0
  const remaining = hasUsage.value ? Math.max(0, (capacity.value || total.value) - used) : 1
  pie.setOption({ animation: false, series: [{ type: 'pie', radius: ['58%', '90%'], silent: true,
    label: { show: false }, data: [
      { value: used, itemStyle: { color: percent.value >= 90 ? '#f97316' : '#6366f1' } },
      { value: remaining, itemStyle: { color: '#e5e7eb' } },
    ] }] })
}
async function renderBar() {
  await nextTick()
  if (!open.value || !barEl.value) return
  observer?.observe(barEl.value)
  if (!barEl.value.clientWidth || !barEl.value.clientHeight) return
  bar ||= init(barEl.value, null, { renderer: 'svg' })
  bar.setOption({ animation: false, grid: { left: 0, right: 0, top: 4, bottom: 4 },
    tooltip: { trigger: 'item', confine: true, renderMode: 'richText', valueFormatter: value => `${number(value)} tokens` },
    xAxis: { type: 'value', show: false, max: total.value || 1 }, yAxis: { type: 'category', show: false, data: ['输入'] },
    series: modules.value.map(item => ({ name: item.label, type: 'bar', stack: 'input', barWidth: 22,
      data: [item.tokens], itemStyle: { color: item.color } })),
  }, true)
  bar.resize()
}
watch([snapshot, modules], () => { renderPie(); renderBar() }, { deep: true })
watch(barEl, element => { if (element && open.value) renderBar() }, { flush: 'post' })
watch(open, async value => {
  if (value) { emit('refresh'); await renderBar() }
  else { if (barEl.value) observer?.unobserve(barEl.value); bar?.dispose(); bar = null }
})
onMounted(() => {
  renderPie()
  observer = new ResizeObserver(() => {
    if (pie) pie.resize(); else renderPie()
    if (bar) bar.resize(); else if (open.value) renderBar()
  })
  observer.observe(pieEl.value)
})
onBeforeUnmount(() => { observer?.disconnect(); pie?.dispose(); bar?.dispose() })
</script>

<template>
  <a-popover v-model:open="open" trigger="click" placement="topRight" overlay-class-name="context-usage-popover">
    <template #content>
      <section class="context-usage-panel" aria-label="上下文用量详情">
        <header><strong>上下文用量</strong><span class="estimate">估算值</span></header>
        <a-select v-if="items.length > 1" v-model:value="selected" class="agent-select" @change="explicitlySelected = true"
          :options="items.map(item => ({ value: item.agent_id, label: `${item.agent_type === 'planner' ? 'Planner · ' : ''}${item.agent_name || item.agent_id}` }))" />
        <template v-if="hasUsage">
          <div class="usage-total"><b>{{ number(total) }}</b> <span>/ {{ capacity ? number(capacity) : '未知' }} tokens</span>
            <em v-if="capacity">{{ percent.toFixed(1) }}%</em></div>
          <div ref="barEl" class="context-bar" role="img" aria-label="各上下文模块 token 占比" />
          <div class="module-list">
            <div v-for="item in modules" :key="item.id" class="module-row">
              <i :style="{ background: item.color }" /><span>{{ item.label }}</span>
              <b>{{ number(item.tokens) }}</b><small>{{ (item.tokens / (total || 1) * 100).toFixed(1) }}%</small>
            </div>
          </div>
          <footer>{{ snapshot.model }} · {{ snapshot.phase }}<br />
            {{ snapshot.created_at ? new Date(snapshot.created_at * 1000).toLocaleString() : '' }} · {{ snapshot.count_method }}
          </footer>
        </template>
        <div v-else class="empty-usage">{{ loading ? '正在读取统计…' : '暂无统计，首次模型调用后显示' }}</div>
        <div v-if="compacting" class="compaction-state" role="status">正在压缩上下文</div>
        <div v-else-if="snapshot?.compaction?.status === 'failed'" class="compaction-state failed">压缩未完成，保留原始反馈</div>
      </section>
    </template>
    <button type="button" class="context-usage-trigger" :class="{ compacting }" :title="tooltip" :aria-label="tooltip">
      <span ref="pieEl" class="context-pie" aria-hidden="true" />
      <span v-if="compacting" class="compact-dot" />
    </button>
  </a-popover>
</template>

<style scoped>
.context-usage-trigger { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border: 0; border-radius: 8px; background: transparent; cursor: pointer; flex-shrink: 0; }
.context-usage-trigger:hover, .context-usage-trigger:focus-visible { background: #eef2ff; outline: 2px solid #c7d2fe; }
.context-pie { display: block; width: 27px; height: 27px; }
.compact-dot { position: absolute; width: 7px; height: 7px; background: #6366f1; border: 2px solid white; border-radius: 50%; right: 1px; bottom: 2px; }
.context-usage-panel { width: min(380px, calc(100vw - 56px)); color: #334155; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.estimate { font-size: 11px; color: #64748b; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
.agent-select { width: 100%; margin-bottom: 10px; }
.usage-total { font-size: 12px; color: #64748b; }.usage-total b { font-size: 21px; color: #1e293b; }.usage-total em { float: right; font-style: normal; color: #6366f1; padding-top: 8px; }
.context-bar { height: 34px; width: 100%; margin: 6px 0; }
.module-list { max-height: 240px; overflow: auto; }.module-row { display: grid; grid-template-columns: 8px minmax(0,1fr) 76px 48px; gap: 9px; align-items: center; padding: 5px 0; font-size: 12px; }
.module-row i { width: 7px; height: 7px; border-radius: 2px; }.module-row b, .module-row small { text-align: right; font-variant-numeric: tabular-nums; font-weight: 400; }.module-row small { color: #64748b; }
footer { font-size: 10px; color: #94a3b8; line-height: 1.7; margin-top: 12px; overflow-wrap: anywhere; }
.empty-usage { padding: 24px 8px; color: #94a3b8; text-align: center; font-size: 12px; }.compaction-state { color: #6366f1; background: #eef2ff; padding: 7px 10px; border-radius: 6px; font-size: 12px; margin-top: 8px; }.failed { color: #b45309; background: #fffbeb; }
</style>
