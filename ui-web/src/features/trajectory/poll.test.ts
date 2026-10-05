// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { absorb, forget, gone, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as poll from './poll'
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

const entry = (id: string, at: number): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
  span_id: id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: null,
  operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null,
  meta: {},
})

const page = (entries: TrajectoryEntry[], next: string | null, over: Partial<TrajectoryListResult> = {}): TrajectoryListResult => ({
  epoch: 'e1', snapshot_revision: 100, entries, next_cursor: next, index_state: READY, complete: true, ...over,
})

const quietBatch = (after: number, over: Partial<TrajectoryChangesResult> = {}): TrajectoryChangesResult => ({
  epoch: 'e1', from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
  reset_required: false, index_state: READY, ...over,
})

let stateCalls = 0
let stateAnswer: () => Promise<{ enabled: boolean; policy_revision: number; recording_enabled: boolean }>
let listCalls: Array<string | null> = []
let listQueue: Array<Deferred<TrajectoryListResult>> = []
let changesCalls: Array<{ epoch: string; after: number }> = []
let changesQueue: Array<Deferred<TrajectoryChangesResult>> = []

const source: TrajectorySource = {
  state: () => { stateCalls += 1; return stateAnswer() },
  list: (_key, cursor) => {
    listCalls.push(cursor ?? null)
    const d = deferred<TrajectoryListResult>()
    listQueue.push(d)
    return d.promise
  },
  changes: (_key, epoch, after) => {
    changesCalls.push({ epoch, after })
    const d = deferred<TrajectoryChangesResult>()
    changesQueue.push(d)
    return d.promise
  },
}

/* Promise settlement under fake timers: a few microtask turns. */
const flush = async (): Promise<void> => { for (let i = 0; i < 12; i += 1) await Promise.resolve() }
const tick = async (ms: number): Promise<void> => { await vi.advanceTimersByTimeAsync(ms); await flush() }

const answerPage = async (p: TrajectoryListResult): Promise<void> => { listQueue.shift()!.resolve(p); await flush() }
const failPage = async (e: unknown): Promise<void> => { listQueue.shift()!.reject(e); await flush() }
const answerChanges = async (b: TrajectoryChangesResult): Promise<void> => { changesQueue.shift()!.resolve(b); await flush() }
const failChanges = async (e: unknown): Promise<void> => { changesQueue.shift()!.reject(e); await flush() }

function setVisible(on: boolean): void {
  Object.defineProperty(document, 'visibilityState', { value: on ? 'visible' : 'hidden', configurable: true })
}

/* A page that has shaken hands with a gateway serving the view, in a
   conversation with content: everything but the view itself. */
async function ready(enabled = true): Promise<void> {
  absorb(['trajectory-v1'])
  stateAnswer = async () => ({ enabled, policy_revision: 1, recording_enabled: true })
  store.install()
  unpitch()
  store.sessionChanged('gui:a')
  poll.install()
  await store.refreshState()
  await flush()
}

/* Into the trajectory view, with a one-page snapshot committed. */
async function entered(): Promise<void> {
  store.setView('trajectory')
  await flush()
  expect(listCalls).toEqual([null])
  await answerPage(page([entry('r0', 0), entry('r1', 1)], null))
  await tick(0)
  expect(store.get().snapshotReady).toBe(true)
}

beforeEach(() => {
  vi.useFakeTimers()
  stateCalls = 0
  stateAnswer = async () => ({ enabled: true, policy_revision: 1, recording_enabled: true })
  listCalls = []
  listQueue = []
  changesCalls = []
  changesQueue = []
  store._resetForTests()
  poll._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  setSources({ trajectory: source })
  setVisible(true)
  document.body.innerHTML = '<div class="chat"></div>'
})

