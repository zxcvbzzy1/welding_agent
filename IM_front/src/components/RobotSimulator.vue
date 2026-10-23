<script setup>
import { computed, onMounted, onBeforeUnmount, ref, shallowRef, useId, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import URDFLoader from 'urdf-loader'
import { robotUrdfUrl, resolveRobotAsset } from '@/utils/robotAssets'
import http from '@/api/http'
import { playbackSegments, samplePlayback } from '@/utils/robotPlayback'

const props = defineProps({
  sourceUrl: { type: String, default: '' },
  simulation: { type: Object, default: null },
  compact: { type: Boolean, default: false },
  active: { type: Boolean, default: true },
})
const instanceId = useId()
const data = shallowRef(null)
const dataLoading = ref(false)
const dataError = ref('')
const selectedSeam = ref('')
const playing = ref(true)
const loop = ref(true)
const rate = ref(1)
const elapsed = ref(0)
const currentSeam = ref('')
const currentStage = ref('')
const stageLabel = computed(() => ({ APPROACH: '接近', WELDING: '沿缝运动', RETRACT: '退刀' })[currentStage.value] || '')
const playbackTime = computed(() => duration.value ? (loop.value ? elapsed.value % duration.value : Math.min(elapsed.value, duration.value)) : 0)
const resetNotice = ref('')
const playbackMode = computed(() => Boolean(props.sourceUrl || props.simulation))
const segments = computed(() => playbackSegments(data.value, selectedSeam.value, rate.value))
const diagnosticSeams = computed(() => (data.value?.seams || []).filter(s => !selectedSeam.value || s.id === selectedSeam.value)
  .map(s => ({ id: s.id, reason: s.reason, errors: s.points.map((p, i) => ({ reason: p.reason, index: i })).filter(p => p.reason).slice(0, 100) })))
const seamOptions = computed(() => [{ value: '', label: '全部焊缝' }, ...(data.value?.seams || []).map(s => ({ value: s.id, label: s.id + (!s.segments.length ? ' · 无可播放段' : '') }))])
const duration = computed(() => segments.value.reduce((total, s) => total + s.duration, 0))
const selectionSummary = computed(() => {
  const seams = (data.value?.seams || []).filter(s => !selectedSeam.value || s.id === selectedSeam.value)
  return {
    unreachable: seams.reduce((n, s) => n + s.points.filter(p => p.status !== 'reachable').length, 0),
    geometry: seams.filter(s => !s.points.length).length,
    discontinuities: seams.reduce((n, s) => n + s.points.filter(p => p.status === 'reachable' && p.reason).length, 0),
  }
})
const effectivePlaying = computed(() => playing.value && visible.value && documentVisible.value && props.active)
const visible = ref(true)
const documentVisible = ref(!document.hidden)
let intersectionObserver, lastTime = null, lastSegment = '', fetchSequence = 0, fetchAbort, paths, tcpMarker, tcpAxes
function restart() {
  elapsed.value = 0
  lastSegment = ''
  lastTime = null
  currentSeam.value = ''; currentStage.value = ''; resetNotice.value = ''
  playing.value = true
  if (!segments.value.length) resetJoints()
  applyPlayback()
}
function setPlaybackPose(sample) {
  if (!robot || !sample) return
  data.value.joint_names.forEach((name, i) => robot.setJointValue(name, sample.joints[i]))
  currentSeam.value = sample.seamId
  currentStage.value = sample.stage
  const segmentKey = `${sample.seamId}:${sample.segmentIndex}`
  if (lastSegment && lastSegment !== segmentKey) resetNotice.value = '已重置至下一连续段'
  else if (sample.progress > 0) resetNotice.value = ''
  lastSegment = segmentKey
  if (sample.finished) playing.value = false
}
function applyPlayback() {
  if (data.value && segments.value.length) setPlaybackPose(samplePlayback(segments.value, elapsed.value, loop.value))
}
function animate(timestamp) {
  if (effectivePlaying.value && !loading.value && !dataLoading.value && segments.value.length) {
    if (lastTime !== null) elapsed.value += Math.min((timestamp - lastTime) / 1000, .1)
    applyPlayback()
  }
  lastTime = timestamp
  if (robot && tcpMarker && data.value) {
    robot.updateMatrixWorld(true)
    const offset = data.value.tcp_offset_pose
    tcpMarker.position.copy(robot.links.wrist3_link.localToWorld(new THREE.Vector3(...offset.slice(0, 3).map(v => v / 1000))))
  }
  controls?.update()
  renderer?.render(scene, camera)
}
function syncVisibility() {
  lastTime = null
  renderer?.setAnimationLoop(visible.value && documentVisible.value && props.active ? animate : null)
}
function onVisibility() { documentVisible.value = !document.hidden; syncVisibility() }
function drawTrajectory() {
  if (!scene) return
  if (paths) { scene.remove(paths); disposeObject(paths) }
  if (tcpAxes) { tcpAxes.removeFromParent(); disposeObject(tcpAxes); tcpAxes = null }
  paths = new THREE.Group()
  scene.add(paths)
  if (!data.value) return
  for (const seam of data.value.seams.filter(s => !selectedSeam.value || s.id === selectedSeam.value)) {
    const points = seam.points.length ? seam.points.map(p => p.pose.slice(0, 3)) : [seam.start, seam.end]
    const vertices = points.flatMap(p => p.map(v => v / 1000))
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3))
    paths.add(new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: '#148cbb' })))
    const failed = seam.points.filter(p => p.status !== 'reachable').flatMap(p => p.pose.slice(0, 3).map(v => v / 1000))
    if (failed.length) {
      const badGeometry = new THREE.BufferGeometry()
      badGeometry.setAttribute('position', new THREE.Float32BufferAttribute(failed, 3))
      paths.add(new THREE.Points(badGeometry, new THREE.PointsMaterial({ color: '#ef4444', size: .012, depthTest: false })))
    }
  }
  tcpMarker = new THREE.Mesh(new THREE.SphereGeometry(.007, 12, 8), new THREE.MeshBasicMaterial({ color: '#ff9b21', depthTest: false }))
  paths.add(tcpMarker)
  if (robot) {
    const offset = data.value.tcp_offset_pose
    tcpAxes = new THREE.AxesHelper(.08)
    tcpAxes.position.set(...offset.slice(0, 3).map(v => v / 1000))
    tcpAxes.rotation.set(...offset.slice(3).map(THREE.MathUtils.degToRad), 'ZYX')
    robot.links.wrist3_link.add(tcpAxes)
  }
  applyPlayback()
}
async function loadSimulation() {
  const sequence = ++fetchSequence
  fetchAbort?.abort()
  fetchAbort = new AbortController()
  data.value = null; dataError.value = ''; dataLoading.value = playbackMode.value
  selectedSeam.value = ''; elapsed.value = 0; lastSegment = ''; lastTime = null
  drawTrajectory()
  try {
    const value = props.simulation || (props.sourceUrl ? (await http.get(props.sourceUrl, { signal: fetchAbort.signal })).item : null)
    if (disposed || sequence !== fetchSequence) return
    if (value && (value.schema_version !== 1 || value.robot !== 'fairino5_v6' || value.joint_units !== 'radian' || value.joint_names?.length !== 6 || !Array.isArray(value.seams))) throw new Error('不支持的仿真数据')
    data.value = value
    playing.value = true
    drawTrajectory()
    if (robot && !loading.value) resetView()
  } catch (err) {
    if (!disposed && sequence === fetchSequence && !fetchAbort.signal.aborted) dataError.value = '仿真读取失败，请重试或重新生成仿真。'
  } finally { if (!disposed && sequence === fetchSequence) dataLoading.value = false }
}
watch(() => [props.sourceUrl, props.simulation], loadSimulation)
watch(selectedSeam, () => { restart(); drawTrajectory() })
watch(() => props.active, syncVisibility)
watch(playing, () => { lastTime = null })
watch(rate, (_, oldRate) => {
  const previous = samplePlayback(playbackSegments(data.value, selectedSeam.value, oldRate), elapsed.value, loop.value)
  if (previous) {
    const next = segments.value[previous.segmentIndex]
    const a = next.points[previous.pointIndex], b = next.points[Math.min(previous.pointIndex + 1, next.points.length - 1)]
    elapsed.value = segments.value.slice(0, previous.segmentIndex).reduce((t, s) => t + s.duration, 0)
      + a.time_s + previous.alpha * (b.time_s - a.time_s)
  }
  lastTime = null
})

