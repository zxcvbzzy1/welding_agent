<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Modal, message } from 'ant-design-vue'
import {
  ApiOutlined,
  CameraOutlined,
  CloudDownloadOutlined,
  CloudOutlined,
  DeleteOutlined,
  DownloadOutlined,
  HistoryOutlined,
  PauseCircleOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  SaveOutlined,
  ScanOutlined,
  SwapOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons-vue'
import PointCloudViewer from '@/components/PointCloudViewer.vue'
import {
  cameraErrorMessage,
  cameraFileUrl,
  captureCamera,
  connectCamera,
  disconnectCamera,
  discoverCameras,
  downloadCameraFile,
  getCameraHealth,
  getCurrentCamera,
  normalizeCameraBaseUrl,
  transformCameraPoints,
} from '@/api/camera'
import { useCameraWebRtc } from '@/composables/useCameraWebRtc'
import { useCameraStore } from '@/stores/camera'

const cameraStore = useCameraStore()
const cloneSettings = (value) => JSON.parse(JSON.stringify(value))
const draft = reactive(cloneSettings(cameraStore.settings))
const serviceState = ref('checking')
const serviceError = ref('')
const health = ref(null)
const cameras = ref([])
const currentCamera = ref(null)
const discovering = ref(false)
const connecting = ref(false)
const disconnecting = ref(false)
const captureBusy = ref('')
const downloadBusy = ref('')
const videoElement = ref(null)
const pointCloudUrl = ref('')
const pointCloudLabel = ref('')
const streamAutoSuppressed = ref(false)
const manualPointsText = ref('0, 0, 0')
const transformedPoints = ref([])
const transformBusy = ref(false)
const rtc = useCameraWebRtc(videoElement)

let pollTimer = 0
let probeInFlight = false
let disposed = false

const captureHistory = computed(() => cameraStore.captureHistory)
const isConnected = computed(() => Boolean(currentCamera.value))
const isDirty = computed(() => JSON.stringify(draft) !== JSON.stringify(cameraStore.settings))
const mixedContentWarning = computed(() => (
  window.location.protocol === 'https:'
  && String(cameraStore.settings.serviceBaseUrl).startsWith('http:')
))
const cameraOptions = computed(() => cameras.value.map((camera) => ({
  value: camera.serial_number,
  label: `${camera.device_name || camera.model || '相机'} · ${camera.serial_number}`,
})))

const serviceStatus = computed(() => ({
  checking: { color: 'processing', text: '检测中' },
  online: { color: 'success', text: '服务在线' },
  offline: { color: 'error', text: '服务离线' },
}[serviceState.value]))

const streamStatus = computed(() => ({
  idle: { color: 'default', text: '未启动' },
  negotiating: { color: 'processing', text: '连接中' },
  playing: { color: 'success', text: '实时播放' },
  disconnected: { color: 'warning', text: '已断开' },
  error: { color: 'error', text: '连接失败' },
}[rtc.state.value] || { color: 'default', text: rtc.state.value }))

const captureActions = [
  { kind: '2d', label: '采集 2D 图', icon: PictureOutlined },
  { kind: 'depth', label: '采集深度图', icon: ScanOutlined },
  { kind: 'point-cloud', label: '采集点云', icon: CloudOutlined },
  { kind: 'all', label: '采集全部', icon: CloudDownloadOutlined },
]

const captureKindLabels = {
  '2d': '2D 图像',
  depth: '深度图',
  'point-cloud': '点云',
  all: '全部数据',
}

const fileLabels = {
  image_2d: '2D 图像',
  depth_map: '深度图',
  point_cloud: '无纹理点云',
  textured_point_cloud: '纹理点云',
  transformed_point_cloud: '转换后点云',
  transformed_textured_point_cloud: '转换后纹理点云',
}

const processingOptions = [
  { value: '', label: '保持相机当前值' },
  { value: 'Off', label: '关闭' },
  { value: 'Weak', label: '弱' },
  { value: 'Normal', label: '标准' },
  { value: 'Strong', label: '强' },
]

const edgeOptions = [
  { value: '', label: '保持相机当前值' },
  { value: 'Sharp', label: '锐利' },
  { value: 'Normal', label: '标准' },
  { value: 'Smooth', label: '平滑' },
]

const poseMatrix = computed(() => {
  const toRadians = (degrees) => Number(degrees || 0) * Math.PI / 180
  const rx = toRadians(draft.rotationX)
  const ry = toRadians(draft.rotationY)
  const rz = toRadians(draft.rotationZ)
  const sx = Math.sin(rx); const cx = Math.cos(rx)
  const sy = Math.sin(ry); const cy = Math.cos(ry)
  const sz = Math.sin(rz); const cz = Math.cos(rz)
  return [
    [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx, Number(draft.translationX || 0)],
    [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx, Number(draft.translationY || 0)],
    [-sy, cy * sx, cy * cx, Number(draft.translationZ || 0)],
    [0, 0, 0, 1],
  ]
})

const effectiveTransformation = computed(() => (
  draft.coordinateTransformMode === 'matrix'
    ? draft.transformationMatrix.map((row) => row.map(Number))
    : poseMatrix.value
))

function formatMatrixValue(value) {
  const number = Math.abs(value) < 1e-12 ? 0 : value
  return Number(number.toFixed(6))
}

function newLocalId() {
  return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function streamCanStart() {
  return isConnected.value && serviceState.value === 'online'
    && !['negotiating', 'playing'].includes(rtc.state.value)
}

async function startStream() {
  if (!streamCanStart()) return
  streamAutoSuppressed.value = false
  await nextTick()
  await rtc.start(cameraStore.settings.serviceBaseUrl)
}

async function stopStream() {
  streamAutoSuppressed.value = true
  await rtc.stop()
}

async function loadDiscoveredCameras({ notify = false } = {}) {
  if (discovering.value || serviceState.value !== 'online') return
  discovering.value = true
  try {
    cameras.value = await discoverCameras(cameraStore.settings.serviceBaseUrl)
    if (!draft.preferredSerialNumber && cameras.value.length === 1) {
      draft.preferredSerialNumber = cameras.value[0].serial_number
    }
    if (notify) message.success(`发现 ${cameras.value.length} 台相机`)
  } catch (reason) {
    if (notify) message.error(cameraErrorMessage(reason, '相机发现失败'))
  } finally {
    discovering.value = false
  }
}

async function syncCurrentCamera(cameraConnected, serialNumber) {
  if (!cameraConnected) {
    currentCamera.value = null
    if (rtc.state.value !== 'idle') await rtc.stop()
    return
  }
  if (!currentCamera.value || currentCamera.value.serial_number !== serialNumber) {
    try {
      currentCamera.value = await getCurrentCamera(cameraStore.settings.serviceBaseUrl)
    } catch (reason) {
      currentCamera.value = null
      serviceError.value = cameraErrorMessage(reason, '无法读取当前相机')
      return
    }
  }
  if (!streamAutoSuppressed.value && streamCanStart()) await startStream()
}

async function probeService({ includeDiscovery = false, notify = false } = {}) {
  if (probeInFlight || disposed) return
  probeInFlight = true
  const wasOffline = serviceState.value === 'offline'
  if (!health.value) serviceState.value = 'checking'
  try {
    const result = await getCameraHealth(cameraStore.settings.serviceBaseUrl)
    if (disposed) return
    health.value = result
    serviceState.value = 'online'
    serviceError.value = result.stream_last_error || ''
    await syncCurrentCamera(result.camera_connected, result.camera_serial_number)
    if (includeDiscovery || wasOffline) await loadDiscoveredCameras()
    if (notify) message.success('相机服务连接正常')
  } catch (reason) {
    if (disposed) return
    serviceState.value = 'offline'
    serviceError.value = cameraErrorMessage(reason, '无法连接相机服务')
    health.value = null
    currentCamera.value = null
    cameras.value = []
    if (rtc.state.value !== 'idle') await rtc.stop()
    if (notify) message.error(serviceError.value)
  } finally {
    probeInFlight = false
  }
}

async function saveSettings() {
  try {
    const normalizedBaseUrl = normalizeCameraBaseUrl(draft.serviceBaseUrl)
    const baseChanged = normalizedBaseUrl !== cameraStore.settings.serviceBaseUrl
    cameraStore.saveSettings({ ...draft, serviceBaseUrl: normalizedBaseUrl })
    Object.assign(draft, cloneSettings(cameraStore.settings))
    message.success('相机配置已保存到当前浏览器')

    if (baseChanged) {
      streamAutoSuppressed.value = false
      await rtc.stop()
      serviceState.value = 'checking'
      serviceError.value = ''
      health.value = null
      currentCamera.value = null
      cameras.value = []
      pointCloudUrl.value = ''
      pointCloudLabel.value = ''
      await probeService({ includeDiscovery: true })
    }
  } catch (reason) {
    message.error(reason?.message || '相机配置保存失败')
  }
}

async function connectSelectedCamera() {
  if (!draft.preferredSerialNumber || connecting.value) return
  connecting.value = true
  try {
    currentCamera.value = await connectCamera(
      cameraStore.settings.serviceBaseUrl,
      draft.preferredSerialNumber,
    )
    if (health.value) {
      health.value.camera_connected = true
      health.value.camera_serial_number = currentCamera.value.serial_number
    }
    message.success(`已连接相机 ${currentCamera.value.serial_number}`)
    await startStream()
  } catch (reason) {
    message.error(cameraErrorMessage(reason, '连接相机失败'))
  } finally {
    connecting.value = false
  }
}

function confirmDisconnect() {
  Modal.confirm({
    title: '断开当前相机？',
    content: '该操作会关闭微服务上的所有实时视频连接，可能影响其他客户端。',
    okText: '确认断开',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      disconnecting.value = true
      try {
        await rtc.stop()
        await disconnectCamera(cameraStore.settings.serviceBaseUrl)
        currentCamera.value = null
        if (health.value) {
          health.value.camera_connected = false
          health.value.camera_serial_number = null
          health.value.rtc_peer_count = 0
        }
        message.success('相机已断开')
      } catch (reason) {
        message.error(cameraErrorMessage(reason, '断开相机失败'))
        throw reason
      } finally {
        disconnecting.value = false
      }
    },
  })
}

function pointCloudPath(files) {
  return files?.transformed_textured_point_cloud
    || files?.transformed_point_cloud
    || files?.textured_point_cloud
    || files?.point_cloud
    || ''
}

function captureOptions() {
  const parameters = {}
  if (draft.depthRangeEnabled) {
    if (Number(draft.depthMinMm) >= Number(draft.depthMaxMm)) {
      throw new Error('深度下限必须小于深度上限')
    }
    parameters.depth_range = {
      min_mm: Number(draft.depthMinMm),
      max_mm: Number(draft.depthMaxMm),
    }
  }
  if (draft.roiEnabled) {
    if (Number(draft.roiWidth) <= 0 || Number(draft.roiHeight) <= 0) {
      throw new Error('ROI 宽度和高度必须大于 0')
    }
    parameters.roi = {
      x: Number(draft.roiX),
      y: Number(draft.roiY),
      width: Number(draft.roiWidth),
      height: Number(draft.roiHeight),
    }
  }
  if (draft.exposureEnabled) {
    const sequence = String(draft.exposureSequence)
      .split(/[,，\s]+/)
      .filter(Boolean)
      .map(Number)
    if (!sequence.length || sequence.some((value) => !Number.isFinite(value) || value <= 0)) {
      throw new Error('曝光序列请输入以逗号分隔的正数（毫秒）')
    }
    parameters.exposure_sequence_ms = sequence
  }
  if (draft.surfaceSmoothing) parameters.surface_smoothing = draft.surfaceSmoothing
  if (draft.noiseRemoval) parameters.noise_removal = draft.noiseRemoval
  if (draft.outlierRemoval) parameters.outlier_removal = draft.outlierRemoval
  if (draft.edgePreservation) parameters.edge_preservation = draft.edgePreservation

  const transformation = draft.coordinateTransformEnabled
    ? effectiveTransformation.value
    : null
  if (transformation && (
    transformation.length !== 4
    || transformation.some((row) => row.length !== 4 || row.some((value) => !Number.isFinite(value)))
    || transformation[3].some((value, index) => Math.abs(value - [0, 0, 0, 1][index]) > 1e-9)
  )) {
    throw new Error('转换矩阵必须是有效的齐次 4×4 矩阵，末行为 0, 0, 0, 1')
  }
  return {
    textured: draft.textured,
    customReferenceFrame: draft.customReferenceFrame,
    maxPoints: draft.maxPoints,
    parameters,
    transformation,
  }
}

function parseManualPoints() {
  const rows = String(manualPointsText.value).split(/\r?\n/).filter((line) => line.trim())
  const points = rows.map((line, index) => {
    const values = line.trim().split(/[,，\s]+/).map(Number)
    if (values.length !== 3 || values.some((value) => !Number.isFinite(value))) {
      throw new Error(`第 ${index + 1} 行不是有效的 X, Y, Z 坐标`)
    }
    return { x: values[0], y: values[1], z: values[2] }
  })
  if (!points.length) throw new Error('请至少输入一个坐标点')
  return points
}

async function runPointTransform() {
  if (transformBusy.value) return
  transformBusy.value = true
  try {
    const result = await transformCameraPoints(
      cameraStore.settings.serviceBaseUrl,
      parseManualPoints(),
      effectiveTransformation.value,
    )
    transformedPoints.value = result.points || []
    message.success(`已转换 ${transformedPoints.value.length} 个坐标点`)
  } catch (reason) {
    message.error(cameraErrorMessage(reason, reason?.message || '坐标转换失败'))
  } finally {
    transformBusy.value = false
  }
}

function previewCapture(capture) {
  const path = pointCloudPath(capture.files)
  if (!path) return
  try {
    pointCloudUrl.value = cameraFileUrl(capture.serviceBaseUrl, path)
    pointCloudLabel.value = path.split('/').pop() || '点云预览'
  } catch (reason) {
    message.error(reason?.message || '点云文件地址无效')
  }
}

async function runCapture(kind) {
  if (!isConnected.value || captureBusy.value) return
  captureBusy.value = kind
  try {
    const result = await captureCamera(
      cameraStore.settings.serviceBaseUrl,
      kind,
      kind === 'point-cloud' || kind === 'all' ? captureOptions() : {},
    )
    const capture = {
      localId: newLocalId(),
      kind,
      captureId: result.capture_id,
      capturedAt: result.captured_at,
      files: result.files || {},
      serviceBaseUrl: cameraStore.settings.serviceBaseUrl,
      pointCloud: result.point_cloud || null,
    }
    cameraStore.addCapture(capture)
    if (pointCloudPath(capture.files)) previewCapture(capture)
    message.success(`${captureKindLabels[kind]}采集完成`)
  } catch (reason) {
    message.error(cameraErrorMessage(reason, `${captureKindLabels[kind]}采集失败`))
  } finally {
    captureBusy.value = ''
  }
}

async function downloadFile(capture, fileType, path) {
  const busyKey = `${capture.localId}:${fileType}`
  if (downloadBusy.value) return
  downloadBusy.value = busyKey
  try {
    const { blob, filename } = await downloadCameraFile(capture.serviceBaseUrl, path)
    const blobUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1000)
  } catch (reason) {
    message.error(cameraErrorMessage(reason, '文件下载失败，记录可能已经失效'))
  } finally {
    downloadBusy.value = ''
  }
}

