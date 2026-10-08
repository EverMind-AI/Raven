// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { absorb, has, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
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

describe('the Escape layer', () => {
  it('answers for an open pick list first, then for the pane while it holds the focus', async () => {
    await ready()
    const { ESCAPE_ORDER } = await import('../../state/escapeOrder')
    const layer = ESCAPE_ORDER.find((l) => l.id === 'trajectory.escapeOpen()')!
    list.select('r1', { source: 'click' })
    expect(details.get().open).toBe(true)
    document.body.innerHTML = '<div class="chat"></div><div class="trajectory-details"><button id="f"></button></div>'
    expect(layer.isOpen()).toBe(false)
    document.getElementById('f')!.focus()
    expect(layer.isOpen()).toBe(true)
    list.openBucket(['r0'], 0)
    layer.close()
    expect(list.get().timeline.bucket).toBeNull()
    expect(details.get().open).toBe(true)
    layer.close()
    expect(details.get().open).toBe(false)
    expect(layer.isOpen()).toBe(false)
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
    /* One two-byte and one three-byte character beside an ASCII one: the
       count is UTF-8's, not the string's length. */
    expect(details.bytesOf('a\u00e9\u20ac')).toBe(JSON.stringify('a\u00e9\u20ac').length + 1 + 2)
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
    expect(record.letGo).toBe(40)
    expect(record.pages[0]?.offset).toBe(40)
    expect(record.nextCursor).toBeNull()
    expect(record.pages.every((p) => !p.truncated)).toBe(true)
    expect(details.isPartial(record)).toBe(true)
  })

  it('lets the stalest page go, never the one just asked for: the first page comes back after twelve, and the last stays', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['messages']))
    const page = (offset: number) => body('r1', 1, 'messages', { items: Array.from({ length: 20 }, (_, k) => ({ role: 'user', content: `m${offset + k}` })), offset },
      { renderer: 'messages', next_cursor: offset + 20 < 240 ? `c${offset + 20}` : null, total_items: 240 })
    void details.loadBlock('messages')
    for (let offset = 0; offset < 240; offset += 20) {
      await answerBlock(page(offset))
      if (offset + 20 < 240) void details.loadMore('messages')
    }
    let record = details.block('messages')!
    expect(record.pages.map((p) => p.offset).sort((a, b) => a - b)).toEqual([40, 60, 80, 100, 120, 140, 160, 180, 200, 220])
    expect(record.evicted).toEqual([0, 20])
    expect(details.onEvictedPage(record, 7)).toBe(true)
    /* Back to the first message: its page is asked for by its own cursor, filed, and kept; the stalest other page goes. */
    void details.loadPageAt('messages', 'o0', 0)
    expect(blockCalls.at(-1)?.cursor).toBe('o0')
    await answerBlock(page(0))
    record = details.block('messages')!
    expect(details.messageAt(record, 0)).toEqual({ role: 'user', content: 'first'.length ? 'm0' : 'm0' })
    expect(details.pageHolding(record, 0)?.offset).toBe(0)
    expect(record.pages).toHaveLength(details.PAGE_WINDOW)
    expect(record.evicted).toEqual([20, 40])
    expect(details.onEvictedPage(record, 7)).toBe(false)
    expect(details.onEvictedPage(record, 45)).toBe(true)
    expect(record.bytes).toBeLessThanOrEqual(details.BUDGET)
    /* The last page is still held: asking for it reads nothing. */
    const asked = blockCalls.length
    void details.loadPageAt('messages', 'o220', 220)
    expect(blockCalls).toHaveLength(asked)
    expect(details.messageAt(record, 239)).toEqual({ role: 'user', content: 'm239' })
  })
})

/* ── the outline walk ─────────────────────────────────────────────── */

