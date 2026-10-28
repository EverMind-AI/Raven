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

  it('goes when the gateway stops serving the surface, though the switch it last reported was on', async () => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    expect(store.available()).toBe(true)
    store.absent()
    expect(store.get().enabled).toBe(true)
    expect(store.available()).toBe(false)
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
    store.setTimeline({ scale: 4, offset: 120, fit: false, frozenUnit: 0.5, frozenMark: 30, anchor: { id: 'r1', frac: 0.25 } })
    store.openBucket(['r0', 'r1'], 10)
    store.setThresholdOpen(true)
    expect(store.get().timeline.bucket).toEqual({ ids: ['r0', 'r1'], x: 10 })
    store.sessionChanged('gui:b')
    expect(store.get().timeline).toEqual(store.initialTimeline)
    store.sessionChanged('gui:a')
    expect(store.get().timeline).toEqual({
      scale: 4, offset: 120, fit: false, frozenUnit: 0.5, frozenMark: 30, anchor: { id: 'r1', frac: 0.25 }, bucket: null, threshold: false,
    })
    /* Leaving the view, or losing it, closes the list and the threshold's popover too. */
    store.setView('trajectory')
    store.openBucket(['r0'], 0)
    store.setThresholdOpen(true)
    store.setView('chat')
    expect(store.get().timeline.bucket).toBeNull()
    expect(store.get().timeline.threshold).toBe(false)
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

describe('the switches and the rows they hide', () => {
  const rowsOf = (list: TrajectoryEntry[]): void => { store.set({ ...store.get(), ...store.rowsOf(list), snapshotReady: true, epoch: 'e1' }) }
  const hidden = (id: string, at: number, reason: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    entry(id, at, { meta: { hidden: reason }, ...over })

  beforeEach(() => {
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
  })

  it('keeps every row but shows the ones the index did not hide, and remembers the switch in the browser', () => {
    rowsOf([entry('a', 0), hidden('reply', 1, 'redundant_reply', { slot: 'turn.output', turn_span_id: 'turn', charged_ms: 7000 }), hidden('enq', 2, 'empty_internal'), entry('b', 3)])
    const s = store.get()
    expect(s.entries.map((e) => e.entry_id)).toEqual(['a', 'reply', 'enq', 'b'])
    expect(s.visible.map((e) => e.entry_id)).toEqual(['a', 'b'])
    expect(s.visibleIndex).toEqual({ a: 0, b: 1 })
    expect(s.index.b).toBe(3)
    expect(s.turnTotals).toEqual({ turn: 7000 })
    expect(store.hiddenOf(s.entries[1]!)).toBe('redundant_reply')
    store.setPrefs({ showHidden: true })
    expect(store.get().visible).toHaveLength(4)
    expect(JSON.parse(localStorage.getItem(store.PREFS_KEY) as string)).toEqual({ minChargedMs: 20, showHidden: true })
    store.setPrefs({ showHidden: false })
    expect(store.get().visible).toHaveLength(2)
  })

  it('shows a hidden row for now when it is selected, and forgets that with the conversation, the view or a switch', () => {
    rowsOf([entry('a', 0), hidden('reply', 1, 'redundant_reply'), entry('b', 2)])
    store.select('reply', { source: 'link' })
    expect(store.get().selectedId).toBe('reply')
    expect(store.get().revealed).toEqual(['reply'])
    expect(store.get().visible.map((e) => e.entry_id)).toEqual(['a', 'reply', 'b'])
    store.setPrefs({ minChargedMs: 0 })
    expect(store.get().revealed).toEqual([])
    expect(store.get().visible.map((e) => e.entry_id)).toEqual(['a', 'b'])
    store.select('reply', { source: 'link' })
    expect(store.get().revealed).toEqual(['reply'])
    store.set({ ...store.get(), view: 'trajectory' })
    store.setView('chat')
    expect(store.get().revealed).toEqual([])
    store.select('reply', { source: 'bar' })
    store.sessionChanged('gui:b')
    expect(store.get().revealed).toEqual([])
  })

  it('finds the first row of a span by trace and span id, and nothing for a span it does not hold', () => {
    rowsOf([
      entry('x:1:in', 0, { trace_id: 'x', span_id: '1', slot: 'tool.input' }),
      entry('x:1:out', 1, { trace_id: 'x', span_id: '1' }),
      entry('y:1:out', 2, { trace_id: 'y', span_id: '1' }),
    ])
    expect(store.entryOfSpan('x', '1')).toBe('x:1:in')
    expect(store.entryOfSpan('y', '1')).toBe('y:1:out')
    expect(store.entryOfSpan('z', '1')).toBeNull()
    store.applyChanges({
      epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [],
      removed: [{ entry_id: 'x:1:in', revision: 101, replaced_by: null }], has_more: false, reset_required: false, index_state: READY,
    })
    expect(store.entryOfSpan('x', '1')).toBe('x:1:out')
  })

  it('reads the settings back from the browser on a fresh start, an earlier build\'s switches included', () => {
    localStorage.setItem(store.PREFS_KEY, JSON.stringify({ minChargedMs: 200, showHidden: true }))
    expect(store.readPrefs()).toEqual({ minChargedMs: 200, showHidden: true })
    /* A threshold off the scale lands on the nearest detent. */
    localStorage.setItem(store.PREFS_KEY, JSON.stringify({ minChargedMs: 37, showHidden: false }))
    expect(store.readPrefs()).toEqual({ minChargedMs: 50, showHidden: false })
    /* The two switches an earlier build kept. */
    localStorage.setItem(store.PREFS_KEY, JSON.stringify({ hideShort: false, showInternal: true }))
    expect(store.readPrefs()).toEqual({ minChargedMs: 0, showHidden: true })
    localStorage.setItem(store.PREFS_KEY, JSON.stringify({ hideShort: true, showInternal: false }))
    expect(store.readPrefs()).toEqual({ minChargedMs: 20, showHidden: false })
    localStorage.setItem(store.PREFS_KEY, 'not json')
    expect(store.readPrefs()).toEqual({ minChargedMs: 20, showHidden: false })
    localStorage.removeItem(store.PREFS_KEY)
  })

  it('saves a migrated setting in the current shape, which reads back without the old switches', () => {
    localStorage.setItem(store.PREFS_KEY, JSON.stringify({ hideShort: false, showInternal: true }))
    store.set({ ...store.get(), prefs: store.readPrefs() })
    store.setPrefs({ minChargedMs: 100 })
    const kept = JSON.parse(localStorage.getItem(store.PREFS_KEY) as string) as Record<string, unknown>
    expect(kept).toEqual({ minChargedMs: 100, showHidden: true })
    expect(Object.keys(kept).sort()).toEqual(['minChargedMs', 'showHidden'])
    expect(store.readPrefs()).toEqual({ minChargedMs: 100, showHidden: true })
    /* Setting an off-scale value snaps it, and the same value twice writes nothing new. */
    store.setPrefs({ minChargedMs: 480 })
    expect(store.get().prefs.minChargedMs).toBe(500)
    localStorage.removeItem(store.PREFS_KEY)
  })
})

/* ── the Scope Tabs ───────────────────────────────────────────────── */

describe('Scope Tabs', () => {
  const A = 't:runA'
  const B = 't:runB'
  const G = 't:dag'
  const run = (id: string, at: number, opens: string): TrajectoryEntry =>
    entry(id, at, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: opens })
  const step = (id: string, at: number, scope: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    entry(id, at, { subagent_scope: scope, ...over })
  /* The main session's turn, two spawned runs and an old-format graph whose
     call is two rows, each scope holding steps; a hidden row in two of them
     and the turn's hidden reply in the main session. */
  const ROWS = (): TrajectoryEntry[] => [
    entry('m0', 0, { turn_start: true, kind: 'user.input', slot: 'turn.input' }),
    run('doorA', 1, A),
    run('doorB', 2, B),
    step('a0', 3, A), step('a1', 4, A), step('a2', 5, A), step('a3', 6, A),
    step('ah', 7, A, { meta: { hidden: 'empty_internal' } }),
    entry('m1', 8),
    step('b0', 9, B),
    step('bh', 10, B, { meta: { hidden: 'empty_internal' } }),
    entry('gIn', 11, { kind: 'tool.input', slot: 'tool.input', meta: { tool: 'run_subagent_dag' }, opens_subagent_scope: G }),
    entry('gOut', 12, { meta: { tool: 'run_subagent_dag' }, opens_subagent_scope: G }),
    step('g0', 13, G),
    entry('reply', 14, { kind: 'turn.end', slot: 'turn.output', charged_ms: 5000, meta: { hidden: 'redundant_reply' } }),
  ]
  const ids = (rows: TrajectoryEntry[]): string[] => rows.map((e) => e.entry_id)
  const open = (): string[] => store.get().scopes.map((o) => o.id)

  async function loaded(rows: TrajectoryEntry[] = ROWS()): Promise<void> {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    store.setView('trajectory')
    const walk = store.load()
    await flush()
    await answer(page(rows, null))
    await walk
  }

  it('draws the main session alone at first and knows every scope\'s first door and size', async () => {
    await loaded()
    const s = store.get()
    expect(s.scope).toBeNull()
    expect(ids(s.visible)).toEqual(['m0', 'doorA', 'doorB', 'm1', 'gIn', 'gOut'])
    expect(s.doors).toEqual({ [A]: 'doorA', [B]: 'doorB', [G]: 'gIn' })
    expect(s.scopeSizes).toEqual({ [A]: 5, [B]: 2, [G]: 1 })
    expect(s.hiddenCount).toBe(1)
    expect(s.hiddenTurns).toEqual({ 1: 5000 })
    store.openScope(A)
    const a = store.get()
    expect(ids(a.visible)).toEqual(['a0', 'a1', 'a2', 'a3'])
    expect(a.visibleIndex).toEqual({ a0: 0, a1: 1, a2: 2, a3: 3 })
    expect(a.hiddenCount).toBe(1)
    /* The steps carry the dispatching turn's number, and the turn's reply is hidden, yet the tab has no band. */
    expect(a.hiddenTurns).toEqual({})
    /* The whole set stays for links and the feed. */
    expect(a.index.m0).toBe(0)
    expect(store.entryOfSpan('t', 'b0')).toBe('b0')
    expect(a.turnTotals).toEqual({ turn: 5000 })
  })

  it('opens a tab at the end, says whether it opened it just now, and keeps the door it was opened from', async () => {
    await loaded()
    expect(store.openScope(A)).toBe(true)
    expect(store.openScope(B)).toBe(true)
    expect(open()).toEqual([A, B])
    expect(store.get().scope).toBe(B)
    expect(store.openScope(A)).toBe(false)
    expect(store.get().scope).toBe(A)
    expect(open()).toEqual([A, B])
    expect(store.get().scopes[0]!.door?.entry_id).toBe('doorA')
    store.switchScope(null)
    expect(store.get().scope).toBeNull()
    store.switchScope('t:never-opened')
    expect(store.get().scope).toBeNull()
    /* The door going away leaves the tab with the door it was opened from. */
    store.applyChanges(batch({ removed: [{ entry_id: 'doorA', revision: 101, replaced_by: null }] }))
    expect(store.get().doors[A]).toBeUndefined()
    expect(store.get().scopes[0]!.door?.entry_id).toBe('doorA')
  })

  it('hands a closed tab on screen to its right-hand neighbour, else its left, else the main session', async () => {
    await loaded()
    store.openScope(A)
    store.openScope(B)
    store.openScope(G)
    store.switchScope(B)
    store.closeScope(B)
    expect(store.get().scope).toBe(G)
    expect(open()).toEqual([A, G])
    store.closeScope(G)
    expect(store.get().scope).toBe(A)
    store.closeScope(A)
    expect(store.get().scope).toBeNull()
    expect(open()).toEqual([])
    /* A tab not on screen goes without moving the reader. */
    store.openScope(A)
    store.openScope(B)
    store.closeScope(A)
    expect(store.get().scope).toBe(B)
    expect(open()).toEqual([B])
    store.closeScope('t:never-opened')
    expect(open()).toEqual([B])
  })

  it('opens a closed tab afresh', async () => {
    await loaded()
    store.openScope(A)
    store.select('a1', { source: 'click' })
    store.setPlace(false, { id: 'a1', offset: 3 })
    store.setTimeline({ scale: 4, fit: false })
    store.switchScope(null)
    store.closeScope(A)
    expect(store.get().saved[A]).toBeUndefined()
    store.openScope(A)
    const s = store.get()
    expect([s.selectedId, s.follow, s.anchor]).toEqual([null, true, null])
    expect(s.timeline).toEqual(store.initialTimeline)
  })

  it('keeps each tab\'s selection, place and zoom, but never an open pick list or popover', async () => {
    await loaded()
    store.select('m1', { source: 'click' })
    store.setPlace(false, { id: 'm1', offset: 2 })
    store.setTimeline({ scale: 2, fit: false })
    store.openBucket(['m1'], 10)
    store.setThresholdOpen(true)
    store.openScope(A)
    expect([store.get().selectedId, store.get().follow, store.get().anchor]).toEqual([null, true, null])
    expect(store.get().timeline).toEqual(store.initialTimeline)
    store.select('a2', { source: 'keyboard' })
    store.setPlace(false, { id: 'a2', offset: 4 })
    store.setTimeline({ scale: 3, fit: false })
    store.openScope(B)
    store.select('b0', { source: 'click' })
    store.setPlace(true, null)
    store.switchScope(null)
    let s = store.get()
    expect([s.selectedId, s.selectedBy, s.follow, s.anchor]).toEqual(['m1', 'migrate', false, { id: 'm1', offset: 2 }])
    expect([s.timeline.scale, s.timeline.bucket, s.timeline.threshold]).toEqual([2, null, false])
    store.switchScope(A)
    s = store.get()
    expect([s.selectedId, s.follow, s.anchor, s.timeline.scale]).toEqual(['a2', false, { id: 'a2', offset: 4 }, 3])
    store.switchScope(B)
    s = store.get()
    expect([s.selectedId, s.follow, s.anchor, s.timeline.scale]).toEqual(['b0', true, null, 1])
    expect(Object.keys(s.saved).sort()).toEqual(['', A])
  })

  it('moves to another tab for a row of it, opening the tab when it is not open, through whichever door', async () => {
    await loaded()
    store.select('m1', { source: 'click' })
    const before = store.get().selections
    store.select('a1', { source: 'link' })
    let s = store.get()
    expect([s.scope, s.selectedId, s.selectedBy]).toEqual([A, 'a1', 'link'])
    expect(s.selections).toBe(before + 1)
    expect(s.saved['']?.selectedId).toBe('m1')
    store.select('b0', { source: 'bar' })
    s = store.get()
    expect([s.scope, s.selectedId, s.selectedBy]).toEqual([B, 'b0', 'bar'])
    expect(open()).toEqual([A, B])
    store.select('m0', { source: 'keyboard' })
    s = store.get()
    expect([s.scope, s.selectedId, s.selectedBy]).toEqual([null, 'm0', 'keyboard'])
    /* A row the new tab hides is shown there for now, as in the tab it was asked from. */
    store.select('ah', { source: 'link' })
    s = store.get()
    expect(s.scope).toBe(A)
    expect(s.revealed).toEqual(['ah'])
    expect(ids(s.visible)).toEqual(['a0', 'a1', 'a2', 'a3', 'ah'])
  })

  it('selects within the tab as it always did, whichever the door', async () => {
    await loaded()
    const sources = ['click', 'keyboard', 'bar', 'link', 'migrate'] as const
    const wrong: string[] = []
    sources.forEach((source, k) => {
      const id = k % 2 ? 'm0' : 'm1'
      const before = store.get().selections
      store.select(id, { source })
      const s = store.get()
      if (s.scope !== null || s.selectedId !== id || s.selectedBy !== source || s.selections !== before + 1) wrong.push(source)
    })
    expect(wrong).toEqual([])
    store.select('reply', { source: 'link' })
    expect(store.get().scope).toBeNull()
    expect(store.get().revealed).toEqual(['reply'])
    store.select('nope', { source: 'click' })
    expect(store.get().selectedId).toBe('reply')
  })

  it('forgets the rows a link showed in a tab when the tab closes, and only those', async () => {
    await loaded()
    store.select('ah', { source: 'link' })
    store.select('bh', { source: 'link' })
    expect(store.get().revealed).toEqual(['ah', 'bh'])
    const prefs = store.get().prefs
    store.closeScope(A)
    expect(store.get().revealed).toEqual(['bh'])
    expect(ids(store.get().visible)).toEqual(['b0', 'bh'])
    expect(store.get().prefs).toEqual(prefs)
    store.openScope(A)
    expect(ids(store.get().visible)).toEqual(['a0', 'a1', 'a2', 'a3'])
    /* A shown row the feed moved to another tab is that tab's now. */
    store.select('ah', { source: 'link' })
    store.applyChanges(batch({ upserts: [step('ah', 7, B, { meta: { hidden: 'empty_internal' } })] }))
    store.closeScope(A)
    expect(store.get().revealed).toEqual(['bh', 'ah'])
    expect(store.get().scope).toBe(B)
    expect(ids(store.get().visible)).toEqual(['ah', 'b0', 'bh'])
  })

  it('leaves the generation alone, so a snapshot read in progress still lands', async () => {
    await loaded()
    const walk = store.load()
    await flush()
    await answer(page(ROWS().slice(0, 6), 'c6', { snapshot_revision: 200 }))
    const g = store.gen()
    store.openScope(A)
    store.switchScope(null)
    store.select('b0', { source: 'link' })
    store.closeScope(A)
    expect(store.gen()).toBe(g)
    await answer(page(ROWS().slice(6), null, { snapshot_revision: 200 }))
    await walk
    const s = store.get()
    expect([s.revision, s.snapshotReady, s.listing, s.scope]).toEqual([200, true, false, B])
    expect(ids(s.visible)).toEqual(['b0'])
  })

  it('carries a saved tab\'s selection and anchor across the feed', async () => {
    await loaded()
    store.openScope(A)
    store.select('a1', { source: 'click' })
    store.setPlace(false, { id: 'a1', offset: 5 })
    store.switchScope(null)
    store.applyChanges(batch({ removed: [{ entry_id: 'a1', revision: 101, replaced_by: 'a1b' }], upserts: [step('a1b', 4, A)] }))
    expect(store.get().saved[A]).toMatchObject({ selectedId: 'a1b', anchor: { id: 'a1b', offset: 5 }, follow: false })
    store.applyChanges(batch({ removed: [{ entry_id: 'a1b', revision: 102, replaced_by: null }], from_revision: 101, to_revision: 102 }))
    expect(store.get().saved[A]).toMatchObject({ selectedId: null, anchor: { id: 'a2', offset: 0 }, follow: false })
    /* An anchor the feed moved to another tab is no longer this tab's row. */
    store.applyChanges(batch({ upserts: [step('a2', 5, B)], from_revision: 102, to_revision: 103 }))
    expect(store.get().saved[A]?.anchor).toEqual({ id: 'a3', offset: 0 })
    store.applyChanges(batch({
      removed: [{ entry_id: 'a3', revision: 104, replaced_by: null }, { entry_id: 'ah', revision: 104, replaced_by: null }],
      from_revision: 103, to_revision: 104,
    }))
    expect(store.get().saved[A]).toMatchObject({ anchor: null, follow: true })
  })

  describe('the place of the tab on screen, carried in the same write as the change', () => {
    /* Anchored in A, the feed takes the anchor away, and the reader goes to B and back without scrolling. */
    async function away(
      anchor: string, removed: TrajectoryChangesResult['removed'], upserts: TrajectoryEntry[] = [], first = (): void => {},
    ): Promise<void> {
      await loaded()
      store.openScope(A)
      first()
      store.setPlace(false, { id: anchor, offset: 7 })
      store.applyChanges(batch({ removed, upserts }))
      store.openScope(B)
      store.switchScope(A)
    }

    it('to the replacement, at the same offset', async () => {
      await away('a1', [{ entry_id: 'a1', revision: 101, replaced_by: 'a1b' }], [step('a1b', 4, A)])
      expect([store.get().follow, store.get().anchor]).toEqual([false, { id: 'a1b', offset: 7 }])
    })

    it('to the first row after it in the tab', async () => {
      await away('a1', [{ entry_id: 'a1', revision: 101, replaced_by: null }])
      expect([store.get().follow, store.get().anchor]).toEqual([false, { id: 'a2', offset: 0 }])
    })

    it('to the tail when no row of the tab is left after it', async () => {
      await away('a3', [{ entry_id: 'a3', revision: 101, replaced_by: null }, { entry_id: 'ah', revision: 101, replaced_by: null }])
      expect([store.get().follow, store.get().anchor]).toEqual([true, null])
    })

    it('to the tail when the rows of the tab left after it are all hidden', async () => {
      await away('a3', [{ entry_id: 'a3', revision: 101, replaced_by: null }])
      expect(store.entry('ah')).not.toBeNull()
      expect([store.get().follow, store.get().anchor]).toEqual([true, null])
    })

    it('to a hidden row after it that the switch shows', async () => {
      await away('a3', [{ entry_id: 'a3', revision: 101, replaced_by: null }], [], () => { store.setPrefs({ showHidden: true }) })
      expect([store.get().follow, store.get().anchor]).toEqual([false, { id: 'ah', offset: 0 }])
      store.setPrefs({ showHidden: false })
      localStorage.removeItem(store.PREFS_KEY)
    })

    it('to a hidden row after it that a link showed', async () => {
      await away('a3', [{ entry_id: 'a3', revision: 101, replaced_by: null }], [], () => { store.select('ah', { source: 'link' }) })
      expect([store.get().follow, store.get().anchor]).toEqual([false, { id: 'ah', offset: 0 }])
    })

    it('nowhere while a hidden anchor still has a drawn row of its tab after it', async () => {
      await loaded([step('a0', 0, A), step('ah0', 1, A, { meta: { hidden: 'empty_internal' } }), step('a1', 2, A), entry('m0', 3)])
      store.openScope(A)
      store.setPlace(false, { id: 'ah0', offset: 3 })
      store.applyChanges(batch({ upserts: [entry('m0', 3, { preview: 'changed' })] }))
      expect([store.get().follow, store.get().anchor]).toEqual([false, { id: 'ah0', offset: 3 }])
      store.applyChanges(batch({ removed: [{ entry_id: 'a1', revision: 102, replaced_by: null }], from_revision: 101, to_revision: 102 }))
      expect([store.get().follow, store.get().anchor]).toEqual([true, null])
    })

    it('across a snapshot read again, for the tab on screen and the saved ones', async () => {
      await loaded()
      store.select('m1', { source: 'click' })
      store.setPlace(false, { id: 'm1', offset: 1 })
      store.openScope(A)
      store.setPlace(false, { id: 'a1', offset: 3 })
      const walk = store.load()
      await flush()
      await answer(page(ROWS().filter((e) => e.entry_id !== 'a1' && e.entry_id !== 'm1'), null, { snapshot_revision: 200 }))
      await walk
      expect(store.get().anchor).toEqual({ id: 'a2', offset: 0 })
      expect(store.get().saved['']).toMatchObject({ selectedId: null, anchor: { id: 'gIn', offset: 0 }, follow: false })
      store.openScope(B)
      store.switchScope(A)
      expect(store.get().anchor).toEqual({ id: 'a2', offset: 0 })
      expect(open()).toEqual([A, B])
    })

    it('keeps the selection where the feed moved it, and the tab with it', async () => {
      await loaded()
      store.openScope(A)
      store.select('a2', { source: 'click' })
      store.applyChanges(batch({ upserts: [step('a2', 5, B)] }))
      const s = store.get()
      expect([s.scope, s.selectedId]).toEqual([A, 'a2'])
      expect(s.visibleIndex.a2).toBeUndefined()
    })
  })

  it('starts every conversation on the main session alone, keeps its main place, and keeps the tabs across the view', async () => {
    await loaded()
    store.select('m1', { source: 'click' })
    store.setPlace(false, { id: 'm1', offset: 2 })
    store.openScope(A)
    store.select('a1', { source: 'click' })
    store.setView('chat')
    store.setView('trajectory')
    expect([store.get().scope, open(), store.get().selectedId]).toEqual([A, [A], 'a1'])
    store.sessionChanged('gui:b')
    let s = store.get()
    expect([s.scope, s.scopes, s.saved, s.doors, s.scopeSizes, s.hiddenCount]).toEqual([null, [], {}, {}, {}, 0])
    store.sessionChanged('gui:a')
    s = store.get()
    expect([s.scope, s.scopes, s.selectedId, s.anchor]).toEqual([null, [], 'm1', { id: 'm1', offset: 2 }])
  })

  it('filters fifty thousand rows by tab and counts every scope', () => {
    const scopes = Array.from({ length: 20 }, (_, k) => `t:run${k}`)
    const rows: TrajectoryEntry[] = []
    for (let i = 0; i < 50_000; i += 1) {
      const scope = scopes[Math.floor(i / 10) % 20]!
      if (i % 10 === 0) rows.push(entry(`d${i}`, i, { opens_subagent_scope: scope }))
      else if (i % 10 < 4) rows.push(entry(`m${i}`, i))
      else rows.push(entry(`s${i}`, i, { subagent_scope: scope }))
    }
    const wrong: string[] = []
    const main = store.rowsOf(rows, { ...store.get(), scope: null })
    if (main.visible.length !== 20_000) wrong.push(`main ${main.visible.length}`)
    if (Object.keys(main.doors).length !== 20) wrong.push(`doors ${Object.keys(main.doors).length}`)
    scopes.forEach((scope, k) => {
      if (main.doors[scope] !== `d${k * 10}`) wrong.push(`door ${scope} ${main.doors[scope]}`)
      if (main.scopeSizes[scope] !== 1500) wrong.push(`size ${scope} ${main.scopeSizes[scope]}`)
    })
    const one = store.rowsOf(rows, { ...store.get(), scope: scopes[3]! })
    if (one.visible.length !== 1500 || one.visible.some((e) => e.subagent_scope !== scopes[3])) wrong.push(`tab ${one.visible.length}`)
    expect(wrong).toEqual([])
  })
})

/* ── the waterfall's drawn set ────────────────────────────────────── */

describe('shown and selectAcross', () => {
  const load = async (rows: TrajectoryEntry[]): Promise<void> => {
    serve()
    store.install()
    unpitch()
    store.sessionChanged('gui:a')
    await store.refreshState()
    const walk = store.load()
    await flush()
    await answer(page(rows, null))
    await walk
  }

  it('shown carries every scope while visible stays on the tab', async () => {
    await load([entry('main1', 0), entry('sub1', 1, { subagent_scope: 't:sub' })])
    expect(store.get().shown.map((row) => row.entry_id)).toEqual(['main1', 'sub1'])
    expect(store.get().visible.map((row) => row.entry_id)).toEqual(['main1'])
  })

  it('selectAcross selects another scope without switching tabs', async () => {
    await load([entry('main1', 0), entry('sub1', 1, { subagent_scope: 't:sub' })])
    store.selectAcross('sub1', { source: 'link' })
    expect(store.get().selectedId).toBe('sub1')
    expect(store.get().scope).toBe(null)
  })

  it('selectAcross reveals a hidden entry', async () => {
    await load([entry('main1', 0), entry('hidden1', 1, { meta: { hidden: 'folded' } })])
    expect(store.get().shownIndex['hidden1']).toBeUndefined()
    store.selectAcross('hidden1', { source: 'link' })
    expect(store.get().revealed).toContain('hidden1')
    expect(store.get().shownIndex['hidden1']).toBeTypeOf('number')
  })
})