const viewport = ref(null)
const loading = ref(true)
const progress = ref(0)
const error = ref('')
const joints = ref([])
const showGrid = ref(true)
const showAxes = ref(true)
let renderer, scene, camera, controls, robot, grid, axes, observer
let disposed = false
const abort = new AbortController()
function disposeObject(object) {
  object?.traverse(child => {
    child.geometry?.dispose()
    for (const material of (Array.isArray(child.material) ? child.material : [child.material])) {
      if (!material) continue
      for (const value of Object.values(material)) if (value?.isTexture) value.dispose()
      material.dispose()
    }
  })
}
function resize() {
  if (!renderer || !viewport.value) return
  const { clientWidth: width, clientHeight: height } = viewport.value
  renderer.setSize(width, height)
  camera.aspect = width / Math.max(height, 1)
  camera.updateProjectionMatrix()
  if (robot && !loading.value) resetView(false)
}
function resetView(resetDirection = true, includeAll = false) {
  if (!robot) return
  robot.updateMatrixWorld(true)
  const box = new THREE.Box3().setFromObject(robot)
  if (includeAll && paths && data.value) box.union(new THREE.Box3().setFromObject(paths))
  const center = box.getCenter(new THREE.Vector3())
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.25)
  const verticalFov = THREE.MathUtils.degToRad(camera.fov) / 2
  const limitingFov = Math.min(verticalFov, Math.atan(Math.tan(verticalFov) * camera.aspect))
  const distance = radius / Math.sin(limitingFov) * 1.12
  const direction = resetDirection === false
    ? camera.position.clone().sub(controls.target).normalize()
    : new THREE.Vector3(1, -1.25, 0.8).normalize()
  camera.position.copy(center).addScaledVector(direction, distance)
  controls.maxDistance = Math.max(15, distance * 2)
  camera.far = Math.max(100, distance * 4)
  camera.updateProjectionMatrix()
  controls.target.copy(center)
  controls.update()
}
function updateJoint(joint, value) {
  if (!robot || value === null || !Number.isFinite(Number(value))) return
  joint.value = Math.min(joint.max, Math.max(joint.min, Number(value)))
  robot.setJointValue(joint.name, THREE.MathUtils.degToRad(joint.value))
}
function resetJoints() { for (const joint of joints.value) updateJoint(joint, 0) }
function toggleHelpers() {
  if (grid) grid.visible = showGrid.value
  if (axes) axes.visible = showAxes.value
}
onMounted(() => {
  try {
    scene = new THREE.Scene()
    scene.background = new THREE.Color('#edf2f7')
    camera = new THREE.PerspectiveCamera(40, 1, 0.005, 100)
    camera.up.set(0, 0, 1)
    renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    viewport.value.appendChild(renderer.domElement)
    renderer.domElement.setAttribute('aria-label', 'FR5 六轴机械臂三维视图')
    controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.minDistance = 0.15
    controls.maxDistance = 15
    scene.add(new THREE.HemisphereLight(0xffffff, 0x738099, 1.8))
    const light = new THREE.DirectionalLight(0xffffff, 2)
    light.position.set(2, -3, 4)
    scene.add(light)
    grid = new THREE.GridHelper(3, 30, 0xa0aec0, 0xd4dce6)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -0.002
    axes = new THREE.AxesHelper(0.3)
    scene.add(grid, axes)
    observer = new ResizeObserver(resize)
    observer.observe(viewport.value)
    resize()
    intersectionObserver = new IntersectionObserver(entries => {
      visible.value = entries.some(e => e.isIntersecting)
      syncVisibility()
    })
    intersectionObserver.observe(viewport.value)
    document.addEventListener('visibilitychange', onVisibility)
    syncVisibility()
    loadSimulation()
    const manager = new THREE.LoadingManager()
    manager.setURLModifier(resolveRobotAsset)
    manager.onProgress = (_, loaded, total) => { if (!disposed) progress.value = Math.round(loaded / total * 100) }
    manager.onError = () => { if (!disposed) { error.value = '模型资源加载失败，请检查 URDF 和 STL 文件后刷新。'; loading.value = false } }
    manager.onLoad = () => {
      if (disposed || error.value || !robot) return
      loading.value = false
      progress.value = 100
      drawTrajectory()
      resetView()
    }
    const loader = new URDFLoader(manager)
    loader.packages = { fairino_description: '/robot-assets' }
    loader.fetchOptions = { signal: abort.signal }
    const loadMesh = loader.loadMeshCb
    loader.loadMeshCb = (path, manager, material, done) => loadMesh(path, manager, material, (object, err) => {
      if (disposed) disposeObject(object)
      else done(object, err)
    })
    loader.load(robotUrdfUrl, model => {
      if (disposed) { disposeObject(model); return }
      robot = model
      scene.add(robot)
      joints.value = Object.values(robot.joints).filter(j => j.jointType === 'revolute')
        .map(j => ({ name: j.name, min: THREE.MathUtils.radToDeg(j.limit.lower), max: THREE.MathUtils.radToDeg(j.limit.upper), value: 0 }))
    }, undefined, () => {
      if (!disposed) { error.value = 'URDF 加载失败，请刷新重试。'; loading.value = false }
    })
  } catch {
    error.value = '无法创建三维视图，请检查浏览器是否启用 WebGL。'
    loading.value = false
  }
})
onBeforeUnmount(() => {
  disposed = true
  abort.abort()
  fetchAbort?.abort()
  ++fetchSequence
  intersectionObserver?.disconnect()
  document.removeEventListener('visibilitychange', onVisibility)
  observer?.disconnect()
  renderer?.setAnimationLoop(null)
  controls?.dispose()
  disposeObject(scene)
  renderer?.dispose()
  renderer?.domElement.remove()
})
</script>