function removeCapture(capture) {
  try {
    cameraStore.removeCapture(capture)
  } catch (reason) {
    message.error(reason?.message || '删除本地记录失败')
  }
}

function confirmClearHistory() {
  Modal.confirm({
    title: '清空最近采集记录？',
    content: '只会清除浏览器中的下载引用，不会删除微服务上的文件。',
    okText: '清空',
    okType: 'danger',
    cancelText: '取消',
    onOk() {
      cameraStore.clearCaptureHistory()
    },
  })
}

onMounted(async () => {
  await probeService({ includeDiscovery: true })
  pollTimer = window.setInterval(() => probeService(), 10000)
})

onBeforeUnmount(() => {
  disposed = true
  window.clearInterval(pollTimer)
  rtc.dispose()
})
</script>

<template>
  <main class="camera-page">
    <header class="camera-page-head">
      <div class="page-title">
        <div class="page-title-icon"><VideoCameraOutlined /></div>
        <div>
          <span class="page-kicker">CAMERA WORKBENCH</span>
          <h1>相机管理</h1>
          <p>管理 Mech-Eye 连接、实时画面、采集文件与点云预览</p>
        </div>
      </div>
      <div class="page-head-actions">
        <a-tag v-if="isDirty" color="orange">配置未保存</a-tag>
        <a-tag :color="serviceStatus.color">{{ serviceStatus.text }}</a-tag>
        <a-button type="primary" :disabled="!isDirty" @click="saveSettings">
          <template #icon><SaveOutlined /></template>
          保存配置
        </a-button>
      </div>
    </header>

    <a-alert
      v-if="mixedContentWarning"
      class="camera-warning"
      type="warning"
      show-icon
      message="当前页面使用 HTTPS，但相机服务使用 HTTP；浏览器可能拦截请求，请为服务配置 HTTPS 或通过同源代理访问。"
    />

    <div class="camera-workspace">
      <aside class="camera-controls">
        <section class="control-section">
          <div class="section-heading">
            <ApiOutlined />
            <strong>微服务</strong>
            <a-tag :color="serviceStatus.color" size="small">{{ serviceStatus.text }}</a-tag>
          </div>
          <label class="field-label" for="camera-service-url">服务地址</label>
          <a-input id="camera-service-url" v-model:value="draft.serviceBaseUrl" placeholder="http://10.42.0.1:7899" />
          <div class="field-meta">当前运行：{{ cameraStore.settings.serviceBaseUrl }}</div>
          <a-alert v-if="serviceError" class="compact-alert" type="warning" :message="serviceError" show-icon />
          <a-button block :loading="serviceState === 'checking'" @click="probeService({ includeDiscovery: true, notify: true })">
            <template #icon><ReloadOutlined /></template>
            重新检测
          </a-button>
        </section>

        <section class="control-section">
          <div class="section-heading">
            <CameraOutlined />
            <strong>相机连接</strong>
            <span class="section-spacer"></span>
            <a-button type="text" size="small" :loading="discovering" :disabled="serviceState !== 'online'" @click="loadDiscoveredCameras({ notify: true })">
              刷新
            </a-button>
          </div>
          <label class="field-label">首选相机</label>
          <a-select
            v-model:value="draft.preferredSerialNumber"
            class="camera-select"
            :options="cameraOptions"
            :loading="discovering"
            :disabled="serviceState !== 'online' || isConnected"
            placeholder="请选择发现的相机"
          />
          <div v-if="currentCamera" class="connected-camera">
            <div class="connected-camera-top">
              <span class="online-dot"></span>
              <strong>{{ currentCamera.device_name || currentCamera.model || '已连接相机' }}</strong>
            </div>
            <dl>
              <div><dt>序列号</dt><dd>{{ currentCamera.serial_number }}</dd></div>
              <div><dt>地址</dt><dd>{{ currentCamera.ip_address }}:{{ currentCamera.port }}</dd></div>
              <div><dt>固件</dt><dd>{{ currentCamera.firmware_version || '—' }}</dd></div>
            </dl>
          </div>
          <a-empty v-else-if="!discovering && serviceState === 'online' && cameras.length === 0" description="未发现相机" :image-style="{ height: '36px' }" />
          <div class="connection-actions">
            <a-button
              v-if="!isConnected"
              type="primary"
              block
              :loading="connecting"
              :disabled="serviceState !== 'online' || !draft.preferredSerialNumber"
              @click="connectSelectedCamera"
            >
              <template #icon><PoweroffOutlined /></template>
              连接相机
            </a-button>
            <a-button v-else danger block :loading="disconnecting" @click="confirmDisconnect">
              <template #icon><PoweroffOutlined /></template>
              断开相机
            </a-button>
          </div>
        </section>

        <section class="control-section">
          <div class="section-heading">
            <ScanOutlined />
            <strong>采集配置</strong>
          </div>
          <div class="switch-row">
            <div><strong>纹理点云</strong><span>同时保存带颜色的 PLY</span></div>
            <a-switch v-model:checked="draft.textured" />
          </div>
          <div class="switch-row">
            <div><strong>自定义坐标系</strong><span>使用 Viewer 中配置的坐标系</span></div>
            <a-switch v-model:checked="draft.customReferenceFrame" />
          </div>
          <label class="field-label">最多保存点数</label>
          <a-input-number
            v-model:value="draft.maxPoints"
            class="full-number-input"
            :min="0"
            :step="50000"
            :precision="0"
            placeholder="0 表示全部点"
          />
          <div class="field-meta">0 表示全部；正整数表示均匀抽样后的最大点数</div>

          <details class="advanced-settings">
            <summary>单次采集参数</summary>
            <div class="switch-row compact-switch">
              <div><strong>深度范围</strong><span>仅保留指定距离（mm）</span></div>
              <a-switch v-model:checked="draft.depthRangeEnabled" size="small" />
            </div>
            <div v-if="draft.depthRangeEnabled" class="two-column-fields">
              <label><span>下限</span><a-input-number v-model:value="draft.depthMinMm" :min="0" :precision="0" /></label>
              <label><span>上限</span><a-input-number v-model:value="draft.depthMaxMm" :min="1" :precision="0" /></label>
            </div>

            <div class="switch-row compact-switch">
              <div><strong>3D ROI</strong><span>深度图与点云采集区域</span></div>
              <a-switch v-model:checked="draft.roiEnabled" size="small" />
            </div>
            <div v-if="draft.roiEnabled" class="two-column-fields">
              <label><span>X</span><a-input-number v-model:value="draft.roiX" :min="0" :precision="0" /></label>
              <label><span>Y</span><a-input-number v-model:value="draft.roiY" :min="0" :precision="0" /></label>
              <label><span>宽度</span><a-input-number v-model:value="draft.roiWidth" :min="1" :precision="0" /></label>
              <label><span>高度</span><a-input-number v-model:value="draft.roiHeight" :min="1" :precision="0" /></label>
            </div>

            <div class="switch-row compact-switch">
              <div><strong>3D 曝光序列</strong><span>HDR 可填写多个曝光时间</span></div>
              <a-switch v-model:checked="draft.exposureEnabled" size="small" />
            </div>
            <a-input
              v-if="draft.exposureEnabled"
              v-model:value="draft.exposureSequence"
              class="setting-control"
              placeholder="例如：5, 10"
              suffix="ms"
            />

            <label class="setting-select"><span>表面平滑</span><a-select v-model:value="draft.surfaceSmoothing" :options="processingOptions" /></label>
            <label class="setting-select"><span>噪声去除</span><a-select v-model:value="draft.noiseRemoval" :options="processingOptions" /></label>
            <label class="setting-select"><span>离群点去除</span><a-select v-model:value="draft.outlierRemoval" :options="processingOptions" /></label>
            <label class="setting-select"><span>边缘保留</span><a-select v-model:value="draft.edgePreservation" :options="edgeOptions" /></label>
            <p class="scope-note">以上参数只对本次采集生效，完成后恢复相机原值。</p>
          </details>
          <div class="capture-grid">
            <a-button
              v-for="action in captureActions"
              :key="action.kind"
              :loading="captureBusy === action.kind"
              :disabled="!isConnected || Boolean(captureBusy)"
              @click="runCapture(action.kind)"
            >
              <template #icon><component :is="action.icon" /></template>
              {{ action.label }}
            </a-button>
          </div>
        </section>

        <section class="control-section">
          <div class="section-heading">
            <SwapOutlined />
            <strong>坐标转换</strong>
          </div>
          <div class="switch-row">
            <div><strong>生成转换后 PLY</strong><span>原始 PLY 仍会保留</span></div>
            <a-switch v-model:checked="draft.coordinateTransformEnabled" />
          </div>
          <a-radio-group v-model:value="draft.coordinateTransformMode" class="transform-mode" button-style="solid" size="small">
            <a-radio-button value="pose">位姿</a-radio-button>
            <a-radio-button value="matrix">4×4 矩阵</a-radio-button>
          </a-radio-group>

          <template v-if="draft.coordinateTransformMode === 'pose'">
            <div class="coordinate-caption">平移 XYZ（mm）</div>
            <div class="three-column-fields">
              <a-input-number v-model:value="draft.translationX" placeholder="X" />
              <a-input-number v-model:value="draft.translationY" placeholder="Y" />
              <a-input-number v-model:value="draft.translationZ" placeholder="Z" />
            </div>
            <div class="coordinate-caption">旋转 XYZ（°），按 Rz · Ry · Rx 合成</div>
            <div class="three-column-fields">
              <a-input-number v-model:value="draft.rotationX" placeholder="Rx" />
              <a-input-number v-model:value="draft.rotationY" placeholder="Ry" />
              <a-input-number v-model:value="draft.rotationZ" placeholder="Rz" />
            </div>
            <div class="matrix-preview">
              <span v-for="(value, index) in poseMatrix.flat()" :key="index">{{ formatMatrixValue(value) }}</span>
            </div>
          </template>
          <div v-else class="matrix-editor">
            <a-input-number
              v-for="cell in 16"
              :key="cell"
              v-model:value="draft.transformationMatrix[Math.floor((cell - 1) / 4)][(cell - 1) % 4]"
              :controls="false"
            />
          </div>
          <p class="scope-note">矩阵语义：目标坐标 = 矩阵 × 当前点云坐标。</p>
        </section>
      </aside>

      <section class="camera-main">
        <div class="preview-grid">
          <article class="preview-card">
            <header class="preview-head">
              <div><VideoCameraOutlined /><strong>实时视频</strong></div>
              <div class="preview-actions">
                <a-tag :color="streamStatus.color">{{ streamStatus.text }}</a-tag>
                <a-button size="small" :disabled="!streamCanStart()" @click="startStream">
                  <template #icon><PlayCircleOutlined /></template>
                  启动
                </a-button>
                <a-button size="small" :disabled="rtc.state.value === 'idle'" @click="stopStream">
                  <template #icon><PauseCircleOutlined /></template>
                  停止
                </a-button>
              </div>
            </header>
            <div class="video-stage">
              <video ref="videoElement" autoplay muted playsinline :class="{ 'video-visible': rtc.state.value === 'playing' }"></video>
              <div v-if="rtc.state.value !== 'playing'" class="video-placeholder">
                <a-spin v-if="rtc.state.value === 'negotiating'" size="large" />
                <VideoCameraOutlined v-else class="placeholder-icon" />
                <strong>{{ rtc.state.value === 'negotiating' ? '正在协商实时视频' : '实时视频未播放' }}</strong>
                <span v-if="rtc.error.value">{{ rtc.error.value }}</span>
                <span v-else-if="!isConnected">请先从左侧连接相机</span>
                <span v-else>点击“启动”重新建立 WebRTC 连接</span>
              </div>
              <div v-if="health?.rtc_peer_count != null" class="peer-count">服务端 {{ health.rtc_peer_count }} 个连接</div>
            </div>
          </article>

          <article class="preview-card">
            <header class="preview-head">
              <div><CloudOutlined /><strong>点云预览</strong></div>
              <span class="preview-hint">拖拽旋转 · 滚轮缩放 · 右键平移</span>
            </header>
            <div class="cloud-stage">
              <PointCloudViewer :source-url="pointCloudUrl" :source-label="pointCloudLabel" />
            </div>
          </article>
        </div>

        <article class="transform-card">
          <header class="history-head">
            <div><SwapOutlined /><strong>手工坐标转换</strong><span>每行输入 X, Y, Z</span></div>
            <a-button type="primary" size="small" :loading="transformBusy" @click="runPointTransform">执行转换</a-button>
          </header>
          <div class="manual-transform-body">
            <div>
              <label class="field-label">源坐标（mm）</label>
              <a-textarea v-model:value="manualPointsText" :rows="5" placeholder="100, 200, 300&#10;120, 220, 320" />
            </div>
            <div>
              <label class="field-label">转换结果</label>
              <div class="transform-result">
                <a-empty v-if="transformedPoints.length === 0" description="尚未执行转换" :image-style="{ height: '32px' }" />
                <code v-for="(point, index) in transformedPoints" v-else :key="index">
                  {{ index + 1 }}: {{ formatMatrixValue(point.x) }}, {{ formatMatrixValue(point.y) }}, {{ formatMatrixValue(point.z) }}
                </code>
              </div>
            </div>
          </div>
        </article>

        <article class="history-card">
          <header class="history-head">
            <div>
              <HistoryOutlined />
              <strong>最近采集</strong>
              <span>{{ captureHistory.length }} / 20</span>
            </div>
            <a-button v-if="captureHistory.length" type="text" danger size="small" @click="confirmClearHistory">
              <template #icon><DeleteOutlined /></template>
              清空记录
            </a-button>
          </header>

          <a-empty v-if="captureHistory.length === 0" description="暂无采集记录" :image-style="{ height: '48px' }" />
          <div v-else class="history-list">
            <div v-for="capture in captureHistory" :key="capture.localId || `${capture.serviceBaseUrl}:${capture.captureId}`" class="history-item">
              <div class="history-summary">
                <div class="history-icon"><CloudOutlined v-if="pointCloudPath(capture.files)" /><PictureOutlined v-else /></div>
                <div>
                  <strong>{{ captureKindLabels[capture.kind] || capture.kind }}</strong>
                  <span>{{ formatDate(capture.capturedAt) }} · {{ capture.captureId }}</span>
                  <small v-if="capture.pointCloud">
                    点数 {{ capture.pointCloud.saved_points.toLocaleString() }} / {{ capture.pointCloud.original_points.toLocaleString() }}
                  </small>
                  <small :title="capture.serviceBaseUrl">{{ capture.serviceBaseUrl }}</small>
                </div>
              </div>
              <div class="file-actions">
                <a-button v-if="pointCloudPath(capture.files)" size="small" @click="previewCapture(capture)">
                  <template #icon><CloudOutlined /></template>
                  预览
                </a-button>
                <a-button
                  v-for="(path, fileType) in capture.files"
                  :key="fileType"
                  size="small"
                  :loading="downloadBusy === `${capture.localId}:${fileType}`"
                  :disabled="Boolean(downloadBusy)"
                  @click="downloadFile(capture, fileType, path)"
                >
                  <template #icon><DownloadOutlined /></template>
                  {{ fileLabels[fileType] || fileType }}
                </a-button>
                <a-button type="text" danger size="small" aria-label="删除记录" @click="removeCapture(capture)">
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </div>
            </div>
          </div>
        </article>
      </section>
    </div>
  </main>
