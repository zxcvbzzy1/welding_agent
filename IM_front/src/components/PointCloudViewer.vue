<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { parse } from '@loaders.gl/core'
import { PLYLoader } from '@loaders.gl/ply'
import plyWorkerUrl from '@loaders.gl/ply/ply-worker.js?url'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { AimOutlined } from '@ant-design/icons-vue'

const MAX_PREVIEW_POINTS = 100000

const props = defineProps({
  sourceUrl: { type: String, default: '' },
  sourceLabel: { type: String, default: '' },
})

const emit = defineEmits(['loaded', 'error'])
const host = ref(null)
const status = ref('idle')
const errorText = ref('')
const originalPointCount = ref(0)
const renderedPointCount = ref(0)
const pointSize = ref(1.5)

let renderer = null
let scene = null
let camera = null
let controls = null
let resizeObserver = null
let animationFrame = 0
let points = null
let grid = null
let axes = null
let activeRequest = 0
let abortController = null

const countLabel = computed(() => {
  if (!originalPointCount.value) return ''
  const original = originalPointCount.value.toLocaleString('zh-CN')
  const rendered = renderedPointCount.value.toLocaleString('zh-CN')
  return originalPointCount.value === renderedPointCount.value
    ? `${rendered} 点`
    : `${rendered} / ${original} 点`
})

function resize() {
  if (!renderer || !camera || !host.value) return
  const { clientWidth, clientHeight } = host.value
  if (!clientWidth || !clientHeight) return
  renderer.setSize(clientWidth, clientHeight, false)
  camera.aspect = clientWidth / clientHeight
  camera.updateProjectionMatrix()
}

function animate() {
  animationFrame = requestAnimationFrame(animate)
  controls?.update()
  if (renderer && scene && camera) renderer.render(scene, camera)
}

function clearCloud() {
  if (points) {
    scene?.remove(points)
    points.geometry.dispose()
    points.material.dispose()
    points = null
  }
  if (grid) {
    scene?.remove(grid)
    grid.geometry.dispose()
    grid.material.dispose()
    grid = null
  }
  if (axes) {
    scene?.remove(axes)
    axes.geometry.dispose()
    axes.material.dispose()
    axes = null
  }
  originalPointCount.value = 0
  renderedPointCount.value = 0
}

function initializeScene() {
  if (!host.value || renderer) return
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.setClearColor(0xffffff, 1)
  host.value.appendChild(renderer.domElement)

  scene = new THREE.Scene()
  camera = new THREE.PerspectiveCamera(48, 1, 0.01, 1000000)
  camera.position.set(300, 240, 500)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.08
  controls.screenSpacePanning = true

  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(host.value)
  resize()
  animate()
}

function sampleAttribute(source, itemSize, stride, previewCount) {
  const ResultType = source.constructor
  const sampled = new ResultType(previewCount * itemSize)
  let targetIndex = 0
  for (let sourceIndex = 0; sourceIndex < source.length / itemSize; sourceIndex += stride) {
    for (let offset = 0; offset < itemSize; offset += 1) {
      sampled[targetIndex * itemSize + offset] = source[sourceIndex * itemSize + offset]
    }
    targetIndex += 1
  }
  return targetIndex === previewCount ? sampled : sampled.slice(0, targetIndex * itemSize)
}

function fitView() {
  if (!points || !camera || !controls) return
  points.geometry.computeBoundingSphere()
  const radius = Math.max(points.geometry.boundingSphere?.radius || 1, 1)
  const distance = (radius / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) * 1.25
  camera.near = Math.max(radius / 10000, 0.001)
  camera.far = Math.max(radius * 100, 1000)
  camera.position.set(distance * 0.45, distance * 0.35, distance)
  camera.up.set(0, 1, 0)
  camera.updateProjectionMatrix()
  controls.target.set(0, 0, 0)
  controls.update()
}

function addHelpers(radius) {
  const size = Math.max(radius * 2.4, 10)
  grid = new THREE.GridHelper(size, 12, 0x6485ad, 0xcbd7e6)
  grid.rotation.x = Math.PI / 2
  grid.position.z = -radius * 0.12
  grid.material.transparent = true
  grid.material.opacity = 0.48
  scene.add(grid)

  axes = new THREE.AxesHelper(Math.max(radius * 0.65, 5))
  scene.add(axes)
}

function updatePointSize() {
  if (points) points.material.size = Number(pointSize.value)
}

