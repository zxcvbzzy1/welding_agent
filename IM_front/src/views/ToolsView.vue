<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import {
  ToolOutlined,
  ApiOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { listTools, getTool, patchToolConfig } from '@/api/tools'

const tools = ref([])
const loading = ref(false)
const activeName = ref(null)
const detail = ref(null)
const detailLoading = ref(false)
const savingConfig = ref(false)

// 仅 bash 有运行时配置
const config = reactive({ danger_policy: 'reject', auto_confirm: 'ask' })

async function loadList() {
  loading.value = true
  try {
    const res = await listTools()
    tools.value = res.items ?? []
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}
onMounted(loadList)

async function selectTool(tool) {
  if (activeName.value === tool.name) return
  activeName.value = tool.name
  detailLoading.value = true
  try {
    const res = await getTool(tool.name)
    detail.value = res.item
    const cfg = res.item?.config || {}
    config.danger_policy = cfg.danger_policy || 'reject'
    config.auto_confirm = cfg.auto_confirm || 'ask'
  } catch {
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

const hasSelection = computed(() => activeName.value !== null)
const isBash = computed(() => detail.value?.name === 'bash')
const toolCountLabel = computed(() => `${tools.value.length} 个工具`)

const schemaProps = computed(() => {
  const props = detail.value?.input_schema?.properties || {}
  return Object.entries(props).map(([key, v]) => ({
    key,
    type: Array.isArray(v?.type) ? v.type.join(' | ') : v?.type || '',
    description: v?.enum ? `枚举: ${v.enum.join(', ')}` : v?.description || '',
  }))
})
const requiredSet = computed(() => new Set(detail.value?.input_schema?.required || []))
const events = computed(() => detail.value?.events || [])

async function saveConfig() {
  if (!isBash.value || savingConfig.value) return
  savingConfig.value = true
  try {
    await patchToolConfig('bash', {
      danger_policy: config.danger_policy,
      auto_confirm: config.auto_confirm,
    })
    message.success('配置已保存并即时生效')
  } catch {
    // 拦截器已提示
  } finally {
    savingConfig.value = false
  }
}

const fieldColor = (field) =>
  ({ system: 'blue', search: 'green', memory: 'purple', human: 'orange', robot: 'magenta', camera: 'cyan' }[field] || 'default')
</script>

<template>
  <div class="tools-workspace">
    <!-- ── left: tool list ── -->
    <aside class="tools-sidebar">
      <div class="tools-sidebar-head">
        <div>
          <span class="tools-sidebar-kicker">TOOLS</span>
          <h2>工具目录</h2>
        </div>
        <span class="tools-count">{{ toolCountLabel }}</span>
      </div>

      <div class="tools-list-scroll">
        <template v-if="loading && tools.length === 0">
          <div v-for="n in 5" :key="n" class="tool-card tool-card--skeleton">
            <a-skeleton active :paragraph="{ rows: 1 }" />
          </div>
        </template>
        <div v-else-if="!loading && tools.length === 0" class="tools-empty">
          <ToolOutlined class="tools-empty-icon" />
          <p>暂无工具</p>
        </div>
        <div
          v-for="tool in tools"
          :key="tool.name"
          class="tool-card"
          :class="{ 'tool-card--active': activeName === tool.name }"
          @click="selectTool(tool)"
        >
          <div class="tool-card-top">
            <strong class="tool-card-name">{{ tool.name }}</strong>
            <a-tag v-if="tool.field" :color="fieldColor(tool.field)" class="tool-field-tag">{{ tool.field }}</a-tag>
          </div>
          <span v-if="tool.description" class="tool-card-desc muted">{{ tool.description }}</span>
        </div>
      </div>
    </aside>

    <!-- ── right: read-only detail + config ── -->
    <main class="tools-detail">
      <div v-if="!hasSelection" class="tools-placeholder">
        <ApiOutlined class="tools-placeholder-icon" />
        <p>从左侧选择一个工具查看详情。工具为只读展示，部分工具支持参数配置。</p>
      </div>

      <template v-else>
        <a-spin :spinning="detailLoading">
          <div v-if="detail" class="tools-detail-head">
            <div class="tools-detail-title">
              <div class="tools-title-icon"><ToolOutlined /></div>
              <div class="tools-title-copy">
                <div class="tools-detail-kicker">
                  <span class="status-dot"></span>
                  只读展示
                </div>
                <h2>{{ detail.name }}</h2>
                <p>{{ detail.description || '—' }}</p>
              </div>
            </div>
            <a-tag v-if="detail.field" :color="fieldColor(detail.field)">{{ detail.field }}</a-tag>
          </div>

          <div v-if="detail" class="tools-detail-body">
            <!-- 配置区：仅 bash -->
            <section v-if="isBash" class="tools-panel tools-panel--config">
              <div class="panel-head">
                <SafetyCertificateOutlined />
                <strong>危险命令处理策略</strong>
                <span class="panel-chip">可配置 · 即时生效</span>
              </div>
              <div class="config-grid">
                <div class="config-field">
                  <label>检测到高危命令（rm/sudo/kill 等）时</label>
                  <a-radio-group v-model:value="config.danger_policy" button-style="solid">
                    <a-radio-button value="reject">直接拒绝</a-radio-button>
                    <a-radio-button value="confirm">人工确认</a-radio-button>
                  </a-radio-group>
                </div>
                <div class="config-field">
                  <label>
                    人工确认方式
                    <span class="config-hint">（策略为「人工确认」时生效）</span>
                  </label>
                  <a-radio-group
                    v-model:value="config.auto_confirm"
                    button-style="solid"
                    :disabled="config.danger_policy !== 'confirm'"
                  >
                    <a-radio-button value="ask">弹窗询问</a-radio-button>
                    <a-radio-button value="approve">自动批准</a-radio-button>
                    <a-radio-button value="reject">自动拒绝</a-radio-button>
                  </a-radio-group>
                </div>
                <div class="config-actions">
                  <a-button type="primary" :loading="savingConfig" @click="saveConfig">
                    <template #icon><SaveOutlined /></template>
                    保存配置
                  </a-button>
                </div>
              </div>
            </section>

            <!-- 参数 schema -->
            <section class="tools-panel">
              <div class="panel-head">
                <ThunderboltOutlined />
                <strong>输入参数</strong>
                <span class="panel-chip">{{ schemaProps.length }} 项</span>
              </div>
              <a-empty v-if="!schemaProps.length" description="无参数" :image-style="{ height: '40px' }" />
              <table v-else class="schema-table">
                <thead>
                  <tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr>
                </thead>
                <tbody>
                  <tr v-for="p in schemaProps" :key="p.key">
                    <td><code>{{ p.key }}</code></td>
                    <td class="schema-type">{{ p.type }}</td>
                    <td>
                      <a-tag v-if="requiredSet.has(p.key)" color="red" size="small">必填</a-tag>
                      <span v-else class="muted">可选</span>
                    </td>
                    <td class="schema-desc">{{ p.description }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <!-- 事件 -->
            <section v-if="events.length" class="tools-panel">
              <div class="panel-head">
                <ApiOutlined />
                <strong>事件</strong>
                <span class="panel-chip">{{ events.length }}</span>
              </div>
              <div class="events-list">
                <a-tag v-for="ev in events" :key="ev">{{ ev }}</a-tag>
              </div>
            </section>
          </div>
        </a-spin>
      </template>
    </main>
  </div>
</template>

<style scoped>
.tools-workspace {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  height: calc(100vh - 66px);
  overflow: hidden;
}
.tools-sidebar {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.7), rgba(255, 255, 255, 0.48)),
    radial-gradient(circle at 20% 4%, rgba(53, 120, 255, 0.12), transparent 28%);
  border-right: 1px solid rgba(255, 255, 255, 0.62);
  backdrop-filter: blur(20px);
}
.tools-sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 0 0 auto;
  padding: 18px 16px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.62);
}
.tools-sidebar-kicker {
  display: block;
  margin-bottom: 2px;
  color: var(--accent);
  font-size: 10px;
  font-weight: 850;
  letter-spacing: 0.08em;
}
.tools-sidebar-head h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 800;
  color: var(--text);
}
.tools-count {
  padding: 4px 9px;
  color: #175199;
  background: rgba(255, 255, 255, 0.66);
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 999px;
  font-size: 12px;
  font-weight: 750;
}
.tools-list-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: grid;
  align-content: start;
  gap: 8px;
  scrollbar-width: none;
}
.tools-list-scroll::-webkit-scrollbar { display: none; }
.tool-card {
  padding: 13px;
  background: rgba(255, 255, 255, 0.58);
  border: 1px solid rgba(255, 255, 255, 0.56);
  border-radius: 12px;
  box-shadow: 0 6px 16px rgba(27, 39, 66, 0.04);
  cursor: pointer;
  transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
  display: grid;
  gap: 5px;
}
.tool-card:hover {
  transform: translateY(-1px);
  background: rgba(255, 255, 255, 0.82);
  box-shadow: 0 12px 28px rgba(27, 39, 66, 0.08);
}
.tool-card--active {
  background:
    linear-gradient(135deg, rgba(53, 120, 255, 0.16), rgba(24, 198, 212, 0.12)),
    rgba(255, 255, 255, 0.88);
  border-color: rgba(53, 120, 255, 0.24);
}
.tool-card--skeleton { cursor: default; pointer-events: none; min-height: 60px; }
.tool-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tool-card-name {
  color: var(--text);
  font-weight: 760;
  font-size: 13.5px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.tool-field-tag { margin: 0; flex: 0 0 auto; }
.tool-card-desc {
  display: block;
  font-size: 12px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.tools-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 40px 16px;
  color: var(--muted);
}
.tools-empty-icon { font-size: 36px; color: rgba(53, 120, 255, 0.36); }

.tools-detail {
  min-height: 0;
  overflow-y: auto;
  padding: 22px 24px 28px;
  background:
    radial-gradient(circle at 80% 10%, rgba(53, 120, 255, 0.06), transparent 30%),
    radial-gradient(circle at 10% 90%, rgba(24, 198, 212, 0.05), transparent 28%);
}
.tools-placeholder {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--muted);
  text-align: center;
}
.tools-placeholder-icon { font-size: 52px; color: rgba(53, 120, 255, 0.22); }
.tools-placeholder p { margin: 0; font-size: 14px; max-width: 320px; }

