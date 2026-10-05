// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { absorb, has, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './details'
import * as list from './store'

import type {
  TrajectoryBlockDescriptor, TrajectoryBlockResult, TrajectoryChangesResult, TrajectoryDetailResult, TrajectoryEntry,
  TrajectoryIndexState, TrajectoryListResult, TrajectorySource,
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

const entry = (id: string, at: number, revision = 1): TrajectoryEntry => ({
  entry_id: id, revision, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
  span_id: id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: null,
  operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null,
  meta: {},
})

const block = (id: string, renderer: TrajectoryBlockDescriptor['renderer'] = 'text'): TrajectoryBlockDescriptor => ({
  id, renderer, availability: 'available', preview: null, total_items: null, related_operation: null, reason: null,
})

const descriptor = (entryId: string, revision: number, blocks: string[], over: Partial<TrajectoryDetailResult> = {}): TrajectoryDetailResult => ({
  session_key: 'gui:a', epoch: 'e1', entry_id: entryId, entry_revision: revision, kind: 'tool.output', span_name: 'tool.call',
  slot: 'tool.output', operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], notes: [],
  blocks: [...blocks.map((b) => block(b)), block('timing', 'key_values'), block('raw', 'json')],
  revision_changed: false, truncated: false, ...over,
})

const body = (entryId: string, revision: number, blockId: string, data: unknown, over: Partial<TrajectoryBlockResult> = {}): TrajectoryBlockResult => ({
  entry_id: entryId, entry_revision: revision, epoch: 'e1', block_id: blockId, renderer: 'text', availability: 'available',
  reason: null, data: data as TrajectoryBlockResult['data'], next_cursor: null, total_items: null, integrity: [], truncated: false, ...over,
})

let detailCalls: Array<{ key: string; entryId: string }> = []
let detailQueue: Array<Deferred<TrajectoryDetailResult>> = []
let blockCalls: Array<{ entryId: string; revision: number; epoch: string; blockId: string; cursor: string | null }> = []
let blockQueue: Array<Deferred<TrajectoryBlockResult>> = []
let rows: TrajectoryEntry[] = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async (): Promise<TrajectoryListResult> => ({
    epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, index_state: READY, complete: true,
  }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: (key, entryId) => {
    detailCalls.push({ key, entryId })
    const d = deferred<TrajectoryDetailResult>()
    detailQueue.push(d)
    return d.promise
  },
  block: (_key, entryId, revision, epoch, blockId, cursor) => {
    blockCalls.push({ entryId, revision, epoch, blockId, cursor: cursor ?? null })
    const d = deferred<TrajectoryBlockResult>()
    blockQueue.push(d)
    return d.promise
  },
}

const flush = async (): Promise<void> => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }
const answerDetail = async (r: TrajectoryDetailResult): Promise<void> => { detailQueue.shift()!.resolve(r); await flush() }
const failDetail = async (e: unknown): Promise<void> => { detailQueue.shift()!.reject(e); await flush() }
const answerBlock = async (r: TrajectoryBlockResult): Promise<void> => { blockQueue.shift()!.resolve(r); await flush() }
const failBlock = async (e: unknown): Promise<void> => { blockQueue.shift()!.reject(e); await flush() }

const batch = (over: Partial<TrajectoryChangesResult>): TrajectoryChangesResult => ({
  epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [], removed: [], has_more: false, reset_required: false,
  index_state: READY, ...over,
})

/* A conversation with five rows on screen, in the trajectory view. */
async function ready(): Promise<void> {
  absorb(['trajectory-v1'])
  list.install()
  details.install()
  unpitch()
  list.sessionChanged('gui:a')
  await list.refreshState()
  list.setView('trajectory')
  rows = Array.from({ length: 5 }, (_, k) => entry(`r${k}`, k))
  await list.load()
}

beforeEach(() => {
  detailCalls = []
  detailQueue = []
  blockCalls = []
  blockQueue = []
  rows = []
  list._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
})

afterEach(() => {
  resetSources()
  document.body.innerHTML = ''
})

/* ── opening and closing ──────────────────────────────────────────── */

