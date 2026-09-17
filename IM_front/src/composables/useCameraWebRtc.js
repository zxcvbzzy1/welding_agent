import { readonly, ref } from 'vue'
import {
  cameraRtcPeerUrl,
  closeRtcPeer,
  createRtcAnswer,
  getRtcConfiguration,
} from '@/api/camera'

function waitForIceGathering(connection, timeoutMs = 10000) {
  if (connection.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      connection.removeEventListener('icegatheringstatechange', listener)
      resolve()
    }
    const listener = () => {
      if (connection.iceGatheringState === 'complete') finish()
    }
    const timer = window.setTimeout(finish, timeoutMs)
    connection.addEventListener('icegatheringstatechange', listener)
  })
}

function logSdpCandidates(label, sdp) {
  const candidates = String(sdp || '')
    .split(/\r?\n/)
    .filter((line) => line.startsWith('a=candidate:'))

  console.group(`[WebRTC] ${label} SDP candidates (${candidates.length})`)
  if (candidates.length) {
    candidates.forEach((candidate) => console.log(candidate))
  } else {
    console.warn(`完整 ${label} SDP 中没有 a=candidate 行`)
  }
  console.groupEnd()
}

export function useCameraWebRtc(videoRef) {
  const state = ref('idle')
  const error = ref('')
  const peerId = ref('')
  let connection = null
  let connectedBaseUrl = ''
  let generation = 0

  function detachVideo() {
    if (!videoRef.value) return
    const stream = videoRef.value.srcObject
    if (stream instanceof MediaStream) {
      for (const track of stream.getTracks()) track.stop()
    }
    videoRef.value.srcObject = null
  }

  async function stop({ notifyServer = true } = {}) {
    generation += 1
    const previousConnection = connection
    const previousPeerId = peerId.value
    const previousBaseUrl = connectedBaseUrl
    connection = null
    peerId.value = ''
    connectedBaseUrl = ''

    detachVideo()
    if (previousConnection) previousConnection.close()
    state.value = 'idle'
    error.value = ''

    if (notifyServer && previousPeerId && previousBaseUrl) {
      try {
        await closeRtcPeer(previousBaseUrl, previousPeerId)
      } catch {
        // The server also reaps disconnected peers; local teardown must still succeed.
      }
    }
  }

  async function start(baseUrl) {
    await stop()
    const token = ++generation
    state.value = 'negotiating'
    error.value = ''

    let rtcConfiguration
    try {
      rtcConfiguration = await getRtcConfiguration(baseUrl)
    } catch (reason) {
      if (token !== generation) return
      state.value = 'error'
      error.value = reason?.response?.data?.detail
        || reason?.message
        || '无法读取 WebRTC ICE 配置'
      return
    }
    if (token !== generation) return

    const pc = new RTCPeerConnection(rtcConfiguration)
    connection = pc
    connectedBaseUrl = baseUrl
    const videoTransceiver = pc.addTransceiver('video', { direction: 'recvonly' })
    const videoCapabilities = RTCRtpReceiver.getCapabilities?.('video')
    const h264Codecs = videoCapabilities?.codecs?.filter(
      (codec) => codec.mimeType.toLowerCase() === 'video/h264',
    ) || []
    if (!h264Codecs.length) {
      pc.close()
      connection = null
      state.value = 'error'
      error.value = '当前浏览器不支持 WebRTC H.264'
      return
    }
    videoTransceiver.setCodecPreferences(h264Codecs)

    pc.ontrack = ({ track, streams, receiver }) => {
      if (token !== generation || !videoRef.value) return
      if ('playoutDelayHint' in receiver) receiver.playoutDelayHint = 0
      if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 0
      videoRef.value.srcObject = streams[0] || new MediaStream([track])
      videoRef.value.play().catch(() => {})
    }
    pc.onconnectionstatechange = () => {
      if (token !== generation) return
      const next = pc.connectionState
      if (next === 'connected') {
        state.value = 'playing'
        error.value = ''
      } else if (next === 'failed') {
        state.value = 'error'
        error.value = 'WebRTC ICE 连接失败，请检查 STUN/TURN 配置及 UDP/防火墙策略'
      } else if (next === 'disconnected') {
        state.value = 'disconnected'
        error.value = '实时视频连接已断开'
      } else if (next === 'closed' && connection === pc) {
        state.value = 'idle'
      }
    }

    try {
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      await waitForIceGathering(pc)
      if (token !== generation || !pc.localDescription) return

      logSdpCandidates('OFFER', pc.localDescription.sdp)

      const answer = await createRtcAnswer(baseUrl, pc.localDescription)
      if (token !== generation) {
        if (answer.peer_id) closeRtcPeer(baseUrl, answer.peer_id).catch(() => {})
        return
      }
      peerId.value = answer.peer_id
      logSdpCandidates('ANSWER', answer.sdp)
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type })
    } catch (reason) {
      if (token !== generation) return
      state.value = 'error'
      error.value = reason?.response?.data?.detail || reason?.message || 'WebRTC 协商失败'
      pc.close()
      connection = null
      if (peerId.value) {
        closeRtcPeer(baseUrl, peerId.value).catch(() => {})
        peerId.value = ''
      }
    }
  }

  function dispose() {
    generation += 1
    const id = peerId.value
    const baseUrl = connectedBaseUrl
    if (connection) connection.close()
    connection = null
    peerId.value = ''
    connectedBaseUrl = ''
    detachVideo()
    if (id && baseUrl) {
      fetch(cameraRtcPeerUrl(baseUrl, id), {
        method: 'DELETE',
        keepalive: true,
      }).catch(() => {})
    }
  }

  return {
    state: readonly(state),
    error: readonly(error),
    peerId: readonly(peerId),
    start,
    stop,
    dispose,
  }
}
