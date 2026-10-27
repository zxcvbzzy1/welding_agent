import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchPointCloudBuffer, pointCloudRequest, validatePlyHeader } from './pointCloud.js'

const ply = 'ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 1 2\n3 4 5\n'
const buffer = value => new TextEncoder().encode(value).buffer
const session = { pageUrl: 'https://im.test/chat', token: 'test-token' }

test('validates ASCII, binary and CRLF headers before parsing', () => {
  for (const format of ['ascii', 'binary_little_endian', 'binary_big_endian']) {
    assert.equal(validatePlyHeader(buffer(ply.replace('ascii', format))), 2)
  }
  assert.equal(validatePlyHeader(buffer(ply.replaceAll('\n', '\r\n'))), 2)
  assert.throws(() => validatePlyHeader(buffer('<html>login</html>')), /不是 PLY/)
  assert.throws(() => validatePlyHeader(buffer(ply.replace('end_header', 'missing'))), /文件头/)
  assert.throws(() => validatePlyHeader(buffer(ply.replace('vertex 2', 'vertex 0'))), /非空/)
  assert.throws(() => validatePlyHeader(buffer(ply.replace('float z', 'float intensity'))), /坐标/)
})

test('IM downloads carry auth; camera and other origins never receive the token', () => {
  assert.deepEqual(pointCloudRequest('/api/im/point-clouds/id', session), {
    url: 'https://im.test/api/im/point-clouds/id', headers: { Authorization: 'Bearer test-token' },
  })
  for (const url of ['http://camera.test/api/v1/files/cloud.ply', 'https://evil.test/api/im/point-clouds/id', 'https://im.test/cloud.ply']) {
    assert.deepEqual(pointCloudRequest(url, session).headers, {})
  }
  assert.deepEqual(pointCloudRequest('/api/im/files/id/download', { ...session, token: '' }).headers, {})
})

test('resolves configured API origin and deployment prefix without sending auth to the frontend', () => {
  const config = { ...session, apiBaseUrl: 'https://api.test/backend/' }
  const request = pointCloudRequest('/api/im/files/id/download', config)
  assert.equal(request.url, 'https://api.test/backend/api/im/files/id/download')
  assert.equal(request.headers.Authorization, 'Bearer test-token')
  assert.deepEqual(pointCloudRequest('https://im.test/api/im/files/id/download', config).headers, {})
  assert.throws(() => pointCloudRequest('file:///Users/example/cloud.ply', session), /本地文件/)
})

test('downloads through authenticated endpoint and rejects HTML success responses', async () => {
  const controller = new AbortController()
  let requested
  const data = await fetchPointCloudBuffer('/api/im/files/id/download', {
    ...session, signal: controller.signal,
    fetchImpl: async (url, options) => { requested = { url, ...options }; return new Response(ply) },
  })
  assert.equal(requested.headers.Authorization, 'Bearer test-token')
  assert.equal(requested.signal, controller.signal)
  assert.equal(validatePlyHeader(data), 2)
  await assert.rejects(fetchPointCloudBuffer('/bad.ply', {
    ...session, fetchImpl: async () => new Response('<html>SPA fallback</html>'),
  }), /不是 PLY/)
})

test('reports HTTP and network failures while preserving cancellation', async () => {
  for (const [status, detail] of [[401, '未登录'], [404, '点云产物不存在'], [410, '文件已删除']]) {
    await assert.rejects(fetchPointCloudBuffer('/api/im/files/id/download', {
      ...session, fetchImpl: async () => Response.json({ detail }, { status }),
    }), new RegExp(detail))
  }
  await assert.rejects(fetchPointCloudBuffer('/cloud.ply', {
    ...session, fetchImpl: async () => { throw new TypeError('Failed to fetch') },
  }), /文件服务是否可达/)
  const abort = new DOMException('aborted', 'AbortError')
  await assert.rejects(fetchPointCloudBuffer('/cloud.ply', {
    ...session, fetchImpl: async () => { throw abort },
  }), error => error === abort)
})
