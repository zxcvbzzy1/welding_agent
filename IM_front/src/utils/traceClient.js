// One metadata subscription per expanded run. Bodies never enter the scope event store.
export class TraceClient {
  constructor({ api, openStream, eventNames, changed = () => {} }) {
    Object.assign(this, { api, openStream, eventNames, changed })
    this.runs = new Map()
    this.details = new Map()
    this.epoch = 0
  }
  session(runId) {
    if (!this.runs.has(runId)) this.runs.set(runId, { events: new Map(), views: new Map(), source: null })
    return this.runs.get(runId)
  }
  async expand(event) {
    if (!event.run_id) return
    const session = this.session(event.run_id)
    const key = event.event_id
    const view = session.views.get(key) || { event, open: false, limit: 100, cursor: undefined, loading: false, error: false }
    session.views.set(key, view)
    view.open = true
    if (!session.source) {
      const source = this.openStream(event.run_id)
      session.source = source
      const consume = (raw) => {
        if (session.source !== source) return
        const item = JSON.parse(raw.data)
        if (item.version === 2 && item.event_id && item.name !== 'llm.delta') {
          session.events.set(item.event_id, item)
          this.changed()
        }
      }
      source.addEventListener('stream.restore', raw => {
        if (session.source !== source) return
        for (const item of JSON.parse(raw.data)) session.events.set(item.event_id, item)
        this.changed()
      })
      source.onmessage = consume
      for (const name of this.eventNames) source.addEventListener(name, consume)
      source.addEventListener('stream.reset', () => {
        if (session.source !== source) return
        this.epoch++
        session.events.clear()
        this.details.clear()
        for (const run of this.runs.values()) for (const view of run.views.values()) {
          if (view.loading) { view.loading = false; view.cursor = undefined }
        }
        for (const view of session.views.values()) view.cursor = undefined
        this.changed()
      })
      session.streamError = false
      source.onerror = raw => {
        if (session.source === source) {
          session.streamError = Boolean(raw.terminal)
          this.changed()
        }
      }
    }
    if (view.cursor === undefined || view.error) await this.load(event)
    this.changed()
  }
  retryStream(event) {
    const session = this.session(event.run_id)
    session.source?.close()
    session.source = null
    return this.expand(event)
  }
  collapse(event) {
    const session = this.runs.get(event.run_id)
    const view = session?.views.get(event.event_id)
    if (view) view.open = false
    if (session && ![...session.views.values()].some(v => v.open)) {
      session.source?.close()
      session.source = null
    }
  }
  async load(event, more = false) {
    const session = this.session(event.run_id)
    const view = session.views.get(event.event_id)
    if (!view || view.loading) return
    const epoch = this.epoch
    view.loading = true
    view.error = false
    this.changed()
    try {
      const result = await this.api.runSummaries(event.run_id, {
        execution_id: event.execution_id || undefined, after: more ? view.cursor : undefined, limit: 100,
      })
      if (epoch !== this.epoch) return
      for (const item of result.items) session.events.set(item.event_id, item)
      view.cursor = result.next_cursor
      if (more) view.limit += 100
    } catch {
      if (epoch === this.epoch) view.error = true
    } finally {
      if (epoch === this.epoch) { view.loading = false; this.changed() }
    }
  }
  state(event) {
    const session = this.runs.get(event.run_id)
    const view = session?.views.get(event.event_id)
    const items = [...(session?.events.values() || [])]
      .filter(item => item.name !== 'llm.call.updated')
      .filter(item => !event.execution_id || item.execution_id === event.execution_id)
      .sort((a, b) => a.created_at - b.created_at || a.event_id.localeCompare(b.event_id))
    return { streamError: session?.streamError, items: items.slice(0, view?.limit || 100), loading: view?.loading, error: view?.error,
      more: Boolean(view?.cursor) || items.length > (view?.limit || 100) }
  }
  async more(event) {
    const view = this.runs.get(event.run_id)?.views.get(event.event_id)
    if (view?.cursor) await this.load(event, true)
    else if (view) { view.limit += 100; this.changed() }
  }
  async detail(event) {
    const key = `${event.category || 'run'}:${event.event_id}`
    const cached = this.details.get(key)
    if (cached && !cached.error) return
    const epoch = this.epoch
    this.details.set(key, { loading: true })
    this.changed()
    try {
      const result = event.category === 'scope'
        ? await this.api.scopeEvent(event.scope_id, event.event_id)
        : await this.api.runEvent(event.run_id, event.event_id)
      if (epoch === this.epoch) this.details.set(key, { item: result.item })
    } catch {
      if (epoch === this.epoch) this.details.set(key, { error: true })
    } finally { if (epoch === this.epoch) this.changed() }
  }
  body(event) { return this.details.get(`${event.category || 'run'}:${event.event_id}`) || {} }
  close() {
    this.epoch++
    for (const session of this.runs.values()) { session.source?.close(); session.source = null }
    this.runs.clear()
    this.details.clear()
    this.changed()
  }
}
