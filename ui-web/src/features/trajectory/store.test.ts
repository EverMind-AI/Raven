// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { setCurrent } from '../../lib/session'
import { absorb, forget, gone, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, pitch, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type {
  TrajectoryChangesResult, TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult, TrajectorySource,
} from './types'

/* ── a scripted source ─────────────────────────────────────────────── */

interface Deferred<T> {
  promise: Promise<T>
  resolve(value: T): void
  reject(reason: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const entry = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id,
  revision: 1,
  kind: 'tool.output',
  span_name: 'tool.call',
  slot: 'tool.output',
  trace_id: 't',
  span_id: id,
  parent_span_id: null,
  turn_span_id: 'turn',
  turn_number: 1,
  turn_start: false,
  origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0],
  event_time: '2026-01-01T00:00:00Z',
  preview: id,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: null,
  operation_end: null,
  duration_ms: null,
  charged_ms: null,
  timing_basis: 'not_recorded',
  duration_owner: null,
  meta: {},
  ...over,
})

const page = (entries: TrajectoryEntry[], next: string | null, over: Partial<TrajectoryListResult> = {}): TrajectoryListResult => ({
  epoch: 'e1', snapshot_revision: 100, entries, next_cursor: next, index_state: READY, complete: true, ...over,
})

const batch = (over: Partial<TrajectoryChangesResult> = {}): TrajectoryChangesResult => ({
  epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [], removed: [], has_more: false,
  reset_required: false, index_state: READY, ...over,
})

/* Pages are answered from a queue of deferreds, so a case decides when each
   lands and in what order relative to everything else. */
let listCalls: Array<{ key: string; cursor: string | null }> = []
let listQueue: Array<Deferred<TrajectoryListResult>> = []
let stateAnswer: () => Promise<{ enabled: boolean; policy_revision: number; recording_enabled: boolean }>

const source: TrajectorySource = {
  state: () => stateAnswer(),
  list: (key, cursor) => {
    listCalls.push({ key, cursor: cursor ?? null })
    const d = deferred<TrajectoryListResult>()
    listQueue.push(d)
    return d.promise
  },
  changes: () => Promise.reject(new Error('not scripted')),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
}

const flush = async (): Promise<void> => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }

/** Answer the oldest unanswered page. */
const answer = async (p: TrajectoryListResult): Promise<void> => { listQueue.shift()!.resolve(p); await flush() }
const fail = async (e: unknown): Promise<void> => { listQueue.shift()!.reject(e); await flush() }

/* A whole six-page snapshot of 1,200 rows, answered in order. */
async function answerAll(total = 1200, per = 200): Promise<void> {
  for (let i = 0; i < total; i += per) {
    const rows = Array.from({ length: per }, (_, k) => entry(`r${i + k}`, i + k))
    const last = i + per >= total
    await answer(page(rows, last ? null : `c${i + per}`))
  }
}

function serve(enabled = true): void {
  absorb(['trajectory-v1'])
  stateAnswer = async () => ({ enabled, policy_revision: 1, recording_enabled: true })
}

beforeEach(() => {
  listCalls = []
  listQueue = []
  stateAnswer = async () => ({ enabled: true, policy_revision: 1, recording_enabled: true })
  store._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
})

afterEach(() => {
  resetSources()
  setCurrent(null)
  document.body.innerHTML = ''
})

/* ── whether the toggle may show ──────────────────────────────────── */

