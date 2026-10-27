import { API_BASE_URL } from './http'
import { fetchPointCloudBuffer } from '../utils/pointCloud'

export function fetchPointCloud(sourceUrl, { signal } = {}) {
  let token = ''
  try {
    token = JSON.parse(localStorage.getItem('agent-im-auth') || 'null')?.token || ''
  } catch { /* A malformed/expired session is reported by the download endpoint. */ }
  return fetchPointCloudBuffer(sourceUrl, {
    signal, token, apiBaseUrl: API_BASE_URL, pageUrl: window.location.href,
  })
}