async function loadCloud() {
  const sourceUrl = props.sourceUrl
  const requestId = ++activeRequest
  abortController?.abort()
  abortController = null
  clearCloud()
  errorText.value = ''

  if (!sourceUrl) {
    status.value = 'idle'
    return
  }

  status.value = 'loading'
  const controller = new AbortController()
  abortController = controller

  try {
    const response = await fetch(sourceUrl, { signal: controller.signal })
    if (!response.ok) {
      let detail = ''
      try {
        const body = await response.json()
        detail = body?.detail || ''
      } catch {
        // File responses are not guaranteed to be JSON.
      }
      throw new Error(detail || `点云文件加载失败（HTTP ${response.status}）`)
    }
    const buffer = await response.arrayBuffer()
    const mesh = await parse(buffer, PLYLoader, {
      core: { worker: true, reuseWorkers: false },
      ply: { workerUrl: plyWorkerUrl },
    })
    if (requestId !== activeRequest) return

    const positions = mesh?.attributes?.POSITION?.value
    if (!positions?.length) throw new Error('PLY 文件中没有可显示的点坐标')
    const colors = mesh?.attributes?.COLOR_0?.value
    const total = Math.floor(positions.length / 3)
    const stride = Math.max(1, Math.ceil(total / MAX_PREVIEW_POINTS))
    const previewCount = Math.ceil(total / stride)
    const previewPositions = stride === 1
      ? positions
      : sampleAttribute(positions, 3, stride, previewCount)
    const previewColors = colors?.length >= total * 3
      ? (stride === 1 ? colors : sampleAttribute(colors, 3, stride, previewCount))
      : null

    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(previewPositions, 3))
    if (previewColors) {
      const normalized = previewColors instanceof Uint8Array || previewColors instanceof Uint8ClampedArray
      geometry.setAttribute('color', new THREE.BufferAttribute(previewColors, 3, normalized))
    }
    geometry.center()
    geometry.computeBoundingSphere()

    const material = new THREE.PointsMaterial({
      color: previewColors ? 0xffffff : 0x74b9ff,
      size: Number(pointSize.value),
      sizeAttenuation: false,
      vertexColors: Boolean(previewColors),
    })
    points = new THREE.Points(geometry, material)
    scene.add(points)
    addHelpers(geometry.boundingSphere?.radius || 1)

    originalPointCount.value = total
    renderedPointCount.value = previewPositions.length / 3
    status.value = 'ready'
    fitView()
    emit('loaded', { original: total, rendered: renderedPointCount.value })
  } catch (reason) {
    if (reason?.name === 'AbortError' || requestId !== activeRequest) return
    status.value = 'error'
    errorText.value = reason?.message || '点云解析失败'
    emit('error', errorText.value)
  } finally {
    if (abortController === controller) abortController = null
  }
}

onMounted(() => {
  initializeScene()
  loadCloud()
})

watch(() => props.sourceUrl, loadCloud)

onBeforeUnmount(() => {
  activeRequest += 1
  abortController?.abort()
  resizeObserver?.disconnect()
  cancelAnimationFrame(animationFrame)
  clearCloud()
  controls?.dispose()
  renderer?.dispose()
  renderer?.domElement?.remove()
  controls = null
  renderer = null
  scene = null
  camera = null
})
</script>

<template>
  <div class="point-cloud-viewer">
    <div ref="host" class="point-cloud-canvas"></div>

    <div v-if="status === 'idle'" class="viewer-overlay">
      <AimOutlined class="overlay-icon" />
      <strong>等待点云</strong>
      <span>采集点云或从历史记录中选择预览</span>
    </div>
    <div v-else-if="status === 'loading'" class="viewer-overlay">
      <a-spin size="large" />
      <strong>正在加载并解析点云</strong>
      <span>大文件将在后台线程处理</span>
    </div>
    <div v-else-if="status === 'error'" class="viewer-overlay viewer-overlay--error">
      <strong>点云预览失败</strong>
      <span>{{ errorText }}</span>
    </div>

    <div v-if="status === 'ready'" class="viewer-toolbar">
      <span class="viewer-label" :title="sourceLabel">{{ sourceLabel || '点云预览' }}</span>
      <span class="viewer-count">{{ countLabel }}</span>
      <label class="point-size-control">
        点大小
        <input v-model="pointSize" type="range" min="1" max="5" step="0.25" @input="updatePointSize" />
      </label>
      <button type="button" class="viewer-reset" @click="fitView">重置视角</button>
    </div>
  </div>
</template>

<style scoped>
.point-cloud-viewer {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  overflow: hidden;
  background: #fff;
}

.point-cloud-canvas {
  width: 100%;
  height: 100%;
}

.point-cloud-canvas :deep(canvas) {
  display: block;
  width: 100%;
  height: 100%;
}

.viewer-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 24px;
  color: #26364d;
  text-align: center;
  pointer-events: none;
  background: radial-gradient(circle at 50% 45%, rgba(53, 120, 255, 0.10), transparent 42%);
}

.viewer-overlay span {
  max-width: 360px;
  color: #718096;
  font-size: 12px;
}

.viewer-overlay--error strong {
  color: #c9362b;
}

.overlay-icon {
  color: #3578ff;
  font-size: 36px;
}

.viewer-toolbar {
  position: absolute;
  right: 12px;
  bottom: 12px;
  left: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 8px 10px;
  color: #e9f1ff;
  background: rgba(8, 18, 34, 0.78);
  border: 1px solid rgba(151, 179, 222, 0.22);
  border-radius: 10px;
  backdrop-filter: blur(10px);
}

.viewer-label {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.viewer-count {
  flex: 0 0 auto;
  color: #9eb3d3;
  font-size: 11px;
}

.point-size-control {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  color: #afc0da;
  font-size: 11px;
  white-space: nowrap;
}

.point-size-control input {
  width: 72px;
  accent-color: #5a9cff;
}

.viewer-reset {
  padding: 4px 9px;
  color: #e9f1ff;
  font-size: 11px;
  background: rgba(83, 143, 235, 0.22);
  border: 1px solid rgba(116, 185, 255, 0.35);
  border-radius: 7px;
  cursor: pointer;
}

@media (max-width: 640px) {
  .point-cloud-viewer { min-height: 280px; }
  .viewer-count, .point-size-control { display: none; }
  .viewer-reset { margin-left: auto; }
}
</style>