describe('available', () => {
  it('needs the surface, the switch, an open conversation and content in it', async () => {
    expect(store.available()).toBe(false)
    serve()
    await store.refreshState()
    expect(store.available()).toBe(false)
    store.install()
    store.sessionChanged('gui:a')
    /* The key is set but the column is in its empty state: no toggle. */
    expect(store.get().fresh).toBe(true)
    expect(store.available()).toBe(false)
    unpitch()
    expect(store.available()).toBe(true)
    pitch()
    expect(store.available()).toBe(false)
  })

  it('leaves the trajectory view when the conversation is cleared under it, and drops the walk', async () => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    store.setView('trajectory')
    const walk = store.load()
    await flush()
    const g = store.gen()
    pitch()
    expect(store.available()).toBe(false)
    expect(store.get().view).toBe('chat')
    expect(store.get().listing).toBe(false)
    expect(store.gen()).toBeGreaterThan(g)
    /* The page that was in the air lands on nothing. */
    await answer(page([entry('r0', 0)], null))
    await walk
    expect(store.get().entries).toEqual([])
    expect(store.get().snapshotReady).toBe(false)
    /* Content again: the toggle is back, the view stays where the reader left it. */
    unpitch()
    expect(store.available()).toBe(true)
    expect(store.get().view).toBe('chat')
  })

  it('goes with the switch and comes back with it, in the conversation view', async () => {
    serve(false)
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    expect(store.available()).toBe(false)
    serve(true)
    await store.refreshState()
    expect(store.available()).toBe(true)
  })
})

/* ── the surface's verdicts ───────────────────────────────────────── */

describe('refreshState', () => {
  beforeEach(() => {
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
  })

  it('leaves the trajectory view when the switch goes off, keeping the rows', async () => {
    serve()
    await store.refreshState()
    store.setView('trajectory')
    void store.load()
    await answerAll(200)
    expect(store.get().entries).toHaveLength(200)
    stateAnswer = async () => ({ enabled: false, policy_revision: 2, recording_enabled: true })
    await store.refreshState()
    expect(store.get().view).toBe('chat')
    expect(store.get().enabled).toBe(false)
    expect(store.get().entries).toHaveLength(200)
  })

  it('reads -32020 as the switch and -32601 as the surface being absent', async () => {
    serve()
    stateAnswer = () => Promise.reject(new RpcError(-32020, 'off'))
    await store.refreshState()
    expect(store.get().enabled).toBe(false)
    expect(store.get().stateKnown).toBe(true)
    stateAnswer = () => Promise.reject(new RpcError(-32601, 'no'))
    await store.refreshState()
    expect(store.get().served).toBe(false)
    /* Remembered: the next read does not ask at all. */
    let asked = 0
    stateAnswer = async () => { asked += 1; return { enabled: true, policy_revision: 1, recording_enabled: true } }
    await store.refreshState()
    expect(asked).toBe(0)
    expect(store.get().served).toBe(false)
  })

  it('comes back after a refusal when a new handshake announces the surface', async () => {
    serve()
    gone('trajectory', new RpcError(-32601, 'no'))
    await store.refreshState()
    expect(store.get().served).toBe(false)
    forget('trajectory')
    store.handshake()
    await store.refreshState()
    expect(store.get().served).toBe(true)
    expect(store.get().enabled).toBe(true)
  })

  it('drops an answer from before the handshake, so an old refusal cannot close the new one', async () => {
    serve()
    const slow = deferred<{ enabled: boolean; policy_revision: number; recording_enabled: boolean }>()
    stateAnswer = () => slow.promise
    const pending = store.refreshState()
    store.handshake()
    serve()
    slow.reject(new RpcError(-32601, 'no'))
    await pending
    expect(store.get().served).toBe(false)
    expect(store.get().stateKnown).toBe(false)
    await store.refreshState()
    expect(store.get().served).toBe(true)
  })
})

/* ── reading the snapshot ─────────────────────────────────────────── */