</template>

<style scoped>
.camera-page {
  min-height: calc(100vh - 66px);
  padding: 20px 24px 28px;
}

.camera-page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  max-width: 1680px;
  margin: 0 auto 16px;
}

.page-title {
  display: flex;
  align-items: center;
  gap: 14px;
}

.page-title-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 48px;
  height: 48px;
  color: #fff;
  font-size: 22px;
  background: linear-gradient(135deg, var(--accent), var(--accent-5));
  border-radius: 16px;
  box-shadow: 0 13px 28px rgba(53, 120, 255, 0.24);
}

.page-kicker {
  color: var(--accent);
  font-size: 10px;
  font-weight: 900;
  letter-spacing: 0.13em;
}

.page-title h1 {
  margin: 1px 0 2px;
  font-size: 23px;
  line-height: 1.15;
}

.page-title p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}

.page-head-actions {
  display: flex;
  align-items: center;
  gap: 9px;
}

.camera-warning {
  max-width: 1680px;
  margin: 0 auto 14px;
}

.camera-workspace {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  gap: 16px;
  max-width: 1680px;
  margin: 0 auto;
}

.camera-controls,
.preview-card,
.history-card,
.transform-card {
  background: rgba(255, 255, 255, 0.78);
  border: 1px solid rgba(255, 255, 255, 0.82);
  box-shadow: var(--shadow-sm);
  backdrop-filter: blur(18px);
}

