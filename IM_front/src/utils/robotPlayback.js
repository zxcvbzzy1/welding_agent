/** Pure playback sampling. Segment boundaries are resets, never invented moves. */
export function playbackSegments(simulation, seamId = '', rate = 1) {
  return (simulation?.seams || []).filter(s => !seamId || s.id === seamId).flatMap(seam =>
    (seam.segments || []).map(segment => {
      let time = 0
      const source = seam.points.slice(segment.start, segment.end + 1)
      const points = source.map((point, i) => {
        if (i) time += Math.max((point.time_s - source[i - 1].time_s) / rate, point.minimum_duration_s || 0, .02)
        return { ...point, time_s: time }
      })
      return { seamId: seam.id, points, duration: Math.max(time, 0.1) }
    }),
  )
}

export function samplePlayback(segments, elapsed, loop = true) {
  const duration = segments.reduce((sum, s) => sum + s.duration, 0)
  if (!segments.length || duration <= 0) return null
  const finished = !loop && elapsed >= duration
  let time = loop ? ((elapsed % duration) + duration) % duration : Math.max(0, Math.min(elapsed, duration))
  let segment = segments.at(-1)
  for (const candidate of segments) {
    if (time < candidate.duration || candidate === segments.at(-1)) { segment = candidate; break }
    time -= candidate.duration
  }
  const points = segment.points
  if (!points.length) return null
  let index = 0
  while (index + 1 < points.length && points[index + 1].time_s <= time) index++
  const a = points[index], b = points[Math.min(index + 1, points.length - 1)]
  const delta = b.time_s - a.time_s
  const alpha = delta > 0 ? Math.min(1, Math.max(0, (time - a.time_s) / delta)) : 0
  return { joints: a.joints.map((v, i) => v + (b.joints[i] - v) * alpha),
    seamId: segment.seamId, segmentIndex: segments.indexOf(segment), pointIndex: index, alpha,
    stage: alpha > 0 ? b.stage : a.stage, finished, duration,
    progress: finished ? 1 : (loop ? (elapsed % duration) / duration : elapsed / duration) }
}