describe('the pane', () => {
  it('opens on a selection, stays closed on a migration, and keeps the selection when closed', async () => {
    await ready()
    expect(details.get().open).toBe(false)
    list.select('r1', { source: 'click' })
    expect(details.get().open).toBe(true)
    details.closeDetails()
    expect(details.get().open).toBe(false)
    expect(list.get().selectedId).toBe('r1')
    details.openDetails()
    expect(details.get().open).toBe(true)
    list.applyChanges(batch({ removed: [{ entry_id: 'r1', revision: 101, replaced_by: 'r1b' }], upserts: [entry('r1b', 1, 101)] }))
    expect(list.get().selectedId).toBe('r1b')
    expect(details.get().open).toBe(true)
    list.applyChanges(batch({ removed: [{ entry_id: 'r1b', revision: 102, replaced_by: null }], from_revision: 101, to_revision: 102 }))
    expect(list.get().selectedId).toBeNull()
    expect(details.get().open).toBe(false)
  })

  it('clamps its width between 320px and half the area, defaulting to 480px, and narrows under 800px', async () => {
    await ready()
    expect(details.detailsWidthPx()).toBe(details.DEFAULT_WIDTH)
    details.setAreaWidth(1200)
    details.setWidth(100)
    expect(details.detailsWidthPx()).toBe(details.MIN_WIDTH)
    details.setWidth(900)
    expect(details.detailsWidthPx()).toBe(600)
    details.setAreaWidth(700)
    expect(details.narrow()).toBe(true)
    expect(details.detailsWidthPx()).toBe(350)
    details.setAreaWidth(800)
    expect(details.narrow()).toBe(false)
  })
})

/* ── identities and tickets ───────────────────────────────────────── */

describe('identities', () => {
  it('files a block under session, epoch, entry, revision and block, with no aliasing between keys', () => {
    const a = { sessionKey: 'gui:a', epoch: 'e1', entryId: 'x', revision: 1 }
    expect(details.blockKey(a, 'content')).not.toBe(details.blockKey({ ...a, epoch: 'e2' }, 'content'))
    expect(details.blockKey(a, 'content')).not.toBe(details.blockKey({ ...a, revision: 2 }, 'content'))
    expect(details.keyOf('a:b', 'c')).not.toBe(details.keyOf('a', 'b:c'))
    expect(details.keyOf('["a"', 'b')).not.toBe(details.keyOf('[', '"a",b'))
  })

  it('reads the descriptor for the selected entry and then a block under that identity', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    expect(detailCalls).toEqual([{ key: 'gui:a', entryId: 'r1' }])
    expect(details.isLoading('descriptor')).toBe(true)
    await answerDetail(descriptor('r1', 1, ['result', 'params']))
    expect(details.isLoading('descriptor')).toBe(false)
    expect(details.get().current).toEqual({ sessionKey: 'gui:a', epoch: 'e1', entryId: 'r1', revision: 1 })
    expect(details.descriptor()?.blocks.map((b) => b.id)).toEqual(['result', 'params', 'timing', 'raw'])
    void details.loadBlock('result')
    expect(blockCalls).toEqual([{ entryId: 'r1', revision: 1, epoch: 'e1', blockId: 'result', cursor: null }])
    await answerBlock(body('r1', 1, 'result', { text: 'hello' }))
    expect(details.block('result')?.pages[0]?.data).toEqual({ text: 'hello' })
    expect(details.isPartial(details.block('result')!)).toBe(false)
  })

  it('drops an answer for a conversation the reader has left, clearing its own loading mark', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    list.sessionChanged('gui:b')
    await answerDetail(descriptor('r1', 1, ['result']))
    expect(Object.keys(details.get().descriptors)).toEqual([])
    expect(Object.keys(details.get().loading)).toEqual([])
  })

  it('A -> B -> A: the late answer for A is filed, and A is shown from it without a second read', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    list.select('r2', { source: 'click' })
    void details.loadDescriptor()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    /* The first read for r1 is still in the air, so no second one goes out. */
    expect(detailCalls.map((c) => c.entryId)).toEqual(['r1', 'r2'])
    await answerDetail(descriptor('r1', 1, ['result']))
    /* Asked under an older ticket: filed, not shown. */
    expect(Object.keys(details.get().descriptors)).toHaveLength(1)
    expect(details.get().current).toBeNull()
    expect(details.isLoading('descriptor')).toBe(false)
    /* The pane asks again and is answered from the cache at once. */
    void details.loadDescriptor()
    expect(detailCalls.map((c) => c.entryId)).toEqual(['r1', 'r2'])
    expect(details.get().current?.entryId).toBe('r1')
    await answerDetail(descriptor('r2', 1, ['result']))
    expect(details.get().current?.entryId).toBe('r1')
  })

  it('does not let A\'s late failure close or fault B', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    list.select('r2', { source: 'click' })
    void details.loadDescriptor()
    await failDetail(new RpcError(-32021, 'entry_not_found'))
    expect(list.get().selectedId).toBe('r2')
    expect(details.get().open).toBe(true)
    expect(details.fault('descriptor')).toBeNull()
    await answerDetail(descriptor('r2', 1, ['result']))
    void details.loadBlock('result')
    list.select('r3', { source: 'click' })
    await failBlock(new Error('socket'))
    expect(Object.keys(details.get().faults)).toEqual([])
    expect(Object.keys(details.get().loading)).toEqual([])
  })

  it('does not load anything when an answer lands on a closed pane, and reuses it when reopened', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    details.closeDetails()
    await answerDetail(descriptor('r1', 1, ['result']))
    expect(details.get().current).toBeNull()
    expect(blockCalls).toEqual([])
    details.openDetails()
    void details.loadDescriptor()
    expect(detailCalls).toHaveLength(1)
    expect(details.get().current?.entryId).toBe('r1')
  })

  it('lets a request settle only its own marks, never a newer request\'s for the same key', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    void details.loadBlock('result')
    const old = blockQueue.shift()!
    /* Leave and come back to the same tab: the old read is still out, so the
       new visit does not ask again, and the old answer fills the cache. */
    details.closeDetails()
    details.openDetails()
    void details.loadBlock('result')
    expect(blockCalls).toHaveLength(1)
    expect(details.isLoading({ blockId: 'result' })).toBe(true)
    old.resolve(body('r1', 1, 'result', { text: 'late' }))
    await flush()
    expect(details.isLoading({ blockId: 'result' })).toBe(false)
    expect(details.block('result')?.pages[0]?.data).toEqual({ text: 'late' })
  })

  it('acts on a surface verdict only from the connection it came from', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    list.handshake()
    await list.refreshState()
    await failDetail(new RpcError(-32601, 'no'))
    expect(list.get().served).toBe(true)
    expect(has('trajectory')).toBe(true)
    expect(list.get().view).toBe('trajectory')
    /* The same verdict in the current connection does act. */
    void details.loadDescriptor()
    await failDetail(new RpcError(-32601, 'no'))
    expect(list.get().served).toBe(false)
    expect(has('trajectory')).toBe(false)
  })

  it('closes and clears the selection when the current entry is gone', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await failDetail(new RpcError(-32021, 'entry_not_found'))
    expect(details.get().open).toBe(false)
    expect(list.get().selectedId).toBeNull()
  })
})