.camera-controls {
  align-self: start;
  overflow: hidden;
  border-radius: var(--radius-lg);
}

.control-section {
  padding: 16px;
  border-bottom: 1px solid var(--line);
}

.control-section:last-child { border-bottom: 0; }

.section-heading,
.preview-head,
.history-head,
.history-head > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-heading {
  margin-bottom: 13px;
  color: var(--text);
}

.section-heading > :first-child { color: var(--accent); }
.section-spacer { flex: 1; }
.field-label { display: block; margin-bottom: 6px; color: var(--muted); font-size: 12px; font-weight: 700; }
.field-meta { margin: 6px 0 11px; overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.compact-alert { margin: 0 0 10px; font-size: 11px; }
.camera-select { width: 100%; }
.connection-actions { margin-top: 12px; }

.connected-camera {
  margin-top: 12px;
  padding: 11px;
  background: linear-gradient(135deg, rgba(234, 255, 244, 0.9), rgba(234, 243, 255, 0.78));
  border: 1px solid rgba(22, 163, 106, 0.18);
  border-radius: 12px;
}

.connected-camera-top { display: flex; align-items: center; gap: 7px; }
.online-dot { width: 8px; height: 8px; background: var(--success); border-radius: 50%; box-shadow: 0 0 0 4px rgba(22, 163, 106, 0.12); }
.connected-camera dl { margin: 9px 0 0; }
.connected-camera dl div { display: flex; justify-content: space-between; gap: 10px; padding-top: 4px; font-size: 11px; }
.connected-camera dt { color: var(--muted); }
.connected-camera dd { margin: 0; overflow: hidden; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }

.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.switch-row strong, .switch-row span { display: block; }
.switch-row strong { font-size: 12px; }
.switch-row span { margin-top: 2px; color: var(--muted); font-size: 10px; }

.capture-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding-top: 4px;
}