describe('load', () => {
  beforeEach(async () => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    store.setView('trajectory')
  })

  it('reads every page, previewing as it goes and committing only at the end', async () => {
    const walk = store.load()
    await flush()
    expect(listCalls).toEqual([{ key: 'gui:a', cursor: null }])
    await answer(page(Array.from({ length: 200 }, (_, k) => entry(`r${k}`, k)), 'c200'))
    let s = store.get()
    expect(s.entries).toHaveLength(200)
    expect(s.listing).toBe(true)
    expect(s.snapshotReady).toBe(false)
    expect(s.epoch).toBeNull()
    expect(s.revision).toBe(0)
    /* A batch landing mid-walk is refused: there is no whole snapshot to put it on. */
    store.applyChanges(batch({ upserts: [entry('x', 5000)] }))
    expect(store.get().entries).toHaveLength(200)
    for (let i = 200; i < 1200; i += 200) {
      expect(listCalls[listCalls.length - 1]).toEqual({ key: 'gui:a', cursor: `c${i}` })
      const rows = Array.from({ length: 200 }, (_, k) => entry(`r${i + k}`, i + k))
      await answer(page(rows, i + 200 >= 1200 ? null : `c${i + 200}`))
    }
    await walk
    s = store.get()
    expect(s.entries).toHaveLength(1200)
    expect(s.listing).toBe(false)
    expect(s.snapshotReady).toBe(true)
    expect(s.epoch).toBe('e1')
    expect(s.revision).toBe(100)
    expect(s.complete).toBe(true)
    expect(s.selectedId).toBeNull()
    expect(listCalls).toHaveLength(6)
  })

  it('starts the walk over on an expired cursor, and gives up after three restarts', async () => {
    const walk = store.load()
    await flush()
    await answer(page([entry('r0', 0)], 'c1'))
    expect(store.get().entries).toHaveLength(1)
    for (let i = 0; i < store.WALK_RETRIES; i += 1) {
      await fail(new RpcError(-32022, 'expired'))
      expect(listCalls[listCalls.length - 1]).toEqual({ key: 'gui:a', cursor: null })
      await answer(page([entry('r0', 0), entry('r1', 1)], 'c2'))
    }
    await fail(new RpcError(-32022, 'expired'))
    await walk
    const s = store.get()
    expect(s.listing).toBe(false)
    expect(s.snapshotReady).toBe(false)
    expect(s.epoch).toBeNull()
    expect(s.fault).toBe('expired')
    /* The preview stays up; the restarts kept the first picture rather than shrinking it. */
    expect(s.entries).toHaveLength(1)
    /* A read again brings the whole snapshot and commits it. */
    const again = store.load()
    await flush()
    await answerAll(400)
    await again
    expect(store.get().entries).toHaveLength(400)
    expect(store.get().snapshotReady).toBe(true)
    expect(store.get().fault).toBeNull()
  })

  it('keeps the first page as a preview when the second fails, and commits on the next read', async () => {
    const walk = store.load()
    await flush()
    await answer(page(Array.from({ length: 200 }, (_, k) => entry(`r${k}`, k)), 'c200'))
    await fail(new Error('socket closed'))
    await walk
    let s = store.get()
    expect(s.entries).toHaveLength(200)
    expect(s.snapshotReady).toBe(false)
    expect(s.listing).toBe(false)
    expect(s.fault).toBe('socket closed')
    const again = store.load()
    await flush()
    await answerAll(400)
    await again
    s = store.get()
    expect(s.entries).toHaveLength(400)
    expect(s.snapshotReady).toBe(true)
    expect(s.revision).toBe(100)
  })

  it('keeps a remembered selection that the snapshot still holds, and clears one it does not', async () => {
    void store.load()
    await flush()
    await answerAll(200)
    store.select('r7', { source: 'click' })
    store.sessionChanged('gui:b')
    store.sessionChanged('gui:a')
    expect(store.get().selectedId).toBe('r7')
    const walk = store.load()
    await flush()
    await answerAll(200)
    await walk
    expect(store.get().selectedId).toBe('r7')
    store.sessionChanged('gui:b')
    store.sessionChanged('gui:a')
    const narrower = store.load()
    await flush()
    await answerAll(5, 5)
    await narrower
    expect(store.get().selectedId).toBeNull()
  })
})

/* ── generations ──────────────────────────────────────────────────── */