describe('the outline walk', () => {
  it('ends its loading when a request fails after the pane moved on, so the entry reads again on return', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['messages']))
    void details.loadOutline()
    expect(blockCalls.at(-1)).toMatchObject({ entryId: 'r1', blockId: 'outline' })
    expect(details.outline()?.loading).toBe(true)
    const r1 = details.get().current!
    /* The reader moves on; r1's outline request then fails on the wire. */
    list.select('r2', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r2', 1, ['content']))
    await failBlock(new Error('socket closed'))
    /* No page had come: the record goes, rather than standing in for an empty list. */
    expect(details.get().outlines[details.descriptorKey(r1)]).toBeUndefined()
    /* Back on r1, the walk starts again and the outline arrives. */
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await flush()
    const before = blockCalls.length
    void details.loadOutline()
    expect(blockCalls).toHaveLength(before + 1)
    expect(blockCalls.at(-1)).toMatchObject({ entryId: 'r1', blockId: 'outline' })
    await answerBlock(body('r1', 1, 'outline', { items: [{ index: 0, role: 'user', bytes: 2, chars: 2, preview: 'hi', partial: false, missing: false, cursor: 'o0' }], offset: 0 },
      { renderer: 'items', total_items: 1 }))
    expect(details.outline()?.items).toHaveLength(1)
    expect(details.outline()?.loading).toBe(false)
  })

  it('ends its loading when the gateway says the view is off, and forgives a leftover mark on retry', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['messages']))
    void details.loadOutline()
    const key = details.descriptorKey(details.get().current!)
    await failBlock(new RpcError(-32020, 'trajectory view is off'))
    expect(details.get().outlines[key]).toBeUndefined()
    /* The view comes back: the list is read again and the entry reopened. */
    await list.refreshState()
    list.setView('trajectory')
    await list.load()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await flush()
    /* A loading mark with no request behind it is a leftover: the retry clears it and reads on. */
    const partial = { identity: details.get().current!, items: [{ index: 0, role: 'user', bytes: 2, chars: 2, preview: 'hi', partial: false, missing: false, cursor: 'o0' }], total: 40, nextCursor: 'o20', loading: true, done: false, fault: null, bytes: 10, at: 1 }
    details.set({ ...details.get(), outlines: { ...details.get().outlines, [key]: partial } })
    const before = blockCalls.length
    void details.retryOutline()
    expect(blockCalls).toHaveLength(before + 1)
    expect(blockCalls.at(-1)).toMatchObject({ entryId: 'r1', blockId: 'outline' })
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

  it('leaves an epoch the list has not reached to the list, and waits', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    list.applyChanges(batch({ upserts: [entry('r1', 1, 2)] }))
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 2, ['result'], { epoch: 'e2' }))
    /* An epoch the list does not know yet: not taken, and the pane waits --
       for the list, not for its own next ask. */
    expect(details.get().current?.epoch).toBe('e1')
    expect(details.get().current?.revision).toBe(1)
    expect(Object.keys(details.get().descriptors)).toHaveLength(1)
    expect(details.get().waitingEpoch).toBe('e2')
    expect(details.mayRead(details.get(), { what: 'descriptor' })).toBe(false)
    void details.loadDescriptor()
    void details.loadBlock('result')
    expect(detailCalls).toHaveLength(2)
    expect(blockCalls).toHaveLength(0)
    /* A page answering with the same foreign epoch waits the same way. */
    details.set({ ...details.get(), waitingEpoch: null, stale: false })
    void details.loadBlock('result')
    await failBlock(new RpcError(-32023, 'moved', { current_revision: 1, current_epoch: 'e2' }))
    expect(details.get().waitingEpoch).toBe('e2')
    expect(detailCalls).toHaveLength(2)
    /* The list arrives at the epoch: the wait ends and the descriptor is stale. */
    list.set({ ...list.get(), epoch: 'e2' })
    expect(details.get().waitingEpoch).toBeNull()
    expect(details.get().stale).toBe(true)
    expect(details.mayRead(details.get(), { what: 'descriptor' })).toBe(true)
    expect(details.mayRead(details.get())).toBe(false)
  })

  it('drops a revision bound from the epoch before when the index is rebuilt, and takes the new epoch\'s descriptor once', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    details.setTab('r1', 'result')
    void details.loadBlock('result')
    /* The page says the entry is far ahead in this epoch... */
    await failBlock(new RpcError(-32023, 'moved', { current_revision: 50, current_epoch: 'e1' }))
    expect(details.get().pending).toEqual({ epoch: 'e1', revision: 50 })
    /* ...but the gateway answers from a rebuilt index, counting from one again. */
    await answerDetail(descriptor('r1', 1, ['result'], { epoch: 'e2' }))
    expect(details.get().waitingEpoch).toBe('e2')
    expect(details.get().current?.epoch).toBe('e1')
    /* The list arrives at the new epoch: the old bound means nothing now. */
    list.set({ ...list.get(), epoch: 'e2', revision: 1 })
    expect(details.get().waitingEpoch).toBeNull()
    expect(details.get().pending).toBeNull()
    expect(details.get().stale).toBe(true)
    void details.loadDescriptor()
    expect(detailCalls).toHaveLength(3)
    await answerDetail(descriptor('r1', 1, ['result'], { epoch: 'e2' }))
    expect(details.get().current).toEqual({ sessionKey: 'gui:a', epoch: 'e2', entryId: 'r1', revision: 1 })
    expect(details.get().stale).toBe(false)
    /* Taken once: asking again is answered from the cache, not the gateway. */
    void details.loadDescriptor()
    expect(detailCalls).toHaveLength(3)
    expect(details.mayRead()).toBe(true)
  })

  it('reads nothing while the view, the switch or the snapshot is away, and moves the ticket when they change', async () => {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['result']))
    const t0 = details.ticket()
    list.setView('chat')
    expect(details.ticket()).toBeGreaterThan(t0)
    expect(details.mayRead()).toBe(false)
    void details.loadBlock('result')
    expect(blockCalls).toHaveLength(0)
    list.setView('trajectory')
    expect(details.mayRead()).toBe(true)
    list.disabledByServer()
    expect(details.mayRead()).toBe(false)
    expect(details.get().open).toBe(true)
    expect(details.descriptor()).not.toBeNull()
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


