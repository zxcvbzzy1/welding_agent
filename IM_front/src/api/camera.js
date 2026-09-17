import axios from 'axios'

const CONTROL_TIMEOUT = 15000
const CAPTURE_TIMEOUT = 120000

export function normalizeCameraBaseUrl(value) {
  const raw = String(value || '').trim()
  if (!raw) throw new Error('请输入相机微服务地址')

  let parsed
  try {
    parsed = new URL(raw)
  } catch {
    throw new Error('相机微服务地址格式不正确')
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('相机微服务地址仅支持 HTTP 或 HTTPS')
  }
  if (parsed.username || parsed.password) {
    throw new Error('相机微服务地址不能包含用户名或密码')
  }
  parsed.hash = ''
  parsed.search = ''
  return parsed.toString().replace(/\/$/, '')
}

function request(baseURL, config, timeout = CONTROL_TIMEOUT) {
  return axios.request({
    baseURL: normalizeCameraBaseUrl(baseURL),
    timeout,
    ...config,
  }).then((response) => response.data)
}

export function cameraErrorMessage(error, fallback = '相机服务请求失败') {
  if (axios.isCancel(error)) return '请求已取消'
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg).filter(Boolean).join('；') || fallback
  }
  if (error?.code === 'ECONNABORTED') return '相机服务响应超时'
  return error?.message || fallback
}

export function getCameraHealth(baseURL) {
  return request(baseURL, { method: 'get', url: '/health' })
}

export function discoverCameras(baseURL) {
  return request(baseURL, { method: 'get', url: '/api/v1/cameras' })
}

export function getCurrentCamera(baseURL) {
  return request(baseURL, { method: 'get', url: '/api/v1/cameras/current' })
}

export function connectCamera(baseURL, serialNumber) {
  return request(baseURL, {
    method: 'post',
    url: '/api/v1/cameras/connect',
    data: { serial_number: serialNumber },
  }, CAPTURE_TIMEOUT)
}

export function disconnectCamera(baseURL) {
  return request(baseURL, { method: 'post', url: '/api/v1/cameras/disconnect' }, CAPTURE_TIMEOUT)
}

export function captureCamera(baseURL, kind, options = {}) {
  const routes = {
    '2d': '/api/v1/captures/2d',
    depth: '/api/v1/captures/depth',
    'point-cloud': '/api/v1/captures/point-cloud',
    all: '/api/v1/captures/all',
  }
  const url = routes[kind]
  if (!url) throw new Error(`不支持的采集类型：${kind}`)

  const config = { method: 'post', url }
  if (kind === 'point-cloud' || kind === 'all') {
    config.data = {
      textured: Boolean(options.textured),
      custom_reference_frame: Boolean(options.customReferenceFrame),
      max_points: Math.max(0, Math.trunc(Number(options.maxPoints) || 0)),
      parameters: options.parameters || {},
      transformation: options.transformation || null,
    }
  }
  return request(baseURL, config, CAPTURE_TIMEOUT)
}

export function transformCameraPoints(baseURL, points, transformation) {
  return request(baseURL, {
    method: 'post',
    url: '/api/v1/transforms/points',
    data: { points, transformation },
  })
}

export function createRtcAnswer(baseURL, description) {
  return request(baseURL, {
    method: 'post',
    url: '/api/v1/rtc/offer',
    data: { sdp: description.sdp, type: description.type },
  }, 30000)
}

export function getRtcConfiguration(baseURL) {
  return request(baseURL, {
    method: 'get',
    url: '/api/v1/rtc/config',
  })
}

export function closeRtcPeer(baseURL, peerId) {
  return request(baseURL, {
    method: 'delete',
    url: `/api/v1/rtc/peers/${encodeURIComponent(peerId)}`,
  }, 5000)
}

export function cameraFileUrl(baseURL, relativePath) {
  const safePath = String(relativePath || '')
    .split('/')
    .filter(Boolean)
    .map((part) => encodeURIComponent(part))
    .join('/')
  return `${normalizeCameraBaseUrl(baseURL)}/api/v1/files/${safePath}`
}

export function cameraRtcPeerUrl(baseURL, peerId) {
  return `${normalizeCameraBaseUrl(baseURL)}/api/v1/rtc/peers/${encodeURIComponent(peerId)}`
}

export async function downloadCameraFile(baseURL, relativePath) {
  const response = await axios.get(cameraFileUrl(baseURL, relativePath), {
    responseType: 'blob',
    timeout: 0,
  })
  return {
    blob: response.data,
    filename: String(relativePath).split('/').pop() || 'capture',
  }
}