/* ── the budget ───────────────────────────────────────────────────── */

describe('the budget', () => {
  const big = (mib: number): string => 'x'.repeat(Math.ceil(mib * 1024 * 1024))

  it('measures bytes as UTF-8 and keeps the whole cache under eight mebibytes', async () => {
    expect(details.bytesOf('aé中')).toBe(JSON.stringify('aé中').length + 1 + 2)
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    const blocks = ['b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8', 'b9']
    await answerDetail(descriptor('r1', 1, blocks))
    for (const id of blocks) {
      details.setTab('r1', id)
      void details.loadBlock(id)
      await answerBlock(body('r1', 1, id, { text: big(1) }))
    }
    const kept = Object.values(details.get().blocks)
    expect(kept.length).toBeLessThan(blocks.length)
    expect(kept.some((r) => r.blockId === 'b9')).toBe(true)
    const total = kept.reduce((n, r) => n + r.bytes, 0) + Object.values(details.get().descriptors).reduce((n, r) => n + r.bytes, 0)
    expect(total).toBeLessThanOrEqual(details.BUDGET)
    expect(Object.keys(details.get().loading)).toEqual([])
  })

  it('lets an entry\'s earlier revision go when a new one takes its place', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    void details.loadBlock('result')
    await answerBlock(body('r1', 1, 'result', { text: 'v1' }))
    list.applyChanges(batch({ upserts: [entry('r1', 1, 2)] }))
    expect(details.get().stale).toBe(true)
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 2, ['result']))
    expect(details.get().current?.revision).toBe(2)
    expect(Object.values(details.get().blocks).filter((r) => r.identity.entryId === 'r1')).toEqual([])
    expect(details.block('result')).toBeNull()
  })

  it('keeps a sliding window of pages and counts what it let go as missing', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['messages']))
    void details.loadBlock('messages')
    for (let page = 0; page < details.PAGE_WINDOW + 2; page += 1) {
      const last = page === details.PAGE_WINDOW + 1
      await answerBlock(body('r1', 1, 'messages', { items: Array.from({ length: 20 }, (_, k) => ({ role: 'user', content: `m${page * 20 + k}` })), offset: page * 20 },
        { renderer: 'messages', next_cursor: last ? null : `c${page + 1}`, total_items: 240 }))
      if (!last) void details.loadMore('messages')
    }
    const record = details.block('messages')!
    expect(record.pages).toHaveLength(details.PAGE_WINDOW)
    expect(record.droppedBefore).toBe(40)
    expect(record.pages[0]?.offset).toBe(40)
    expect(record.nextCursor).toBeNull()
    expect(record.pages.every((p) => !p.truncated)).toBe(true)
    expect(details.isPartial(record)).toBe(true)
  })
})

