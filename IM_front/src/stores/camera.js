import { defineStore } from 'pinia'

const SETTINGS_KEY = 'agent-im-camera-settings-v1'
const HISTORY_KEY = 'agent-im-camera-captures-v1'
const HISTORY_LIMIT = 20

export const DEFAULT_CAMERA_SETTINGS = Object.freeze({
  serviceBaseUrl: 'http://10.42.0.1:7899',
  preferredSerialNumber: '',
  textured: true,
  customReferenceFrame: false,
  maxPoints: 0,
  depthRangeEnabled: false,
  depthMinMm: 100,
  depthMaxMm: 1000,
  roiEnabled: false,
  roiX: 0,
  roiY: 0,
  roiWidth: 500,
  roiHeight: 500,
  exposureEnabled: false,
  exposureSequence: '5',
  surfaceSmoothing: '',
  noiseRemoval: '',
  outlierRemoval: '',
  edgePreservation: '',
  coordinateTransformEnabled: false,
  coordinateTransformMode: 'pose',
  translationX: 0,
  translationY: 0,
  translationZ: 0,
  rotationX: 0,
  rotationY: 0,
  rotationZ: 0,
  transformationMatrix: [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
  ],
})

const PROCESSING_LEVELS = new Set(['', 'Off', 'Weak', 'Normal', 'Strong'])
const EDGE_LEVELS = new Set(['', 'Sharp', 'Normal', 'Smooth'])

function finiteNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function validMatrix(value) {
  if (!Array.isArray(value) || value.length !== 4) {
    return DEFAULT_CAMERA_SETTINGS.transformationMatrix.map((row) => [...row])
  }
  const matrix = value.map((row, rowIndex) => {
    const fallback = DEFAULT_CAMERA_SETTINGS.transformationMatrix[rowIndex]
    if (!Array.isArray(row) || row.length !== 4) return [...fallback]
    return row.map((cell, columnIndex) => finiteNumber(cell, fallback[columnIndex]))
  })
  return matrix
}

function normalizeSettings(stored = {}) {
  const defaults = DEFAULT_CAMERA_SETTINGS
  return {
    serviceBaseUrl: typeof stored.serviceBaseUrl === 'string' ? stored.serviceBaseUrl : defaults.serviceBaseUrl,
    preferredSerialNumber: typeof stored.preferredSerialNumber === 'string' ? stored.preferredSerialNumber : '',
    textured: typeof stored.textured === 'boolean' ? stored.textured : defaults.textured,
    customReferenceFrame: Boolean(stored.customReferenceFrame),
    maxPoints: Math.max(0, Math.trunc(finiteNumber(stored.maxPoints, defaults.maxPoints))),
    depthRangeEnabled: Boolean(stored.depthRangeEnabled),
    depthMinMm: finiteNumber(stored.depthMinMm, defaults.depthMinMm),
    depthMaxMm: finiteNumber(stored.depthMaxMm, defaults.depthMaxMm),
    roiEnabled: Boolean(stored.roiEnabled),
    roiX: finiteNumber(stored.roiX, defaults.roiX),
    roiY: finiteNumber(stored.roiY, defaults.roiY),
    roiWidth: finiteNumber(stored.roiWidth, defaults.roiWidth),
    roiHeight: finiteNumber(stored.roiHeight, defaults.roiHeight),
    exposureEnabled: Boolean(stored.exposureEnabled),
    exposureSequence: typeof stored.exposureSequence === 'string' ? stored.exposureSequence : defaults.exposureSequence,
    surfaceSmoothing: PROCESSING_LEVELS.has(stored.surfaceSmoothing) ? stored.surfaceSmoothing : '',
    noiseRemoval: PROCESSING_LEVELS.has(stored.noiseRemoval) ? stored.noiseRemoval : '',
    outlierRemoval: PROCESSING_LEVELS.has(stored.outlierRemoval) ? stored.outlierRemoval : '',
    edgePreservation: EDGE_LEVELS.has(stored.edgePreservation) ? stored.edgePreservation : '',
    coordinateTransformEnabled: Boolean(stored.coordinateTransformEnabled),
    coordinateTransformMode: stored.coordinateTransformMode === 'matrix' ? 'matrix' : 'pose',
    translationX: finiteNumber(stored.translationX, defaults.translationX),
    translationY: finiteNumber(stored.translationY, defaults.translationY),
    translationZ: finiteNumber(stored.translationZ, defaults.translationZ),
    rotationX: finiteNumber(stored.rotationX, defaults.rotationX),
    rotationY: finiteNumber(stored.rotationY, defaults.rotationY),
    rotationZ: finiteNumber(stored.rotationZ, defaults.rotationZ),
    transformationMatrix: validMatrix(stored.transformationMatrix),
  }
}

function safeRead(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null')
    return value ?? fallback
  } catch {
    return fallback
  }
}

function loadSettings() {
  const stored = safeRead(SETTINGS_KEY, {})
  if (!stored || typeof stored !== 'object' || Array.isArray(stored)) {
    return normalizeSettings()
  }
  return normalizeSettings(stored)
}

function loadHistory() {
  const stored = safeRead(HISTORY_KEY, [])
  if (!Array.isArray(stored)) return []
  return stored
    .filter((item) => item && typeof item === 'object'
      && typeof item.captureId === 'string'
      && typeof item.serviceBaseUrl === 'string'
      && item.files && typeof item.files === 'object')
    .slice(0, HISTORY_LIMIT)
}

export const useCameraStore = defineStore('camera', {
  state: () => ({
    settings: loadSettings(),
    captureHistory: loadHistory(),
  }),
  actions: {
    saveSettings(settings) {
      const next = normalizeSettings(settings)
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(next))
      this.settings = next
    },
    addCapture(capture) {
      const uniqueKey = `${capture.serviceBaseUrl}:${capture.captureId}:${capture.kind}`
      const history = [capture, ...this.captureHistory.filter((item) => (
        `${item.serviceBaseUrl}:${item.captureId}:${item.kind}` !== uniqueKey
      ))].slice(0, HISTORY_LIMIT)
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history))
      this.captureHistory = history
    },
    removeCapture(capture) {
      const history = this.captureHistory.filter((item) => item.localId !== capture.localId)
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history))
      this.captureHistory = history
    },
    clearCaptureHistory() {
      localStorage.removeItem(HISTORY_KEY)
      this.captureHistory = []
    },
  },
})