afterEach(() => {
  poll._resetForTests()
  resetSources()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

/* ── the state chain ──────────────────────────────────────────────── */

describe('the state chain', () => {
  it('asks every five seconds in the conversation view, and at once on sync', async () => {
    await ready()
    expect(poll.isStateRunning()).toBe(true)
    const before = stateCalls
    await tick(poll.STATE_EVERY_MS)
    expect(stateCalls).toBe(before + 1)
    await tick(poll.STATE_EVERY_MS)
    expect(stateCalls).toBe(before + 2)
    poll.sync()
    await flush()
    expect(stateCalls).toBe(before + 3)
    expect(poll.isDataRunning()).toBe(false)
  })

  it('is quiet while the tab is hidden and asks the moment it is shown', async () => {
    await ready()
    const before = stateCalls
    setVisible(false)
    poll.sync()
    await tick(poll.STATE_EVERY_MS * 3)
    expect(stateCalls).toBe(before)
    setVisible(true)
    poll.sync()
    await flush()
    expect(stateCalls).toBe(before + 1)
  })

  it('brings the toggle back when the switch turns on again, without a reconnect', async () => {
    await ready(false)
    expect(store.available()).toBe(false)
    stateAnswer = async () => ({ enabled: true, policy_revision: 2, recording_enabled: true })
    await tick(poll.STATE_EVERY_MS)
    expect(store.available()).toBe(true)
  })

  it('stops on a refusal and resumes after a handshake that announces the surface', async () => {
    await ready()
    stateAnswer = () => Promise.reject(new RpcError(-32601, 'no'))
    await tick(poll.STATE_EVERY_MS)
    expect(store.get().served).toBe(false)
    expect(poll.isStateRunning()).toBe(false)
    const before = stateCalls
    await tick(poll.STATE_EVERY_MS * 3)
    expect(stateCalls).toBe(before)
    stateAnswer = async () => ({ enabled: true, policy_revision: 1, recording_enabled: true })
    forget('trajectory')
    store.handshake()
    await store.refreshState()
    poll.sync()
    await flush()
    expect(store.get().served).toBe(true)
    expect(poll.isStateRunning()).toBe(true)
  })
})

/* ── the data chain: the snapshot first ───────────────────────────── */

describe('the data chain before a snapshot is whole', () => {
  it('reads the snapshot and only then asks for changes', async () => {
    await ready()
    store.setView('trajectory')
    await flush()
    expect(poll.isDataRunning()).toBe(true)
    expect(listCalls).toEqual([null])
    expect(changesCalls).toEqual([])
    await answerPage(page([entry('r0', 0)], 'c1'))
    expect(changesCalls).toEqual([])
    await answerPage(page([entry('r1', 1)], null))
    await tick(0)
    expect(changesCalls).toEqual([{ epoch: 'e1', after: 100 }])
  })

  it('reads again on its slow beat after an expired cursor exhausts the walk, with no changes in between', async () => {
    await ready()
    store.setView('trajectory')
    await flush()
    await answerPage(page([entry('r0', 0)], 'c1'))
    for (let i = 0; i <= store.WALK_RETRIES; i += 1) await failPage(new RpcError(-32022, 'expired'))
    expect(store.get().snapshotReady).toBe(false)
    expect(changesCalls).toEqual([])
    const asked = listCalls.length
    await tick(poll.SLOW_MS - 1)
    expect(listCalls).toHaveLength(asked)
    await tick(1)
    expect(listCalls).toHaveLength(asked + 1)
    await answerPage(page([entry('r0', 0), entry('r1', 1)], null))
    await tick(0)
    expect(store.get().entries).toHaveLength(2)
    expect(changesCalls).toEqual([{ epoch: 'e1', after: 100 }])
  })

  it('reads again after a second page fails, with no changes in between', async () => {
    await ready()
    store.setView('trajectory')
    await flush()
    await answerPage(page([entry('r0', 0)], 'c1'))
    await failPage(new Error('socket'))
    expect(changesCalls).toEqual([])
    expect(store.get().fault).toBe('socket')
    await tick(poll.SLOW_MS)
    expect(listCalls).toEqual([null, 'c1', null])
    await answerPage(page([entry('r0', 0), entry('r1', 1)], null))
    await tick(0)
    expect(changesCalls).toHaveLength(1)
    expect(store.get().fault).toBeNull()
  })

  it('reads the snapshot over when the view is left mid-walk and entered again', async () => {
    await ready()
    store.setView('trajectory')
    await flush()
    await answerPage(page([entry('r0', 0)], 'c1'))
    store.setView('chat')
    await flush()
    expect(poll.isDataRunning()).toBe(false)
    await answerPage(page([entry('r1', 1)], null))
    expect(store.get().snapshotReady).toBe(false)
    store.setView('trajectory')
    await flush()
    expect(listCalls).toEqual([null, 'c1', null])
    expect(changesCalls).toEqual([])
    await answerPage(page([entry('r0', 0), entry('r1', 1), entry('r2', 2)], null))
    await tick(0)
    expect(store.get().entries).toHaveLength(3)
    expect(changesCalls).toEqual([{ epoch: 'e1', after: 100 }])
  })
})

/* ── the data chain: the feed ─────────────────────────────────────── */

describe('the data chain on a whole snapshot', () => {
  it('asks one at a time, half a second apart, slowing after ten quiet seconds and waking on a change', async () => {
    await ready()
    await entered()
    expect(changesCalls).toHaveLength(1)
    await tick(poll.FAST_MS * 4)
    /* Nothing is asked again while the first answer is out. */
    expect(changesCalls).toHaveLength(1)
    /* Nineteen quiet answers each bring the next ask half a second later;
       the twentieth is the tenth quiet second, and the next ask waits two. */
    for (let i = 0; i < poll.QUIET_BEATS; i += 1) {
      await answerChanges(quietBatch(100))
      await tick(poll.FAST_MS)
    }
    expect(changesCalls).toHaveLength(poll.QUIET_BEATS)
    await tick(poll.SLOW_MS - poll.FAST_MS - 1)
    expect(changesCalls).toHaveLength(poll.QUIET_BEATS)
    await tick(1)
    expect(changesCalls).toHaveLength(poll.QUIET_BEATS + 1)
    await answerChanges(quietBatch(100, { to_revision: 101, upserts: [entry('r2', 2)] }))
    expect(store.get().revision).toBe(101)
    await tick(poll.FAST_MS)
    expect(changesCalls).toHaveLength(poll.QUIET_BEATS + 2)
    expect(changesCalls[changesCalls.length - 1]).toEqual({ epoch: 'e1', after: 101 })
  })

  it('asks again at once on has_more, and moves the watermark only after applying', async () => {
    await ready()
    await entered()
    expect(store.get().revision).toBe(100)
    await answerChanges(quietBatch(100, { to_revision: 105, has_more: true, upserts: [entry('r2', 2)] }))
    expect(store.get().revision).toBe(105)
    await tick(0)
    expect(changesCalls[1]).toEqual({ epoch: 'e1', after: 105 })
  })

  it('pauses while the tab is hidden and asks the moment it is shown', async () => {
    await ready()
    await entered()
    await answerChanges(quietBatch(100))
    setVisible(false)
    await tick(poll.FAST_MS * 10)
    expect(changesCalls).toHaveLength(1)
    setVisible(true)
    poll.sync()
    await flush()
    expect(changesCalls).toHaveLength(2)
  })

  it('stops with the switch, keeps asking state, and picks the feed up from the watermark when it is back', async () => {
    await ready()
    await entered()
    await answerChanges(quietBatch(100, { to_revision: 103, upserts: [entry('r2', 2)] }))
    stateAnswer = async () => ({ enabled: false, policy_revision: 2, recording_enabled: true })
    await tick(poll.STATE_EVERY_MS)
    expect(store.get().view).toBe('chat')
    expect(poll.isDataRunning()).toBe(false)
    expect(poll.isStateRunning()).toBe(true)
    stateAnswer = async () => ({ enabled: true, policy_revision: 3, recording_enabled: true })
    await tick(poll.STATE_EVERY_MS)
    expect(store.available()).toBe(true)
    const asked = changesCalls.length
    store.setView('trajectory')
    await flush()
    expect(listCalls).toEqual([null])
    expect(changesCalls).toHaveLength(asked + 1)
    expect(changesCalls[changesCalls.length - 1]).toEqual({ epoch: 'e1', after: 103 })
  })

  it('re-reads the snapshot whole on a reset, then resumes the feed', async () => {
    await ready()
    await entered()
    store.select('r1', { source: 'click' })
    await answerChanges(quietBatch(100, { reset_required: true }))
    expect(store.get().snapshotReady).toBe(false)
    expect(store.get().entries).toHaveLength(2)
    await tick(0)
    expect(listCalls).toEqual([null, null])
    expect(changesCalls).toHaveLength(1)
    await answerPage(page([entry('r0', 0), entry('r1', 1), entry('r2', 2)], null, { epoch: 'e2', snapshot_revision: 7 }))
    await tick(0)
    const s = store.get()
    expect(s.epoch).toBe('e2')
    expect(s.revision).toBe(7)
    expect(s.selectedId).toBe('r1')
    expect(changesCalls[1]).toEqual({ epoch: 'e2', after: 7 })
  })

  it('keeps the committed snapshot when the re-read fails, and tries again on the slow beat', async () => {
    await ready()
    await entered()
    await answerChanges(quietBatch(100, { reset_required: true }))
    await tick(0)
    await failPage(new Error('down'))
    const s = store.get()
    expect(s.epoch).toBe('e1')
    expect(s.revision).toBe(100)
    expect(s.entries).toHaveLength(2)
    expect(s.fault).toBe('down')
    expect(changesCalls).toHaveLength(1)
    await tick(poll.SLOW_MS)
    expect(listCalls).toEqual([null, null, null])
  })

  it('drops an answer from before the view was left and entered again, and beats afresh', async () => {
    await ready()
    await entered()
    const late = changesQueue.shift()!
    store.setView('chat')
    store.setView('trajectory')
    await flush()
    expect(changesCalls).toHaveLength(2)
    late.resolve(quietBatch(100, { to_revision: 150, upserts: [entry('stale', 9)] }))
    await flush()
    expect(store.get().revision).toBe(100)
    expect(store.entry('stale')).toBeNull()
    await answerChanges(quietBatch(100, { to_revision: 101, upserts: [entry('r2', 2)] }))
    expect(store.get().revision).toBe(101)
  })

  it('drops a failure from before a session switch and back, so it cannot fault the new run', async () => {
    await ready()
    await entered()
    const late = changesQueue.shift()!
    store.sessionChanged('gui:b')
    store.sessionChanged('gui:a')
    store.setView('trajectory')
    await flush()
    late.reject(new RpcError(-32020, 'off'))
    await flush()
    expect(store.get().enabled).toBe(true)
    expect(store.get().view).toBe('trajectory')
    expect(store.get().fault).toBeNull()
  })

  it('leaves the view on -32020 and -32601 from the feed itself', async () => {
    await ready()
    await entered()
    await failChanges(new RpcError(-32020, 'off'))
    expect(store.get().view).toBe('chat')
    expect(store.get().enabled).toBe(false)
    expect(poll.isDataRunning()).toBe(false)
    expect(poll.isStateRunning()).toBe(true)
    stateAnswer = async () => ({ enabled: true, policy_revision: 4, recording_enabled: true })
    await tick(poll.STATE_EVERY_MS)
    store.setView('trajectory')
    await flush()
    await failChanges(new RpcError(-32601, 'no'))
    expect(store.get().served).toBe(false)
    expect(poll.isStateRunning()).toBe(false)
    expect(gone('trajectory', null)).toBe(true)
  })

  it('says a plain failure on the status line and keeps asking', async () => {
    await ready()
    await entered()
    await failChanges(new Error('socket'))
    expect(store.get().fault).toBe('socket')
    expect(store.get().entries).toHaveLength(2)
    await tick(poll.FAST_MS)
    expect(changesCalls).toHaveLength(2)
    await answerChanges(quietBatch(100))
    expect(store.get().fault).toBeNull()
  })
})
