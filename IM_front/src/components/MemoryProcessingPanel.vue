<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { getMemoryProcessingSettings, saveMemoryProcessingSettings, listMemoryBatches, getMemoryBatch, retryMemoryBatch, deleteMemoryBatch } from '@/api/memory'

const props = defineProps({ active: Boolean })
const emit = defineEmits(['deleted'])
const defaults = ref({ mode: 'jev', run_batch_size: 1, noul_threshold: 0.7 })
const draft = reactive({ ...defaults.value })
const saved = ref(null)
const server = ref({})
const loading = ref(false)
const settingsError = ref(false)
const saving = ref(false)
const dirty = computed(() => saved.value && Object.keys(defaults.value).some((key) => draft[key] !== saved.value[key]))
const valid = computed(() => Number.isInteger(draft.run_batch_size) && draft.run_batch_size >= 1 && draft.run_batch_size <= 100
  && Number.isFinite(draft.noul_threshold) && draft.noul_threshold >= 0 && draft.noul_threshold <= 1)
const names = { waiting: '等待组批', pending: '待处理', classifying: '分类中', extracting: '提取中', publishing: '发布中', finalizing: '结果保存中', completed: '已完成', failed: '失败', deleted: '已删除，等待清理' }
const categoryNames = { user_preference: '用户偏好', project_state: '项目状态', user_fact: '稳定用户事实', decision: '用户／系统决策', reusable_conclusion: '可复用实验结论' }
const stageNames = { waiting: '等待', classification: '筛选与分类', extraction: '分类提取与归并', publication: '发布', completed: '完成' }
const color = (status) => ({ failed: 'red', completed: 'green', waiting: 'default', pending: 'gold', deleted: 'default' }[status] || 'blue')
const date = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
async function loadSettings() {
  loading.value = true; settingsError.value = false
  try {
    const data = await getMemoryProcessingSettings()
    defaults.value = data.defaults; saved.value = data.item; server.value = data.server
    Object.assign(draft, data.item)
  } catch { settingsError.value = true }
  finally { loading.value = false }
}
async function save() {
  saving.value = true
  try {
    const data = await saveMemoryProcessingSettings({ ...draft })
    saved.value = data.item; server.value = data.server
    message.success('生成配置已保存，仅影响之后归档的 run')
  } catch { /* Shared interceptor reports errors. */ }
  finally { saving.value = false }
}
const items = ref([])
const total = ref(0)
const page = ref(1)
const listLoading = ref(false)
const listError = ref(false)
let listSequence = 0
async function loadBatches(silent = false) {
  const sequence = ++listSequence
  if (!silent) listLoading.value = true
  try {
    const data = await listMemoryBatches({ page: page.value, page_size: 20 })
    if (sequence !== listSequence) return
    if (!data.items.length && page.value > Math.max(1, Math.ceil(data.total / 20))) {
      page.value = Math.max(1, Math.ceil(data.total / 20))
      return await loadBatches(silent)
    }
    items.value = data.items; total.value = data.total; listError.value = false
  } catch { if (sequence === listSequence) listError.value = true }
  finally { if (sequence === listSequence) listLoading.value = false }
}
const open = ref(false)
const selected = ref('')
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref(false)
const retrying = ref('')
const deleting = reactive(new Set())
let detailSequence = 0
async function loadDetail(id, silent = false) {
  const sequence = ++detailSequence
  selected.value = id; open.value = true
  if (!silent) { detail.value = null; detailLoading.value = true }
  try {
    const data = await getMemoryBatch(id)
    if (sequence === detailSequence) { detail.value = data.item; detailError.value = false }
  } catch { if (sequence === detailSequence) detailError.value = true }
  finally { if (sequence === detailSequence) detailLoading.value = false }
}
function close() { open.value = false; ++detailSequence }
async function retry(id) {
  if (retrying.value || deleting.has(id)) return
  retrying.value = id
  try {
    const data = await retryMemoryBatch(id)
    if (selected.value === id && open.value) detail.value = data.item
    message.success('已加入重试队列，将复用完成的步骤')
    await loadBatches()
  } catch { /* Shared interceptor reports errors. */ }
  finally { retrying.value = '' }
}
function confirmDelete(batch) {
  Modal.confirm({
    title: '删除整个处理批次？',
    content: '该批次将立即标记为删除，停止后续生成和重试。关联记忆、派生记忆及 L1 中引用它们的段落将移除，后台随后清理批次与候选记录。此操作无法撤销。',
    okText: '删除批次', okType: 'danger', cancelText: '取消',
    async onOk() {
      const id = batch.batch_id
      if (deleting.has(id)) return
      deleting.add(id)
      try {
        await deleteMemoryBatch(id)
        if (selected.value === id) close()
        emit('deleted')
        message.success('批次已标记删除，后台将自动完成清理')
        await loadBatches()
      } finally { deleting.delete(id) }
    },
  })
}
function changePage(value) { page.value = value; loadBatches() }
let timer
onMounted(() => {
  loadSettings(); loadBatches()
  timer = setInterval(() => {
    if (!props.active || document.hidden || listLoading.value || listError.value) return
    loadBatches(true)
    if (open.value && !detailError.value && !detailLoading.value) loadDetail(selected.value, true)
  }, 10000)
})
watch(() => props.active, (active) => { if (active) loadBatches() })
onBeforeUnmount(() => { clearInterval(timer); ++listSequence; ++detailSequence })
</script>