describe('a stale answer', () => {
  beforeEach(async () => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    store.setView('trajectory')
  })

  it('from a walk cut short by leaving the view writes nothing', async () => {
    const walk = store.load()
    await flush()
    await answer(page([entry('r0', 0)], 'c1'))
    store.setView('chat')
    expect(store.get().listing).toBe(false)
    await answer(page([entry('r1', 1)], null))
    await walk
    const s = store.get()
    expect(s.entries.map((e) => e.entry_id)).toEqual(['r0'])
    expect(s.snapshotReady).toBe(false)
    expect(s.epoch).toBeNull()
  })

  it('from A, after A -> B -> A, writes nothing into the second visit', async () => {
    const first = store.load()
    await flush()
    const late = listQueue.shift()!
    store.sessionChanged('gui:b')
    store.sessionChanged('gui:a')
    const second = store.load()
    await flush()
    late.resolve(page([entry('old', 0)], null))
    await flush()
    await first
    expect(store.get().entries).toEqual([])
    expect(store.get().snapshotReady).toBe(false)
    await answerAll(3, 3)
    await second
    expect(store.get().entries.map((e) => e.entry_id)).toEqual(['r0', 'r1', 'r2'])
    expect(store.get().snapshotReady).toBe(true)
  })

  it('that failed late does not fault the new walk', async () => {
    const first = store.load()
    await flush()
    const late = listQueue.shift()!
    store.setView('chat')
    store.setView('trajectory')
    const second = store.load()
    await flush()
    late.reject(new Error('old socket'))
    await first
    expect(store.get().fault).toBeNull()
    expect(store.get().listing).toBe(true)
    await answerAll(2, 2)
    await second
    expect(store.get().snapshotReady).toBe(true)
  })
})

/* ── the feed ─────────────────────────────────────────────────────── */

describe('applyChanges', () => {
  beforeEach(async () => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    store.setView('trajectory')
    const walk = store.load()
    await flush()
    await answerAll(10, 10)
    await walk
  })

  it('appends, updates and back-fills without touching the selection', () => {
    store.select('r5', { source: 'click' })
    store.applyChanges(batch({ upserts: [entry('r10', 10)], to_revision: 101 }))
    store.applyChanges(batch({ upserts: [entry('r3', 3, { preview: 'changed' })], from_revision: 101, to_revision: 102 }))
    store.applyChanges(batch({ upserts: [entry('r-1', -1)], from_revision: 102, to_revision: 103 }))
    const s = store.get()
    expect(s.selectedId).toBe('r5')
    expect(s.revision).toBe(103)
    expect(s.entries[0]?.entry_id).toBe('r-1')
    expect(s.entries[s.entries.length - 1]?.entry_id).toBe('r10')
    expect(store.entry('r3')?.preview).toBe('changed')
    expect(s.index.r5).toBe(6)
  })

  it('moves an updated row to its new place, forwards or back, and keeps the selection on it', () => {
    store.select('r5', { source: 'click' })
    store.applyChanges(batch({ upserts: [entry('r5', 20)] }))
    expect(store.get().entries.map((e) => e.entry_id).slice(-1)).toEqual(['r5'])
    expect(store.get().selectedId).toBe('r5')
    store.applyChanges(batch({ upserts: [entry('r5', -5)], to_revision: 102 }))
    expect(store.get().entries[0]?.entry_id).toBe('r5')
    expect(store.get().index.r5).toBe(0)
    expect(store.get().selectedId).toBe('r5')
    /* An equal key lands after the row it ties with. */
    store.applyChanges(batch({ upserts: [entry('r4b', 4)], to_revision: 103 }))
    const ids = store.get().entries.map((e) => e.entry_id)
    expect(ids.indexOf('r4b')).toBe(ids.indexOf('r4') + 1)
    expect(store.get().entries).toHaveLength(11)
  })

  it('migrates the selection to a replacement, or clears it', () => {
    store.select('r5', { source: 'click' })
    store.applyChanges(batch({
      removed: [{ entry_id: 'r5', revision: 101, replaced_by: 'r5b' }],
      upserts: [entry('r5b', 5)],
    }))
    expect(store.get().selectedId).toBe('r5b')
    expect(store.entry('r5')).toBeNull()
    store.applyChanges(batch({ removed: [{ entry_id: 'r5b', revision: 102, replaced_by: null }], to_revision: 102 }))
    expect(store.get().selectedId).toBeNull()
  })

  it('refuses a batch for another epoch or a stale snapshot, and a re-read that fails keeps the commitment', async () => {
    store.applyChanges(batch({ epoch: 'e2', upserts: [entry('x', 99)] }))
    expect(store.entry('x')).toBeNull()
    store.snapshotStale()
    store.applyChanges(batch({ upserts: [entry('x', 99)] }))
    expect(store.entry('x')).toBeNull()
    expect(store.get().revision).toBe(100)
    const again = store.load()
    await flush()
    await fail(new Error('down'))
    await again
    const s = store.get()
    expect(s.entries).toHaveLength(10)
    expect(s.epoch).toBe('e1')
    expect(s.revision).toBe(100)
    expect(s.snapshotReady).toBe(false)
    expect(s.fault).toBe('down')
  })

  it('selects only through select', () => {
    expect(store.get().selectedId).toBeNull()
    store.select('nope', { source: 'click' })
    expect(store.get().selectedId).toBeNull()
    store.select('r2', { source: 'keyboard' })
    expect(store.get().selectedId).toBe('r2')
    store.select(null, { source: 'migrate' })
    expect(store.get().selectedId).toBeNull()
  })
})