<template>
  <div class="robot-simulator" :class="{ compact, 'playback-mode': playbackMode }">
    <div v-if="playbackMode" class="playback-controls">
      <a-select v-model:value="selectedSeam" aria-label="选择焊缝" :options="seamOptions" :disabled="dataLoading" />
      <a-button :disabled="!segments.length || loading || !!error" @click="playing = !playing">{{ playing ? '暂停' : '播放' }}</a-button>
      <a-button :disabled="!segments.length" @click="restart">重新开始</a-button>
      <a-select v-model:value="rate" aria-label="播放倍率" :options="[.25,.5,1,2].map(v => ({ value:v, label:`${v}×` }))" />
      <label><a-checkbox v-model:checked="loop" /> 循环播放</label>
      <span class="playback-status">{{ currentSeam }} {{ stageLabel }} · {{ playbackTime.toFixed(1) }} / {{ duration.toFixed(1) }} s</span>
    </div>
    <a-alert v-if="data && !data.tcp_calibrated" type="info" show-icon message="未配置焊枪外参，以法兰作为 TCP" />
    <a-alert v-if="selectionSummary.unreachable || selectionSummary.geometry || selectionSummary.discontinuities" type="warning" show-icon :message="`不可达 ${selectionSummary.unreachable} 点 · 仅几何 ${selectionSummary.geometry} 条 · 关节分段 ${selectionSummary.discontinuities} 处`" />
    <a-alert v-if="dataError" type="error" :message="dataError"><template #action><a-button size="small" @click="loadSimulation">重试</a-button></template></a-alert>
    <a-alert v-if="data && !segments.length" type="info" message="所选焊缝没有可播放的关节段，仅展示几何与诊断。" />
    <div v-if="data" class="playback-note">参考速度 {{ data.speed_mm_s }} mm/s · 橙色为 TCP，红色为不可达点。焊缝与断点之间重置模型，不补充连接运动。{{ resetNotice }}</div>
    <div class="robot-workspace">
      <section class="robot-stage" aria-label="机械臂模型">
        <div ref="viewport" class="robot-viewport" />
        <div class="stage-label"><strong>FR5 / 6 DOF</strong><span>Z ↑ · 米制坐标</span></div>
        <div v-if="loading || dataLoading" class="stage-overlay"><a-spin size="large" /><p>{{ dataLoading ? '正在读取轨迹' : `正在加载模型 ${progress}%` }}</p></div>
        <div v-if="error" class="stage-overlay"><a-alert type="error" show-icon :message="error" /></div>
        <div class="stage-toolbar"><span>拖动旋转 · 滚轮缩放 · 右键平移</span><a-button v-if="data" :disabled="loading || !!error" @click="resetView(true, true)">查看全部轨迹</a-button><a-button :disabled="loading || !!error" @click="resetView()">重置视角</a-button></div>
      </section>
      <aside v-if="!playbackMode" class="robot-panel">
        <div class="panel-heading"><h2>关节姿态</h2><span>角度 / °</span></div>
        <p class="panel-note">调整关节以检查模型姿态，限位读取自 URDF。</p>
        <div v-for="joint in joints" :key="joint.name" class="joint-control">
          <div class="joint-heading"><label :for="`${instanceId}-robot-${joint.name}`">{{ joint.name.toUpperCase() }}</label><a-input-number :id="`${instanceId}-robot-${joint.name}`" :aria-label="`${joint.name.toUpperCase()} 关节角度`" :value="joint.value" :min="joint.min" :max="joint.max" :step="1" :precision="1" :disabled="loading || !!error" @change="value => updateJoint(joint, value)" /></div>
          <a-slider :aria-label="`${joint.name.toUpperCase()} 滑块`" :value="joint.value" :min="joint.min" :max="joint.max" :step="0.1" :disabled="loading || !!error" @change="value => updateJoint(joint, value)" />
          <div class="joint-range"><span>{{ joint.min.toFixed(1) }}°</span><span>{{ joint.max.toFixed(1) }}°</span></div>
        </div>
        <a-button block :disabled="loading || !!error" @click="resetJoints">所有关节归零</a-button>
        <div class="display-options"><h3>场景显示</h3><label>地面网格<a-switch v-model:checked="showGrid" @change="toggleHelpers" /></label><label>基座坐标轴<a-switch v-model:checked="showAxes" @change="toggleHelpers" /></label></div>
      </aside>
    </div>
    <details v-if="data" class="diagnostics"><summary>轨迹诊断</summary>
      <p>每条焊缝最多显示前 100 项诊断，全部结果保存在仿真快照中。</p>
      <div v-for="seam in diagnosticSeams" :key="seam.id">
        <strong>{{ seam.id }}</strong><span v-if="seam.reason"> · {{ seam.reason }}</span>
        <p v-for="point in seam.errors" :key="point.index">点 {{ point.index + 1 }} · {{ point.reason }}</p>
      </div>
    </details>
  </div>