<template>
  <div class="generation-layout">
    <section class="generation-panel">
      <div class="panel-title"><h2>生成配置</h2><a-tag :color="dirty ? 'orange' : 'green'">{{ dirty ? '未保存草稿' : '已保存配置' }}</a-tag></div>
      <p class="muted">先归档原文，再筛选、分类和批量提取。仅处理新 run，已有记忆保持原样。</p>
      <a-alert v-if="settingsError" type="error" message="配置加载失败" show-icon><template #action><a-button size="small" @click="loadSettings">重试</a-button></template></a-alert>
      <a-spin :spinning="loading">
        <fieldset :disabled="!saved || saving" class="generation-fields">
          <label>处理模式<a-select v-model:value="draft.mode" :disabled="!saved || saving" :options="[{ value: 'jev', label: 'JEV 筛选与批量提取' }, { value: 'rules', label: '规则分块直接索引' }]" /></label>
          <label>每批 run 数<a-input-number v-model:value="draft.run_batch_size" :disabled="!saved || saving || draft.mode === 'rules'" :min="1" :max="100" :precision="0" /></label>
          <label>Noul 收录阈值<a-input-number v-model:value="draft.noul_threshold" :disabled="!saved || saving || draft.mode === 'rules'" :min="0" :max="1" :step="0.05" /></label>
        </fieldset>
        <p class="rule-note">同一账号跨房间、跨会话组批。最早待处理 run 等待满 <strong>24 小时</strong>时，即使数量不足也会处理。成功、失败和取消的 run 均计数。</p>
        <a-alert v-if="saved && (!server.jev_ready || !server.extraction_ready)" type="warning" show-icon message="模型配置未就绪" :description="`${!server.jev_ready ? (server.jev_reason || 'JEV 未就绪。') : ''}${!server.extraction_ready ? '提取模型凭据未配置。' : ''}请在服务端配置后重试失败批次；不会自动切换规则模式。`" />
        <p v-if="saved" class="muted">JEV：{{ server.jev_model }} · {{ server.jev_reason || (server.jev_ready ? '已配置，尚未检测连接' : '未就绪') }}<br>提取：{{ server.extraction_model }} · {{ server.extraction_ready ? '已配置凭据' : '缺少凭据' }}</p>
        <div class="actions"><a-button :disabled="!saved || saving" @click="Object.assign(draft, defaults)">恢复默认</a-button><a-button type="primary" :loading="saving" :disabled="!dirty || !valid" @click="save">保存配置</a-button></div>
        <p class="muted">保存只影响之后归档的 run；已入队数据继续使用原配置。恢复默认仅修改草稿。</p>
      </a-spin>
    </section>
    <section class="generation-panel">
      <div class="panel-title"><h2>处理批次 <small>{{ total }}</small></h2><a-button :loading="listLoading" @click="loadBatches(false)">刷新</a-button></div>
      <p class="muted">全批分类完成后开始提取；全部提取通过校验后发布。每 10 秒刷新状态。</p>
      <a-result v-if="listError" status="warning" title="批次加载失败"><template #extra><a-button @click="loadBatches(false)">重试</a-button></template></a-result>
      <a-spin v-else :spinning="listLoading">
        <a-empty v-if="!items.length && !listLoading" description="还没有处理批次。JEV 模式的新 run 归档后会自动加入。" />
        <article v-for="batch in items" :key="batch.batch_id" class="batch-card">
          <div class="panel-title"><a-tag :color="color(batch.status)">{{ names[batch.status] }}</a-tag><time>{{ date(batch.created_at) }}</time></div>
          <div class="batch-counts"><span><b>{{ batch.run_count }}</b> run</span><span><b>{{ batch.candidate_count }}</b> 候选</span><span><b>{{ batch.accepted_count }}</b> 收录</span><span><b>{{ batch.output_count }}</b> 输出</span></div>
          <p v-if="batch.error" class="error-text">{{ batch.error }}</p>
          <div class="actions"><a-button type="link" @click="loadDetail(batch.batch_id)">查看批次详情</a-button><a-space wrap><a-button v-if="batch.status === 'failed'" :loading="retrying === batch.batch_id" :disabled="Boolean(retrying) || deleting.has(batch.batch_id)" @click="retry(batch.batch_id)">重试批次</a-button><a-button danger :loading="deleting.has(batch.batch_id)" :disabled="batch.status === 'deleted' || retrying === batch.batch_id" @click="confirmDelete(batch)">删除批次</a-button></a-space></div>
        </article>
      </a-spin>
      <a-pagination v-if="total" class="pagination" :current="page" :total="total" :page-size="20" :show-size-changer="false" @change="changePage" />
    </section>
  </div>
  <a-drawer :open="open" title="批次详情" width="min(720px, 100vw)" @close="close">
    <a-spin :spinning="detailLoading">
      <a-result v-if="detailError" status="warning" title="批次详情加载失败"><template #extra><a-button @click="loadDetail(selected)">重试</a-button></template></a-result>
      <template v-if="detail">
        <a-tag :color="color(detail.status)">{{ names[detail.status] }}</a-tag><a-tag>{{ stageNames[detail.stage] }}</a-tag>
        <p class="identifier">{{ detail.batch_id }}</p>
        <a-alert v-if="detail.status === 'deleted'" type="info" show-icon message="该批次已删除，不会再生成记忆或允许重试；后台将自动完成清理。" />
        <a-button v-else danger :loading="deleting.has(detail.batch_id)" :disabled="retrying === detail.batch_id" @click="confirmDelete(detail)">删除批次</a-button>
        <a-alert v-if="detail.error" type="error" :message="`${stageNames[detail.failed_stage] || detail.failed_stage}失败`" :description="detail.error" />
        <a-button v-if="detail.status === 'failed'" class="retry" :loading="retrying === detail.batch_id" :disabled="deleting.has(detail.batch_id)" @click="retry(detail.batch_id)">重试批次（保留完成步骤）</a-button>
        <p class="muted">配置快照：{{ detail.config.mode }} · 每批 {{ detail.config.run_batch_size }} run · Noul ≥ {{ detail.config.noul_threshold }}</p>
        <h3>筛选与分类</h3><p>{{ detail.classified_count }} / {{ detail.candidate_count }} 个候选完成，{{ detail.accepted_count }} 个收录</p>
        <h3>各类别提取进度</h3>
        <div v-for="(name, key) in categoryNames" :key="key" class="category-row"><span>{{ name }}</span><span v-if="detail.category_progress[key]">{{ detail.category_progress[key].completed }} / {{ detail.category_progress[key].total }} 子批 · {{ detail.category_progress[key].status === 'completed' ? '完成' : detail.status === 'failed' ? '暂停（本批失败）' : '提取中' }}</span><span v-else class="muted">{{ detail.stage === 'classification' || detail.stage === 'waiting' ? '等待全批分类完成' : '等待本类提取' }}</span></div>
        <h3>来源 run</h3><p v-for="source in detail.sources" :key="source.source_id" class="identifier">{{ source.run_id }} · {{ source.run_status }} · {{ source.room_id }} / {{ source.conversation_id }}</p>
        <h3>候选诊断</h3><p v-if="detail.candidates_truncated" class="muted">展示前 100 条，完整记录保存在数据库中。</p>
        <a-collapse>
          <a-collapse-panel v-for="candidate in detail.candidates" :key="candidate.candidate_id" :header="`${candidate.status === 'accepted' ? '收录' : candidate.status === 'rejected' ? '拒绝' : '等待'} · ${candidate.summary.slice(0, 45)}`">
            <p class="candidate-text">{{ candidate.summary }}</p><p>Noul：{{ candidate.noul_result?.noul ?? '尚未判断' }}</p>
            <template v-if="candidate.choice_result"><p>主类别：{{ categoryNames[candidate.choice_result.category] }} · Choice 置信度：{{ candidate.choice_result.confidence }}</p><div v-for="(value, key) in candidate.choice_result.probabilities" :key="key" class="category-row"><span>{{ categoryNames[key] }}</span><span>{{ (value * 100).toFixed(1) }}%</span></div><p class="muted">{{ candidate.choice_result.model }} · {{ candidate.choice_result.prompt_version }}</p></template>
            <p class="identifier">{{ candidate.file_path }}:{{ candidate.start_line }}–{{ candidate.end_line }}</p>
          </a-collapse-panel>
        </a-collapse>
      </template>
    </a-spin>
  </a-drawer>