.tools-detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 16px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.8), rgba(246, 250, 255, 0.58)),
    rgba(255, 255, 255, 0.42);
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 16px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.88), 0 14px 34px rgba(27, 39, 66, 0.08);
  backdrop-filter: blur(18px) saturate(1.16);
}
.tools-detail-title { display: flex; align-items: center; gap: 13px; min-width: 0; }
.tools-title-icon {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 44px;
  height: 44px;
  color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  border-radius: 13px;
  box-shadow: 0 14px 30px rgba(53, 120, 255, 0.22);
  font-size: 18px;
}
.tools-title-copy { min-width: 0; }
.tools-detail-kicker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 2px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 850;
}
.status-dot {
  width: 7px;
  height: 7px;
  background: linear-gradient(135deg, var(--accent-3, #58d68d), var(--accent-2));
  border-radius: 999px;
}
.tools-detail-title h2 { margin: 0; font-size: 22px; font-weight: 800; color: var(--text); }
.tools-detail-title p {
  max-width: 640px;
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.tools-detail-body { display: grid; gap: 16px; }
.tools-panel {
  padding: 16px 18px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.82), rgba(250, 252, 255, 0.66)),
    rgba(255, 255, 255, 0.48);
  border: 1px solid rgba(255, 255, 255, 0.82);
  border-radius: 16px;
  box-shadow: 0 14px 36px rgba(27, 39, 66, 0.07);
}
.tools-panel--config { border-color: rgba(53, 120, 255, 0.22); }
.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  color: var(--accent);
}
.panel-head strong { color: var(--text); font-size: 15px; font-weight: 800; }
.panel-chip {
  margin-left: auto;
  padding: 4px 10px;
  color: #087681;
  background: rgba(234, 255, 244, 0.78);
  border: 1px solid rgba(88, 214, 141, 0.2);
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
}
.config-grid { display: grid; gap: 16px; }
.config-field { display: grid; gap: 8px; }
.config-field label { font-size: 13px; font-weight: 700; color: var(--text); }
.config-hint { font-weight: 500; color: var(--muted); font-size: 12px; }
.config-actions { display: flex; justify-content: flex-end; }
.schema-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.schema-table th,
.schema-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid rgba(95, 111, 139, 0.12);
  vertical-align: top;
}
.schema-table th { color: var(--muted); font-weight: 700; font-size: 12px; }
.schema-table code {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 12px;
  background: rgba(53, 120, 255, 0.08);
  padding: 1px 6px;
  border-radius: 5px;
}
.schema-type { color: #175199; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.schema-desc { color: var(--muted); }
.events-list { display: flex; flex-wrap: wrap; gap: 6px; }
.muted { color: var(--muted); }

@media (max-width: 760px) {
  .tools-workspace {
    grid-template-columns: 1fr;
    height: auto;
    min-height: calc(100vh - 110px);
    overflow: visible;
  }
  .tools-sidebar { height: min(360px, 46vh); }
}
</style>