/* ── revisions and epochs ─────────────────────────────────────────── */

describe('a changed revision', () => {
  it('is accepted from the descriptor itself, keeps a tab the new descriptor still has, and drops old bodies', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result', 'params']))
    details.setTab('r1', 'params')
    void details.loadBlock('params')
    await answerBlock(body('r1', 1, 'params', { value: { a: 1 } }, { renderer: 'json' }))
    list.applyChanges(batch({ upserts: [entry('r1', 1, 2)] }))
    expect(details.get().stale).toBe(true)
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 2, ['result', 'params'], { revision_changed: true }))
    expect(details.get().stale).toBe(false)
    expect(details.get().current?.revision).toBe(2)
    expect(details.tabOf('r1')).toBe('params')
    expect(details.block('params')).toBeNull()
    void details.loadBlock('params')
    expect(blockCalls[blockCalls.length - 1]).toEqual({ entryId: 'r1', revision: 2, epoch: 'e1', blockId: 'params', cursor: null })
  })

  it('falls back to the overview when the tab is gone from the new descriptor', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result'], { operation_status: 'error', blocks: [block('result'), block('error', 'key_values'), block('timing', 'key_values'), block('raw', 'json')] }))
    details.setTab('r1', 'error')
    list.applyChanges(batch({ upserts: [entry('r1', 1, 2)] }))
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 2, ['result']))
    expect(details.tabOf('r1')).toBe('overview')
  })

  it('re-reads the descriptor rather than the block when a page says the entry moved', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['messages']))
    details.setTab('r1', 'messages')
    void details.loadBlock('messages')
    await answerBlock(body('r1', 1, 'messages', { items: [{ role: 'user', content: 'a' }], offset: 0 }, { renderer: 'messages', next_cursor: 'c1' }))
    void details.loadMore('messages')
    await failBlock(new RpcError(-32023, 'moved', { current_revision: 2, current_epoch: 'e1' }))
    expect(detailCalls).toHaveLength(2)
    expect(blockCalls).toHaveLength(2)
    await answerDetail(descriptor('r1', 2, ['messages']))
    expect(details.get().current?.revision).toBe(2)
    expect(details.block('messages')).toBeNull()
  })

  it('leaves an epoch the list has not reached to the list', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    list.applyChanges(batch({ upserts: [entry('r1', 1, 2)] }))
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 2, ['result'], { epoch: 'e2' }))
    /* An epoch the list does not know yet: not taken, and the pane waits. */
    expect(details.get().current?.epoch).toBe('e1')
    expect(details.get().current?.revision).toBe(1)
    expect(Object.keys(details.get().descriptors)).toHaveLength(1)
    expect(details.get().stale).toBe(true)
    void details.loadBlock('result')
    await failBlock(new RpcError(-32023, 'moved', { current_revision: 1, current_epoch: 'e2' }))
    expect(detailCalls).toHaveLength(2)
  })

  it('gives up following revisions after three hops inside one reader action', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    details.setTab('r1', 'result')
    for (let hop = 2; hop <= details.REVISION_RETRIES + 1; hop += 1) {
      void details.loadBlock('result')
      await failBlock(new RpcError(-32023, 'moved', { current_revision: hop, current_epoch: 'e1' }))
      await answerDetail(descriptor('r1', hop, ['result']))
    }
    expect(details.get().unstable).toBe(false)
    void details.loadBlock('result')
    await failBlock(new RpcError(-32023, 'moved', { current_revision: 9, current_epoch: 'e1' }))
    expect(details.get().unstable).toBe(true)
    expect(detailQueue).toHaveLength(0)
  })
})