</template>

<style scoped>
.generation-layout { display: grid; grid-template-columns: 340px minmax(0, 1fr); gap: 22px; align-items: start; }
.generation-panel { background: #fff; border: 1px solid #e4eaf3; border-radius: 18px; padding: 24px; min-width: 0; }
.panel-title { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
h2 { font-size: 17px; margin: 0; } h3 { font-size: 15px; margin-top: 26px; }
small, time, .muted { color: var(--muted); font-size: 12px; line-height: 1.8; }
.generation-fields { border: 0; padding: 0; margin: 20px 0; display: grid; gap: 18px; min-width: 0; }
label { display: grid; gap: 8px; font-size: 12px; color: var(--muted); }
.generation-fields :deep(.ant-input-number), .generation-fields :deep(.ant-select) { width: 100%; }
.rule-note { padding: 14px; border-radius: 10px; background: #f5f8fd; font-size: 12px; line-height: 1.9; }
.actions { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 16px; }
.batch-card { border: 1px solid #e4eaf3; border-radius: 12px; padding: 18px; margin-top: 14px; }
.batch-counts { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; font-size: 12px; color: var(--muted); }
.batch-counts b { display: block; color: var(--text); font-size: 22px; font-weight: 600; }
.error-text { font-size: 12px; color: #bd303b; overflow-wrap: anywhere; }
.pagination { margin-top: 22px; display: flex; justify-content: flex-end; flex-wrap: wrap; }
.category-row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid #edf1f6; font-size: 12px; }
.identifier { overflow-wrap: anywhere; font-size: 12px; color: var(--muted); line-height: 1.8; }
.retry { margin-top: 12px; }.candidate-text { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 900px) { .generation-layout { grid-template-columns: minmax(0,1fr); gap: 16px; }.generation-panel { padding: 18px; }.batch-card { padding: 14px; }.panel-title { flex-wrap: wrap; } }
</style>
