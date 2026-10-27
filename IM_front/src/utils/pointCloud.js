const MAX_HEADER_BYTES = 64 * 1024

export function validatePlyHeader(buffer) {
  const text = new TextDecoder('ascii').decode(new Uint8Array(buffer, 0, Math.min(buffer.byteLength, MAX_HEADER_BYTES)))
  if (!/^ply\r?\n/.test(text)) {
    throw new Error('下载内容不是 PLY 点云，可能返回了网页或登录页面，请检查文件地址')
  }
  const end = /^end_header\r?\n/m.exec(text)
  if (!end) throw new Error('PLY 文件头不完整或超过 64 KiB')
  const lines = text.slice(0, end.index).split(/\r?\n/)
  if (!lines.some(line => /^format (ascii|binary_little_endian|binary_big_endian) 1\.0$/.test(line))) {
    throw new Error('不支持的 PLY 文件格式')
  }
  let element = ''
  let count = 0
  const properties = new Set()
  for (const line of lines) {
    const values = line.trim().split(/\s+/)
    if (values[0] === 'element') {
      element = values[1]
      if (element === 'vertex') count = Number(values[2])
    } else if (element === 'vertex' && values[0] === 'property' && values.length === 3) {
      properties.add(values[2])
    }
  }
  if (!Number.isSafeInteger(count) || count <= 0 || !['x', 'y', 'z'].every(axis => properties.has(axis))) {
    throw new Error('PLY 文件必须包含非空 vertex 点集及 x、y、z 坐标')
  }
  return count
}

export function pointCloudRequest(sourceUrl, { pageUrl, apiBaseUrl = '', token = '' }) {
  const page = new URL(pageUrl)
  const apiBase = new URL(apiBaseUrl || page.origin, page)
  // Match axios baseURL semantics for relative IM routes, including a deployment prefix.
  const url = new URL(sourceUrl.startsWith('/api/im/') && apiBaseUrl
    ? `${apiBase.href.replace(/\/$/, '')}${sourceUrl}`
    : sourceUrl, page)
  if (!['http:', 'https:', 'blob:', 'data:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('无效的点云文件地址；本地文件请通过 file_id 或 file_path 创建预览')
  }
  const apiPrefix = `${apiBase.pathname.replace(/\/$/, '')}/api/im/`
  const isApi = url.origin === apiBase.origin && url.pathname.startsWith(apiPrefix)
  // Camera URLs may point to another service. Never send the IM token to them.
  return { url: url.href, headers: isApi && token ? { Authorization: `Bearer ${token}` } : {} }
}

export async function fetchPointCloudBuffer(sourceUrl, { signal, fetchImpl = globalThis.fetch, ...options }) {
  const { url, headers } = pointCloudRequest(sourceUrl, options)
  let response
  try {
    response = await fetchImpl(url, { signal, headers })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new Error('点云文件下载失败，请检查文件服务是否可达及跨域配置')
  }
  if (!response.ok) {
    let detail
    try { detail = (await response.json())?.detail } catch { /* Non-JSON error response. */ }
    throw new Error(typeof detail === 'string' && detail
      ? detail
      : `点云文件加载失败（HTTP ${response.status}）`)
  }
  const buffer = await response.arrayBuffer()
  validatePlyHeader(buffer)
  return buffer
}