/* ── conversations ────────────────────────────────────────────────── */

describe('sessionChanged', () => {
  beforeEach(async () => {
    serve()
    store.install()
    unpitch()
    await store.refreshState()
  })

  it('keeps each conversation\'s place, up to twenty of them', async () => {
    store.sessionChanged('gui:a')
    store.setView('trajectory')
    const walk = store.load()
    await flush()
    await answerAll(3, 3)
    await walk
    store.select('r1', { source: 'click' })
    store.setPlace(false, { id: 'r1', offset: 4 })
    store.sessionChanged('gui:b')
    expect(store.get().view).toBe('chat')
    expect(store.get().entries).toEqual([])
    expect(store.get().epoch).toBeNull()
    expect(store.get().snapshotReady).toBe(false)
    expect(store.get().selectedId).toBeNull()
    for (let i = 0; i < store.REMEMBERED_MAX; i += 1) store.sessionChanged(`gui:x${i}`)
    store.sessionChanged('gui:a')
    /* Twenty-one conversations later, the first one's place is gone. */
    expect(store.get().view).toBe('chat')
    expect(store.get().selectedId).toBeNull()
    store.sessionChanged('gui:x5')
    store.setView('trajectory')
    store.select(null, { source: 'migrate' })
    store.sessionChanged('gui:x6')
    store.sessionChanged('gui:x5')
    expect(store.get().view).toBe('trajectory')
    expect(store.get().anchor).toBeNull()
  })

  it('keeps the bar\'s zoom per conversation, but never an open pick list', async () => {
    store.sessionChanged('gui:a')
    store.setView('trajectory')
    const walk = store.load()
    await flush()
    await answerAll(3, 3)
    await walk
    store.setTimeline({ scale: 4, offset: 120, fit: false, frozenUnit: 0.5, anchor: { id: 'r1', frac: 0.25 } })
    store.openBucket(['r0', 'r1'], 10)
    expect(store.get().timeline.bucket).toEqual({ ids: ['r0', 'r1'], x: 10 })
    store.sessionChanged('gui:b')
    expect(store.get().timeline).toEqual(store.initialTimeline)
    store.sessionChanged('gui:a')
    expect(store.get().timeline).toEqual({ scale: 4, offset: 120, fit: false, frozenUnit: 0.5, anchor: { id: 'r1', frac: 0.25 }, bucket: null })
    /* Leaving the view, or losing it, closes the list too. */
    store.setView('trajectory')
    store.openBucket(['r0'], 0)
    store.setView('chat')
    expect(store.get().timeline.bucket).toBeNull()
    store.setView('trajectory')
    store.openBucket(['r0'], 0)
    store.disabledByServer()
    expect(store.get().timeline.bucket).toBeNull()
    store.openBucket([], 0)
    expect(store.get().timeline.bucket).toBeNull()
  })

  it('comes back to the conversation view only while the toggle may show', () => {
    store.sessionChanged('gui:a')
    store.setView('trajectory')
    store.sessionChanged('gui:b')
    pitch()
    store.sessionChanged('gui:a')
    expect(store.get().view).toBe('chat')
  })
})