/* ── the files under the raw record ─────────────────────────────────── */

describe('the files a span names', () => {
  const dirItem = (index: number) => ({ index, key: `f${index}.artifact_path`, path: `/logs/f${index}.json`, size: 10, cursor: `c${index}` })
  const dirPage = (from: number, n: number, total: number, next: string | null) =>
    body('r1', 1, 'files', { items: Array.from({ length: n }, (_, k) => dirItem(from + k)), offset: from }, { renderer: 'items', next_cursor: next, total_items: total })
  const fileBody = (index: number, value: unknown) =>
    body('r1', 1, 'file', { items: [{ index, key: `f${index}.artifact_path`, kind: 'json', value, size: 10, shown_bytes: 10, truncated: null }], offset: index },
      { renderer: 'items', total_items: 99 })

  async function onRaw(): Promise<void> {
    await ready()
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r1', 1, ['raw', 'files', 'file']))
    details.setTab('r1', 'raw')
  }

  it('walks a directory of twelve pages and keeps every entry, the first and the last readable by their cursors', async () => {
    await onRaw()
    void details.loadFileDir()
    for (let page = 0; page < 12; page += 1) {
      const last = page === 11
      expect(blockCalls.at(-1)).toMatchObject({ blockId: 'files', cursor: page === 0 ? null : `d${page}` })
      await answerBlock(dirPage(page * 200, 200, 2400, last ? null : `d${page + 1}`))
    }
    const dir = details.fileDir()!
    expect(dir.items).toHaveLength(2400)
    expect(dir.done).toBe(true)
    expect(dir.items[0]!.index).toBe(0)
    expect(dir.items[2399]!.index).toBe(2399)
    for (const file of [dir.items[0]!, dir.items[2399]!]) {
      void details.loadFile(file)
      expect(blockCalls.at(-1)).toMatchObject({ blockId: 'file', cursor: file.cursor })
      await answerBlock(fileBody(file.index, { n: file.index }))
      expect(details.fileBody(file.index)?.item).toMatchObject({ value: { n: file.index } })
    }
  })

  it('stops the walk at the directory\'s own cap without letting any listed file go, and says how many it listed', async () => {
    await onRaw()
    void details.loadFileDir()
    const long = 'p'.repeat(4000)
    let page = 0
    while (blockQueue.length && page < 20) {
      const items = Array.from({ length: 100 }, (_, k) => ({ ...dirItem(page * 100 + k), path: long }))
      await answerBlock(body('r1', 1, 'files', { items, offset: page * 100 }, { renderer: 'items', next_cursor: `d${page + 1}`, total_items: 9999 }))
      page += 1
    }
    const dir = details.fileDir()!
    expect(dir.capped).toBe(true)
    expect(dir.bytes).toBeGreaterThanOrEqual(details.FILES_DIRECTORY_MAX_BYTES)
    expect(dir.items[0]!.index).toBe(0)
    expect(dir.items).toHaveLength(page * 100)
    expect(blockQueue).toHaveLength(0)
    const asked = blockCalls.length
    void details.loadFileDir()
    expect(blockCalls).toHaveLength(asked)
  })

  it('takes a walk cut short back up from the cursor it holds, and leaves no record when no page had come', async () => {
    await onRaw()
    void details.loadFileDir()
    await failBlock(new Error('socket closed'))
    expect(details.fileDir()?.fault).toBe('socket closed')
    void details.retryFileDir()
    await answerBlock(dirPage(0, 200, 400, 'd1'))
    /* The reader leaves mid-walk; the next page fails after they have gone. */
    list.select('r2', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r2', 1, ['content']))
    await failBlock(new Error('dropped'))
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await flush()
    details.setTab('r1', 'raw')
    const held = details.fileDir()!
    expect(held.items).toHaveLength(200)
    expect(held.loading).toBe(false)
    void details.loadFileDir()
    expect(blockCalls.at(-1)).toMatchObject({ blockId: 'files', cursor: 'd1' })
    await answerBlock(dirPage(200, 200, 400, null))
    expect(details.fileDir()!.items).toHaveLength(400)
    /* A walk that never got a page leaves nothing behind to stand in for an empty directory. */
    list.select('r3', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r3', 1, ['raw', 'files', 'file']))
    void details.loadFileDir()
    list.select('r1', { source: 'click' })
    await failBlock(new Error('gone'))
    expect(Object.values(details.get().fileDirs).some((r) => r.identity.entryId === 'r3')).toBe(false)
  })

  it('keeps every small file however many, lets the stalest content go past the budget, never the one just read, and reads a released one back on request', async () => {
    await onRaw()
    const files = Array.from({ length: 11 }, (_, k) => ({ ...dirItem(k) }))
    for (const file of files) {
      void details.loadFile(file)
      await answerBlock(fileBody(file.index, { small: file.index }))
    }
    expect(files.every((f) => details.fileBody(f.index) !== null)).toBe(true)
    /* Twenty files of about 480 KiB: more than the budget. */
    const big = 'b'.repeat(480 * 1024)
    const bigFiles = Array.from({ length: 20 }, (_, k) => ({ ...dirItem(100 + k) }))
    void details.loadBlock('raw')
    await answerBlock(body('r1', 1, 'raw', { value: { attributes: {} } }, { renderer: 'json' }))
    for (const file of bigFiles) {
      void details.loadFile(file)
      await answerBlock(fileBody(file.index, big))
      /* The one just read is always held. */
      expect(details.fileBody(file.index)).not.toBeNull()
    }
    const s = details.get()
    const used = Object.values(s.fileBodies).reduce((n, r) => n + r.bytes, 0) + Object.values(s.blocks).reduce((n, r) => n + r.bytes, 0)
    expect(used).toBeLessThanOrEqual(details.BUDGET)
    expect(details.block('raw')).not.toBeNull()
    /* The stalest went first: the small files, then the earliest big ones, each marked released. */
    expect(details.isReleased(0)).toBe(true)
    expect(details.fileBody(0)).toBeNull()
    expect(details.isReleased(119)).toBe(false)
    const released = bigFiles.filter((f) => details.isReleased(f.index))
    expect(released.length).toBeGreaterThan(0)
    expect(released[0]!.index).toBe(100)
    /* Asked for again, it is read and kept; its mark goes. */
    const asked = blockCalls.length
    void details.reloadFile(bigFiles[0]!)
    expect(blockCalls).toHaveLength(asked + 1)
    await answerBlock(fileBody(100, big))
    expect(details.isReleased(100)).toBe(false)
    expect(details.fileBody(100)).not.toBeNull()
  })

  it('files a late answer under the entry that asked, and keeps one file\'s failure to that file', async () => {
    await onRaw()
    const a = dirItem(0)
    const b = dirItem(1)
    void details.loadFile(a)
    void details.loadFile(b)
    /* The reader moves on before either answer lands. */
    list.select('r2', { source: 'click' })
    void details.loadDescriptor()
    await answerDetail(descriptor('r2', 1, ['raw', 'files', 'file']))
    await answerBlock(fileBody(0, { from: 'r1' }))
    await failBlock(new Error('file one failed'))
    expect(details.fileBody(0)).toBeNull()
    expect(details.fileFault(1)).toBeNull()
    expect(Object.values(details.get().fileBodies).map((r) => r.identity.entryId)).toEqual(['r1'])
    /* Back on r1: the late answer is there; on its own entry a failure stays with its file. */
    list.select('r1', { source: 'click' })
    void details.loadDescriptor()
    await flush()
    expect(details.fileBody(0)?.item).toMatchObject({ value: { from: 'r1' } })
    void details.loadFile(b)
    await failBlock(new Error('file one failed'))
    expect(details.fileFault(1)).toBe('file one failed')
    expect(details.fileFault(0)).toBeNull()
    void details.reloadFile(b)
    expect(details.fileFault(1)).toBeNull()
    await answerBlock(fileBody(1, { ok: true }))
    expect(details.fileBody(1)?.item).toMatchObject({ value: { ok: true } })
  })
})