</template>

<style scoped>
.robot-simulator { min-width: 0; }
.playback-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.playback-controls :deep(.ant-select) { min-width: 90px; }
.playback-controls :deep(.ant-select:first-child) { min-width: 170px; }
.playback-status, .playback-note { font-size: 12px; color: #64748b; }
.playback-note { margin: 10px 0; line-height: 1.7; }
.robot-simulator > :deep(.ant-alert) { margin-bottom: 8px; }
.playback-mode .robot-workspace { grid-template-columns: minmax(0, 1fr); }
.compact .robot-stage { height: 430px; min-height: 340px; }
.diagnostics { font-size: 12px; color: #64748b; margin-top: 12px; max-height: 180px; overflow-y: auto; }
.diagnostics summary { cursor: pointer; }
.diagnostics p { margin: 4px 0; }
.robot-heading, .panel-heading, .joint-heading, .display-options label { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.eyebrow { color: #64748b; font-size: 10px; font-weight: 750; letter-spacing: 2px; }
h1 { font-size: 28px; margin: 4px 0; letter-spacing: -1px; } h1 span { margin-left: 14px; font-size: 14px; color: #64748b; font-weight: 500; letter-spacing: 0; }
.robot-heading p, .panel-note { color: #64748b; margin: 4px 0 20px; font-size: 13px; }
.robot-workspace { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 20px; }
.robot-stage { min-width: 0; min-height: 680px; height: calc(100vh - 220px); position: relative; border: 1px solid #dce4ee; border-radius: 20px; overflow: hidden; background: #edf2f7; }
.robot-viewport { position: absolute; inset: 0; } .robot-viewport :deep(canvas) { display: block; }
.stage-label { position: absolute; top: 22px; left: 24px; display: grid; gap: 6px; pointer-events: none; font-size: 12px; letter-spacing: 1px; color: #475569; } .stage-label span { font-size: 11px; color: #64748b; letter-spacing: 0; }
.stage-overlay { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; background: #edf2f7dc; }
.stage-toolbar { position: absolute; bottom: 16px; left: 20px; right: 20px; display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 10px; } .stage-toolbar span { color: #64748b; font-size: 12px; margin-right: auto; }
.robot-panel { background: #ffffffdb; border: 1px solid #e2e8f0; padding: 22px; border-radius: 20px; } h2 { font-size: 16px; margin: 0; } .panel-heading span { font-size: 11px; color: #64748b; } .panel-note { margin-top: 9px; line-height: 1.7; }
.joint-control { margin-bottom: 17px; } .joint-heading label { font-size: 12px; font-weight: 750; color: #334155; } .joint-heading :deep(.ant-input-number) { width: 90px; } .joint-range { display: flex; justify-content: space-between; color: #94a3b8; font-size: 10px; margin-top: -5px; }
.display-options { margin-top: 22px; padding-top: 18px; border-top: 1px solid #e2e8f0; } h3 { font-size: 12px; color: #64748b; margin: 0 0 16px; } .display-options label { margin-top: 14px; color: #475569; font-size: 12px; }
@media (max-width: 900px) { .robot-workspace { grid-template-columns: 1fr; } .robot-stage { min-height: 440px; height: 58vh; } .robot-panel { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 24px; } .panel-heading, .panel-note, .display-options { grid-column: 1 / -1; } h1 span { display: block; margin: 6px 0 0; } .stage-toolbar span { max-width: 180px; } }
</style>