.capture-grid :deep(.ant-btn) { padding-inline: 8px; font-size: 12px; }
.full-number-input { width: 100%; }

.advanced-settings {
  margin: 12px 0;
  padding: 10px;
  background: rgba(247, 249, 253, 0.82);
  border: 1px solid var(--line);
  border-radius: 10px;
}

.advanced-settings summary {
  cursor: pointer;
  color: var(--text);
  font-size: 12px;
  font-weight: 800;
}

.advanced-settings[open] summary { margin-bottom: 12px; }
.compact-switch { margin-top: 10px; margin-bottom: 8px; }
.setting-control { margin-bottom: 10px; }

.two-column-fields,
.three-column-fields {
  display: grid;
  gap: 7px;
  margin-bottom: 10px;
}

.two-column-fields { grid-template-columns: 1fr 1fr; }
.three-column-fields { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.two-column-fields label { min-width: 0; }
.two-column-fields label > span { display: block; margin-bottom: 3px; color: var(--muted); font-size: 10px; }
.two-column-fields :deep(.ant-input-number),
.three-column-fields :deep(.ant-input-number) { width: 100%; }

.setting-select {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  color: var(--muted);
  font-size: 11px;
}

.scope-note { margin: 9px 0 0; color: var(--muted); font-size: 10px; line-height: 1.5; }
.transform-mode { display: flex; margin-bottom: 12px; }
.transform-mode :deep(.ant-radio-button-wrapper) { flex: 1; text-align: center; }
.coordinate-caption { margin: 9px 0 5px; color: var(--muted); font-size: 10px; font-weight: 700; }

.matrix-preview,
.matrix-editor {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 4px;
  margin-top: 10px;
}

.matrix-preview span {
  overflow: hidden;
  padding: 4px 2px;
  color: #53627a;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 9px;
  text-align: center;
  text-overflow: ellipsis;
  background: rgba(231, 237, 248, 0.8);
  border-radius: 4px;
}

.matrix-editor :deep(.ant-input-number) { width: 100%; }
.matrix-editor :deep(.ant-input-number-input) { padding-inline: 3px; font-size: 10px; text-align: center; }
.camera-main { min-width: 0; }
.preview-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }

.preview-card {
  min-width: 0;
  overflow: hidden;
  border-radius: var(--radius-lg);
}

.preview-head {
  justify-content: space-between;
  min-height: 52px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}

.preview-head > div { display: flex; align-items: center; gap: 8px; }
.preview-head > div > :first-child { color: var(--accent); }
.preview-actions { flex-wrap: wrap; justify-content: flex-end; }
.preview-hint { color: var(--muted); font-size: 11px; }
.video-stage, .cloud-stage { position: relative; height: clamp(340px, 42vh, 520px); overflow: hidden; }
.video-stage { background: #07101f; }
.cloud-stage { background: #fff; }

.video-stage video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.video-stage video.video-visible { opacity: 1; }

.video-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 24px;
  color: #e8f0ff;
  text-align: center;
  background: radial-gradient(circle at 50% 45%, rgba(53, 120, 255, 0.17), transparent 42%);
}

.video-placeholder span { max-width: 390px; color: #9eb2d1; font-size: 12px; }
.placeholder-icon { color: #74b9ff; font-size: 40px; }
.peer-count { position: absolute; right: 12px; bottom: 12px; padding: 5px 9px; color: #c5d3e9; font-size: 10px; background: rgba(8, 18, 34, 0.72); border-radius: 999px; }

.history-card {
  margin-top: 16px;
  overflow: hidden;
  border-radius: var(--radius-lg);
}

.transform-card {
  margin-top: 16px;
  overflow: hidden;
  border-radius: var(--radius-lg);
}

.manual-transform-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  padding: 14px 16px 16px;
}

.transform-result {
  height: 118px;
  overflow: auto;
  padding: 9px 11px;
  background: rgba(247, 249, 253, 0.9);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.transform-result code { display: block; padding: 2px 0; color: #42526a; font-size: 11px; }

.history-head {
  justify-content: space-between;
  min-height: 52px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line);
}

.history-head > div > :first-child { color: var(--accent); }
.history-head span { color: var(--muted); font-size: 11px; }
.history-list { max-height: 360px; overflow: auto; }

.history-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}

.history-item:last-child { border-bottom: 0; }
.history-summary { display: flex; align-items: center; gap: 11px; min-width: 260px; }
.history-icon { display: grid; place-items: center; flex: 0 0 auto; width: 36px; height: 36px; color: var(--accent); background: var(--accent-soft); border-radius: 11px; }
.history-summary strong, .history-summary span, .history-summary small { display: block; }
.history-summary span { margin-top: 2px; color: var(--muted); font-size: 11px; }
.history-summary small { max-width: 380px; margin-top: 2px; overflow: hidden; color: #98a2b3; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.file-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }

@media (max-width: 1250px) {
  .preview-grid { grid-template-columns: 1fr; }
  .video-stage, .cloud-stage { height: 430px; }
}

@media (max-width: 840px) {
  .camera-page { padding: 16px; }
  .camera-page-head { align-items: flex-start; }
  .page-head-actions { flex-wrap: wrap; justify-content: flex-end; }
  .camera-workspace { grid-template-columns: 1fr; }
  .camera-controls { width: 100%; }
}

@media (max-width: 640px) {
  .camera-page { padding: 12px; }
  .camera-page-head { display: block; }
  .page-head-actions { justify-content: flex-start; margin-top: 12px; }
  .page-title p { display: none; }
  .page-title h1 { font-size: 20px; }
  .preview-head { align-items: flex-start; }
  .preview-hint { display: none; }
  .preview-actions { gap: 5px !important; }
  .video-stage, .cloud-stage { height: 300px; }
  .history-item { align-items: flex-start; flex-direction: column; }
  .history-summary { min-width: 0; width: 100%; }
  .history-summary small { max-width: 68vw; }
  .file-actions { justify-content: flex-start; width: 100%; }
  .manual-transform-body { grid-template-columns: 1fr; }
}
</style>
