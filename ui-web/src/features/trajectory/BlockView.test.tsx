// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import { get as toasts } from '../../state/toast'
import { BlockView, JsonView, KeyValuesView, copyText, usageRows } from './BlockView'
import * as details from './detailStore'
import { OPEN_EST, ROW_GAP } from './Messages'
import * as list from './store'

import type {
  JsonValue, TrajectoryBlockDescriptor, TrajectoryBlockResult, TrajectoryDetailResult, TrajectoryEntry, TrajectoryIndexState,
  TrajectorySource,
} from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

interface Deferred<T> { promise: Promise<T>; resolve(value: T): void; reject(reason: unknown): void }
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

const row: TrajectoryEntry = {
  entry_id: 'r0', revision: 1, kind: 'llm.input', span_name: 'llm.call', slot: 'llm.input', trace_id: 't', span_id: 'r0',
  parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main', sort_key: ['000000', 0],
  event_time: '2026-01-01T00:00:00Z', preview: 'r0', operation_status: 'ok', status_evidence: [], failure_entry: false,
  integrity: [], operation_start: null, operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded',
  duration_owner: null, meta: {},
}

const block = (over: Partial<TrajectoryBlockDescriptor> & Pick<TrajectoryBlockDescriptor, 'id'>): TrajectoryBlockDescriptor => ({
  renderer: 'text', availability: 'available', preview: null, total_items: null, related_operation: null, reason: null, ...over,
})

const descriptor: TrajectoryDetailResult = {
  session_key: 'gui:a', epoch: 'e1', entry_id: 'r0', entry_revision: 1, kind: 'llm.input', span_name: 'llm.call', slot: 'llm.input',
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], notes: [], revision_changed: false, truncated: false,
  blocks: [block({ id: 'messages', renderer: 'messages' }), block({ id: 'model', renderer: 'key_values' }), block({ id: 'raw', renderer: 'json' })],
}

const body = (blockId: string, data: unknown, over: Partial<TrajectoryBlockResult> = {}): TrajectoryBlockResult => ({
  entry_id: 'r0', entry_revision: 1, epoch: 'e1', block_id: blockId, renderer: 'text', availability: 'available', reason: null,
  data: data as TrajectoryBlockResult['data'], next_cursor: null, total_items: null, integrity: [], truncated: false, ...over,
})

let blockQueue: Array<Deferred<TrajectoryBlockResult>> = []
let blockCalls: string[] = []
/* Every read still unanswered, with the call it was: answered by name where the order of reads is not the point. */
let pendingCalls: Array<{ call: string; d: Deferred<TrajectoryBlockResult> }> = []
let detailResult: TrajectoryDetailResult = descriptor

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: [row], next_cursor: null, index_state: READY, complete: true }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: async () => detailResult,
  block: (_k, _e, _r, _ep, blockId, cursor) => {
    const call = `${blockId}|${cursor ?? ''}`
    blockCalls.push(call)
    const d = deferred<TrajectoryBlockResult>()
    blockQueue.push(d)
    pendingCalls.push({ call, d })
    return d.promise
  },
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }) }
const answer = async (r: TrajectoryBlockResult): Promise<void> => { await act(async () => { blockQueue.shift()!.resolve(r) }); await flush() }
const refuse = async (e: unknown): Promise<void> => { await act(async () => { blockQueue.shift()!.reject(e) }); await flush() }
const settleCall = async (call: string, outcome: { ok: TrajectoryBlockResult } | { fail: unknown }): Promise<void> => {
  const at = pendingCalls.findIndex((p) => p.call === call)
  if (at < 0) throw new Error(`no pending read ${call}; pending: ${pendingCalls.map((p) => p.call).join(', ')}`)
  const { d } = pendingCalls.splice(at, 1)[0]!
  blockQueue = blockQueue.filter((x) => x !== d)
  await act(async () => { if ('ok' in outcome) d.resolve(outcome.ok); else d.reject(outcome.fail) })
  await flush()
}
const pendingFiles = (): string[] => pendingCalls.map((p) => p.call).filter((c) => c.startsWith('file|'))
const q = (sel: string): HTMLElement | null => document.querySelector<HTMLElement>(sel)

const messages = (from: number, n: number) => Array.from({ length: n }, (_, k) => ({ role: 'user', content: `m${from + k}` }))
const outlineItems = (from: number, n: number) => Array.from({ length: n }, (_, k) => ({
  index: from + k, role: 'user', bytes: 40, chars: 3, preview: `m${from + k}`, partial: false, missing: false, cursor: `o${from + k}`,
}))

/* The pane the details scroll in, as the messages view finds it: a height it can read, a scroll it can set. */
function paneOf(height: number): HTMLElement {
  const pane = document.createElement('div')
  pane.className = 'trajectory-pane'
  Object.defineProperty(pane, 'clientHeight', { value: height, configurable: true })
  Object.defineProperty(pane, 'scrollTop', { value: 0, writable: true, configurable: true })
  document.body.appendChild(pane)
  return pane
}

async function ready(): Promise<void> {
  list.install()
  details.install()
  unpitch()
  list.sessionChanged('gui:a')
  await list.refreshState()
  list.setView('trajectory')
  await list.load()
  list.select('r0', { source: 'click' })
  await details.loadDescriptor()
}

beforeEach(() => {
  blockQueue = []
  blockCalls = []
  pendingCalls = []
  detailResult = descriptor
  list._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  /* The notices' host, so a copy's own notice has somewhere to stand. */
  document.body.innerHTML = '<div class="chat"></div><div id="toasts"></div>'
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('the renderers', () => {
  it('draws JSON as a tree that folds past two levels and names the gateway\'s placeholders', () => {
    render(<JsonView value={{ a: { b: { c: 1 } }, big: { $oversize: true, bytes: 9000 }, deep: { $depth_truncated: true }, $more_keys: 3 }} />)
    const keys = (): string[] => [...document.querySelectorAll('.trajectory-json-k')].map((k) => k.textContent ?? '')
    expect(keys()).toEqual(['a', 'b', 'big', 'deep'])
    /* `c` sits at depth 3 and is folded away until asked for. */
    const folds = [...document.querySelectorAll('.trajectory-json-fold')] as HTMLElement[]
    const b = folds.find((f) => f.textContent?.includes('b'))!
    act(() => { fireEvent.click(b) })
    expect(keys()).toEqual(['a', 'b', 'c', 'big', 'deep'])
    expect(document.body.textContent).toContain('gui.trajectory.details.oversize {"bytes":9000}')
    expect(document.body.textContent).toContain('gui.trajectory.details.depth_truncated')
    expect(document.body.textContent).toContain('gui.trajectory.details.more_keys {"n":3}')
  })

  it('draws key-values with their source, the timing words, and an omitted tail', () => {
    render(<KeyValuesView blockId="timing" items={[
      { key: 'duration_ms', value: 65000, source: 'derived' },
      { key: 'timing_basis', value: 'span_full', source: 'derived' },
      { key: 'tool.duration_ms', value: 12, source: 'attribute' },
      { key: '$omitted', value: 4, source: 'derived' },
    ]} />)
    const keys = [...document.querySelectorAll('.trajectory-kv-k')].map((k) => k.textContent)
    /* A key with a catalogue label reads as the label; one without reads as itself. */
    expect(keys).toEqual(['gui.trajectory.timing.duration', 'timing_basis', 'tool.duration_ms'])
    expect(document.body.textContent).toContain('65000 ms (1m05s)')
    expect(document.body.textContent).toContain('gui.trajectory.basis.span_full')
    expect([...document.querySelectorAll('.trajectory-kv-src')].map((s) => s.textContent)).toEqual([
      'gui.trajectory.details.source_derived', 'gui.trajectory.details.source_derived', 'gui.trajectory.details.source_attribute',
    ])
    expect(document.body.textContent).toContain('gui.trajectory.details.omitted_n {"n":4}')
  })
})

describe('a block tab', () => {
  it('lists every message from the outline, opens the ones this call added, reads their bodies by page and never asks the reader to load more', async () => {
    await ready()
    /* A continued call: twenty messages the reader has seen, twenty-five new ones. */
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'continued', new_from: 20, message_count: 45 } }]) }) })
    const spec = descriptor.blocks[0]!
    const writeText = vi.fn((_text: string) => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<BlockView block={spec} />, { container: paneOf(30000) })
    await flush()
    expect(blockCalls).toEqual(['outline|'])
    await answer(body('outline', { items: outlineItems(0, 45), offset: 0 }, { renderer: 'items', total_items: 45 }))
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(20)
    expect(document.querySelectorAll('.trajectory-msg-open')).toHaveLength(25)
    expect(q('.trajectory-more')).toBeNull()
    expect(q('.trajectory-msg-fold[data-index="0"]')?.textContent).toContain('m0')
    /* The bodies: the page holding rows 20-39 is asked for once, by the cursor of its first row. */
    expect(blockCalls.slice(1)).toEqual(['messages|o20'])
    await answer(body('messages', { items: messages(20, 20), offset: 20 }, { renderer: 'messages', next_cursor: 'c40', total_items: 45 }))
    expect(document.querySelectorAll('.trajectory-msg')).toHaveLength(20)
    expect(blockCalls.slice(2)).toEqual(['messages|o40'])
    await answer(body('messages', { items: messages(40, 5), offset: 40 }, { renderer: 'messages', next_cursor: null, total_items: 45 }))
    expect(document.querySelectorAll('.trajectory-msg')).toHaveLength(25)
    const texts = [...document.querySelectorAll('.trajectory-msg .trajectory-text-body')].map((m) => m.textContent)
    expect(texts[0]).toBe('m20')
    expect(texts[24]).toBe('m44')
    expect(q('.trajectory-more')).toBeNull()
    /* One row folded by hand stays folded; the whole list opens on request and reads the old page. */
    act(() => { fireEvent.click(q('.trajectory-msg-open[data-index="20"] .trajectory-msg-close') as HTMLElement) })
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(21)
    act(() => { fireEvent.click(document.querySelectorAll('.trajectory-msg-controls .trajectory-link')[0] as HTMLElement) })
    await flush()
    expect(blockCalls.slice(3)).toEqual(['messages|o0'])
    await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c20', total_items: 45 }))
    expect(document.querySelectorAll('.trajectory-msg')).toHaveLength(45)
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(0)
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(JSON.parse(writeText.mock.calls[0]![0])).toHaveLength(45)
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied')
    /* Folding the old messages again keeps the new ones open. */
    act(() => { fireEvent.click(document.querySelectorAll('.trajectory-msg-controls .trajectory-link')[1] as HTMLElement) })
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(20)
    expect(document.querySelectorAll('.trajectory-msg-open')).toHaveLength(25)
  })

  it.each([
    ['first', 3],
    ['independent', 3],
    ['unknown', 3],
    ['none', 0],
  ] as const)('opens every row for a %s call, none for a stored conversation: %i open', async (delta, open) => {
    await ready()
    const meta: Record<string, JsonValue> = delta === 'none'
      ? {}
      : { delta, new_from: delta === 'unknown' ? null : 0, message_count: 3 }
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta }]) }) })
    render(<BlockView block={descriptor.blocks[0]!} />)
    await flush()
    await answer(body('outline', { items: outlineItems(0, 3), offset: 0 }, { renderer: 'items', total_items: 3 }))
    expect(document.querySelectorAll('.trajectory-msg-open')).toHaveLength(open)
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(3 - open)
    if (delta === 'unknown') expect(q('.trajectory-msg-note')?.textContent).toBe('gui.trajectory.details.delta_unknown')
    else expect(q('.trajectory-msg-note')).toBeNull()
    if (open) {
      expect(blockCalls.at(-1)).toBe('messages|o0')
      await answer(body('messages', { items: messages(0, 3), offset: 0 }, { renderer: 'messages', next_cursor: null, total_items: 3 }))
      expect(document.querySelectorAll('.trajectory-msg')).toHaveLength(3)
    } else {
      expect(blockCalls).toEqual(['outline|'])
    }
  })

  it('lets the stalest pages go past the window and folds their rows again until asked, saying the copy is partial', async () => {
    await ready()
    const total = (details.PAGE_WINDOW + 2) * 20
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: total } }]) }) })
    const writeText = vi.fn((_text: string) => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<BlockView block={descriptor.blocks[0]!} />, { container: paneOf(30000) })
    await flush()
    await answer(body('outline', { items: outlineItems(0, total), offset: 0 }, { renderer: 'items', total_items: total }))
    expect(document.querySelectorAll('.trajectory-msg-open')).toHaveLength(total)
    /* Every row is in view in this tall pane: each page is asked for in turn. */
    for (let page = 0; page < details.PAGE_WINDOW + 2; page += 1) {
      const last = page === details.PAGE_WINDOW + 1
      expect(blockCalls.at(-1)).toBe(`messages|o${page * 20}`)
      await answer(body('messages', { items: messages(page * 20, 20), offset: page * 20 }, { renderer: 'messages', next_cursor: last ? null : `c${page + 1}`, total_items: total }))
    }
    /* The window is full: the two stalest pages are gone, their rows fold again, and nothing asks for them on its own. */
    const asked = blockCalls.length
    await flush()
    expect(blockCalls).toHaveLength(asked)
    const record = details.block('messages')!
    expect(record.pages).toHaveLength(details.PAGE_WINDOW)
    expect(record.evicted).toEqual([[0, 20], [20, 40]])
    expect(q('.trajectory-msg-open[data-index="239"] .trajectory-text-body')?.textContent).toBe('m239')
    expect(q('.trajectory-msg-open[data-index="0"]')).toBeNull()
    expect(q('.trajectory-msg-fold[data-index="0"]')?.textContent).toContain('m0')
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(40)
    expect(q('.trajectory-tool-warn')?.textContent).toBe('gui.trajectory.details.partial')
    /* Asking for a folded row reads its page again, and that page is kept over the stalest one. */
    act(() => { fireEvent.click(q('.trajectory-msg-fold[data-index="0"]') as HTMLElement) })
    await flush()
    expect(blockCalls.at(-1)).toBe('messages|o0')
    await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c1', total_items: total }))
    expect(q('.trajectory-msg-open[data-index="0"] .trajectory-text-body')?.textContent).toBe('m0')
    expect(details.block('messages')!.evicted).toEqual([[20, 40], [40, 60]])
    expect(document.querySelectorAll('.trajectory-msg-fold')).toHaveLength(40)
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied_partial')
    expect(JSON.parse(writeText.mock.calls[0]![0])).toHaveLength(details.PAGE_WINDOW * 20)
  })

  it('reads the rows a page was cut short of by their own cursors, and shows them', async () => {
    await ready()
    const total = 40
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: total } }]) }) })
    render(<BlockView block={descriptor.blocks[0]!} />, { container: paneOf(30000) })
    await flush()
    await answer(body('outline', { items: outlineItems(0, total), offset: 0 }, { renderer: 'items', total_items: total }))
    expect(blockCalls.at(-1)).toBe('messages|o0')
    /* The gateway cut the first page to five rows to fit one response. */
    await answer(body('messages', { items: messages(0, 5), offset: 0 }, { renderer: 'messages', next_cursor: 'c5', total_items: total, truncated: true }))
    expect(blockCalls.at(-1)).toBe('messages|o5')
    await answer(body('messages', { items: messages(5, 15), offset: 5 }, { renderer: 'messages', next_cursor: 'c20', total_items: total }))
    expect(q('.trajectory-msg-open[data-index="4"] .trajectory-text-body')?.textContent).toBe('m4')
    expect(q('.trajectory-msg-open[data-index="5"] .trajectory-text-body')?.textContent).toBe('m5')
    expect(q('.trajectory-msg-open[data-index="19"] .trajectory-text-body')?.textContent).toBe('m19')
    expect(blockCalls.at(-1)).toBe('messages|o20')
    await answer(body('messages', { items: messages(20, 20), offset: 20 }, { renderer: 'messages', next_cursor: null, total_items: total }))
    expect(blockCalls.filter((c) => c.startsWith('messages|'))).toEqual(['messages|o0', 'messages|o5', 'messages|o20'])
    expect(document.querySelectorAll('.trajectory-msg-open .trajectory-text-body')).toHaveLength(total)
  })

  it('draws only the rows near the viewport and reads only their pages: no scroll, no walk to the end', async () => {
    await ready()
    const total = 240
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: total } }]) }) })
    const pane = paneOf(400)
    render(<BlockView block={descriptor.blocks[0]!} />, { container: pane })
    await flush()
    await answer(body('outline', { items: outlineItems(0, total), offset: 0 }, { renderer: 'items', total_items: total }))
    expect(blockCalls).toEqual(['outline|', 'messages|o0'])
    const drawn = document.querySelectorAll('.trajectory-msg-open').length
    expect(drawn).toBeGreaterThan(0)
    expect(drawn).toBeLessThan(20)
    expect(q('[data-index="239"]')).toBeNull()
    await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c1', total_items: total }))
    await flush()
    /* The first page is in and the reader has not moved: nothing else is asked for. */
    expect(blockCalls).toEqual(['outline|', 'messages|o0'])
    expect(q('.trajectory-msg-open[data-index="0"] .trajectory-text-body')?.textContent).toBe('m0')
    /* Far down the list, the page under the viewport is asked for, and the rows at the top leave the DOM. */
    pane.scrollTop = 200 * (OPEN_EST + ROW_GAP)
    act(() => { fireEvent.scroll(pane) })
    await flush()
    expect(blockCalls.at(-1)).toBe('messages|o180')
    expect(q('[data-index="0"]')).toBeNull()
    expect(document.querySelectorAll('.trajectory-msg-open').length).toBeLessThan(20)
    await answer(body('messages', { items: messages(180, 20), offset: 180 }, { renderer: 'messages', next_cursor: 'c10', total_items: total }))
    expect(blockCalls.at(-1)).toBe('messages|o200')
    await answer(body('messages', { items: messages(200, 20), offset: 200 }, { renderer: 'messages', next_cursor: 'c11', total_items: total }))
    expect(q('.trajectory-msg-open[data-index="200"] .trajectory-text-body')?.textContent).toBe('m200')
    expect(blockCalls).toHaveLength(4)
  })

  it('asks for the outline again when its first request failed after the reader had left, and once only for an empty list', async () => {
    await ready()
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: 3 } }]) }) })
    const spec = descriptor.blocks[0]!
    const first = render(<BlockView block={spec} />, { container: paneOf(5000) })
    await flush()
    expect(blockCalls).toEqual(['outline|'])
    /* The reader leaves before the outline arrives; the request then fails on the wire. */
    act(() => { first.unmount() })
    list.select(null, { source: 'click' })
    await refuse(new Error('socket closed'))
    expect(details.get().outlines).toEqual({})
    /* Back on the entry, the view asks on its own: no empty list stands in for the outline. */
    list.select('r0', { source: 'click' })
    await details.loadDescriptor()
    render(<BlockView block={spec} />, { container: paneOf(5000) })
    await flush()
    expect(blockCalls).toEqual(['outline|', 'outline|'])
    expect(q('.trajectory-v-none')).toBeNull()
    /* An outline that really is empty is asked for once, and stays so across a remount. */
    await answer(body('outline', { items: [], offset: 0 }, { renderer: 'items', total_items: 0 }))
    expect(q('.trajectory-v-none')?.textContent).toBe('gui.trajectory.details.empty_list')
    expect(details.outline()?.done).toBe(true)
    cleanup()
    render(<BlockView block={spec} />, { container: paneOf(5000) })
    await flush()
    expect(blockCalls).toEqual(['outline|', 'outline|'])
    expect(q('.trajectory-v-none')?.textContent).toBe('gui.trajectory.details.empty_list')
  })

  it('says when a page of bodies failed, asks nothing more until the reader retries, then reads that page and goes on', async () => {
    await ready()
    const total = 60
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: total } }]) }) })
    render(<BlockView block={descriptor.blocks[0]!} />, { container: paneOf(30000) })
    await flush()
    await answer(body('outline', { items: outlineItems(0, total), offset: 0 }, { renderer: 'items', total_items: total }))
    expect(blockCalls.at(-1)).toBe('messages|o0')
    await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c1', total_items: total }))
    expect(blockCalls.at(-1)).toBe('messages|o20')
    await refuse(new Error('page two unavailable'))
    /* The failure is said where the rows wait, and nothing is asked again on its own. */
    expect(details.fault({ blockId: 'messages', more: true })).toBe('page two unavailable')
    const alert = q('.trajectory-msgs-view [role=alert]')
    expect(alert?.textContent).toContain('page two unavailable')
    const asked = blockCalls.length
    await flush()
    expect(blockCalls).toHaveLength(asked)
    expect(q('.trajectory-msg-open[data-index="20"] .trajectory-skel-line')).not.toBeNull()
    /* The reader's retry reads the failed page; the rows after it in view then read theirs. */
    act(() => { fireEvent.click(alert!.querySelector('.trajectory-link') as HTMLElement) })
    await flush()
    expect(q('.trajectory-msgs-view [role=alert]')).toBeNull()
    expect(blockCalls.at(-1)).toBe('messages|o20')
    await answer(body('messages', { items: messages(20, 20), offset: 20 }, { renderer: 'messages', next_cursor: 'c2', total_items: total }))
    expect(q('.trajectory-msg-open[data-index="20"] .trajectory-text-body')?.textContent).toBe('m20')
    expect(blockCalls.at(-1)).toBe('messages|o40')
    await answer(body('messages', { items: messages(40, 20), offset: 40 }, { renderer: 'messages', next_cursor: null, total_items: total }))
    expect(q('.trajectory-msg-open[data-index="59"] .trajectory-text-body')?.textContent).toBe('m59')
    expect(blockCalls).toHaveLength(asked + 2)
  })

  it('names a body the gateway could not serve, and retries a failed read inside the block', async () => {
    await ready()
    render(<BlockView block={descriptor.blocks[1]!} />)
    await flush()
    await act(async () => { blockQueue.shift()!.reject(new Error('disk')) })
    await flush()
    expect(q('.trajectory-fault')?.textContent).toContain('gui.trajectory.details.failed {"detail":"disk"}')
    await act(async () => { fireEvent.click(q('.trajectory-fault .trajectory-link') as HTMLElement) })
    await answer(body('model', null, { renderer: 'key_values', availability: 'missing', reason: 'artifact_missing' }))
    expect(q('.trajectory-fault')).toBeNull()
    expect(q('.trajectory-tool-note')?.textContent).toBe('gui.trajectory.reason.artifact_missing')
    expect(q('.trajectory-copy')).toBeNull()
  })

  it('asks for nothing while the pane is marked unstable, until the reader retries', async () => {
    await ready()
    details.setTab('r0', 'messages')
    details.set({ ...details.get(), unstable: true })
    render(<BlockView block={descriptor.blocks[0]!} />)
    await flush()
    await flush()
    expect(blockCalls).toEqual([])
    /* The reader's own reload is allowed through. */
    act(() => { void details.reloadBlock('messages') })
    await flush()
    expect(blockCalls).toEqual(['messages|'])
  })

  it('asks for nothing while the conversation view is up', async () => {
    await ready()
    details.setTab('r0', 'messages')
    list.setView('chat')
    render(<BlockView block={descriptor.blocks[0]!} />)
    await flush()
    expect(blockCalls).toEqual([])
    act(() => { list.setView('trajectory') })
    await flush()
    expect(blockCalls).toEqual(['outline|'])
  })

  it('copies text as text and JSON as pretty JSON', () => {
    const id = { sessionKey: 'gui:a', epoch: 'e1', entryId: 'r0', revision: 1 }
    const page = (data: unknown) => ({ offset: 0, data: data as never, availability: 'available' as const, reason: null, integrity: [], truncated: false, total: null, at: 0 })
    expect(copyText({ identity: id, blockId: 'content', renderer: 'text', pages: [page({ text: 'plain' })], nextCursor: null, letGo: 0, evicted: [], bytes: 0, at: 0 })).toBe('plain')
    expect(copyText({ identity: id, blockId: 'raw', renderer: 'json', pages: [page({ value: { a: 1 } })], nextCursor: null, letGo: 0, evicted: [], bytes: 0, at: 0 })).toBe('{\n  "a": 1\n}')
  })
})

describe('relations, usage and skills', () => {
  it('links a relation to the row it names, by entry id or by trace and span, and leaves unknown ones plain', async () => {
    await ready()
    act(() => {
      list.set({
        ...list.get(),
        ...list.rowsOf([
          row,
          { ...row, entry_id: 'r0-out', slot: 'llm.output' },
          { ...row, entry_id: 'turn-in', trace_id: 't', span_id: 'turn', slot: 'turn.input' },
          { ...row, entry_id: 'parent-sub', trace_id: 'p', span_id: 'dispatcher', slot: 'subagent.run' },
        ]),
      })
    })
    const items = [
      { key: 'trace_id', value: 't', source: 'derived' },
      { key: 'span_id', value: 'r0', source: 'derived' },
      { key: 'turn_span_id', value: 'turn', source: 'derived' },
      { key: 'parent_span_id', value: 'nowhere', source: 'derived' },
      { key: 'sibling_entries', value: ['r0-out', 'missing-id'], source: 'derived' },
      { key: 'trace.dispatched_in_trace_id', value: 'p', source: 'attribute' },
      { key: 'trace.dispatched_by_span_id', value: 'dispatcher', source: 'attribute' },
    ]
    render(<KeyValuesView items={items} blockId="relations" />)
    const links = [...document.querySelectorAll<HTMLElement>('.trajectory-rel')].map((l) => l.textContent)
    expect(links).toEqual(['turn', 'r0-out', 'dispatcher'])
    expect([...document.querySelectorAll('.trajectory-rel-off')].map((l) => l.textContent)).toEqual(['nowhere', 'missing-id'])
    act(() => { fireEvent.click(document.querySelectorAll<HTMLElement>('.trajectory-rel')[2]!) })
    expect(list.get().selectedId).toBe('parent-sub')
    expect(list.get().selectedBy).toBe('link')
  })

  it('reads the usage counters back in the reader\'s terms, never adding what was not recorded', () => {
    const sample = [
      { key: 'input_tokens', value: 532 }, { key: 'output_tokens', value: 126 }, { key: 'reasoning_tokens', value: 80 },
      { key: 'cache_read_tokens', value: 14592 }, { key: 'cache_write_tokens', value: null }, { key: 'total_tokens', value: 15250 },
      { key: 'cost_total', value: 0.00060885 },
    ]
    expect(usageRows(sample)).toEqual({ total: 15250, input: 15124, cacheRead: 14592, cacheWrite: undefined, output: 126, reasoning: 80, cost: 0.00060885 })
    /* Only the output and the total were recorded: the input is unknown, not zero. */
    expect(usageRows([{ key: 'output_tokens', value: 9 }, { key: 'total_tokens', value: 100 }])).toMatchObject({ total: 100, input: null, output: 9, cacheRead: undefined })
    /* Caches explicitly zero add nothing and say zero. */
    expect(usageRows([{ key: 'input_tokens', value: 50 }, { key: 'cache_read_tokens', value: 0 }, { key: 'cache_write_tokens', value: 0 }])).toMatchObject({ input: 50, cacheRead: 0, cacheWrite: 0, total: null })
    /* A cache write the provider reported joins the input total. */
    expect(usageRows([{ key: 'input_tokens', value: 100 }, { key: 'cache_read_tokens', value: 300 }, { key: 'cache_write_tokens', value: 50 }])).toMatchObject({ input: 450 })
  })

  it('draws the usage block as the six rows and the skills as marked items', async () => {
    await ready()
    const usageBlock = { id: 'usage', renderer: 'key_values' as const, availability: 'available' as const, preview: null, total_items: 7, related_operation: null, reason: null }
    render(<BlockView block={usageBlock} />)
    await flush()
    await answer(body('usage', { items: [
      { key: 'input_tokens', value: 532, source: 'attribute' }, { key: 'output_tokens', value: 126, source: 'attribute' },
      { key: 'reasoning_tokens', value: 80, source: 'attribute' }, { key: 'cache_read_tokens', value: 14592, source: 'attribute' },
      { key: 'cache_write_tokens', value: null, source: 'attribute' }, { key: 'total_tokens', value: 15250, source: 'attribute' },
      { key: 'cost_total', value: 0.00060885, source: 'attribute' },
    ] }, { renderer: 'key_values' }))
    const rows = [...document.querySelectorAll('.trajectory-usage .trajectory-kv-row')].map((r) => [r.querySelector('dt')?.textContent, r.querySelector('dd')?.textContent])
    expect(rows).toEqual([
      ['gui.trajectory.usage.total', '15250'],
      ['gui.trajectory.usage.input', '15124'],
      ['gui.trajectory.usage.cache_read', '14592'],
      ['gui.trajectory.usage.cache_write', 'gui.trajectory.usage.not_recorded'],
      ['gui.trajectory.usage.output', '126 (gui.trajectory.usage.reasoning 80)'],
      ['gui.trajectory.usage.cost', '$0.00060885'],
    ])
    cleanup()
    const injected = { id: 'injected', renderer: 'items' as const, availability: 'available' as const, preview: null, total_items: 2, related_operation: null, reason: null }
    render(<BlockView block={injected} />)
    await flush()
    await answer(body('injected', { items: [{ id: 'pdf', used: true }, { id: 'web', used: false }], offset: 0 }, { renderer: 'items', total_items: 2 }))
    const skills = [...document.querySelectorAll('.trajectory-skill-use .trajectory-item')].map((li) => [li.className, li.textContent])
    expect(skills).toEqual([
      ['trajectory-item trajectory-skill-used', 'pdfgui.trajectory.details.skill_used'],
      ['trajectory-item trajectory-skill-unused', 'webgui.trajectory.details.skill_unused'],
    ])
  })
})


/* A layout as a browser would give the file sections: each its height, the
   list's gap between them, the pane's scroll moving them. Section heights are
   read through `heights` each time, so a test can fold or reflow one. */
function layOut(pane: HTMLElement, heights: () => number[], gap = 14): () => void {
  const original = HTMLElement.prototype.getBoundingClientRect
  const rect = (top: number, height: number): DOMRect =>
    ({ top, bottom: top + height, left: 0, right: 400, width: 400, height, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement): DOMRect {
    if (this === pane) return rect(0, pane.clientHeight)
    const index = this.dataset?.fileIndex
    if (index === undefined) return original.call(this)
    const hs = heights()
    let top = 0
    for (let k = 0; k < Number(index); k += 1) top += (hs[k] ?? 100) + gap
    return rect(top - pane.scrollTop, hs[Number(index)] ?? 100)
  }
  return () => { HTMLElement.prototype.getBoundingClientRect = original }
}

/* A resize observer the test fires by hand, as the browser would on a size change. */
let observers: Array<{ fire: () => void }> = []
function fakeResizeObserver(): void {
  observers = []
  /* Only what is observed is fired: an observer created and never pointed at anything stays silent. */
  vi.stubGlobal('ResizeObserver', class {
    private readonly entry: { fire: () => void }
    constructor(callback: () => void) { this.entry = { fire: () => callback() } }
    observe(): void { if (!observers.includes(this.entry)) observers.push(this.entry) }
    unobserve(): void {}
    disconnect(): void { observers = observers.filter((o) => o !== this.entry) }
  })
}

describe('a message list whose rows change size by themselves', () => {
  it('measures a row again when it shrinks by itself, and draws the rows that come into view', async () => {
    fakeResizeObserver()
    const heights = new Map<number, number>([[0, 4000]])
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get(this: HTMLElement) { const i = this.dataset?.index; return i === undefined ? 0 : (heights.get(Number(i)) ?? 100) },
    })
    try {
      await ready()
      act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: 40 } }]) }) })
      render(<BlockView block={descriptor.blocks[0]!} />, { container: paneOf(300) })
      await flush()
      await answer(body('outline', { items: outlineItems(0, 40), offset: 0 }, { renderer: 'items', total_items: 40 }))
      await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c20', total_items: 40 }))
      await flush()
      /* The first message is tall: it fills the window alone. */
      expect([...document.querySelectorAll('.trajectory-msgs [data-index]')].map((e) => (e as HTMLElement).dataset.index)).toEqual(['0'])
      /* Its tree folds: nothing in the list re-rendered, but the browser says the list resized. */
      heights.set(0, 100)
      act(() => { for (const o of [...observers]) o.fire() })
      await flush()
      expect(document.querySelectorAll('.trajectory-msgs [data-index]').length).toBeGreaterThan(3)
    } finally {
      if (original) Object.defineProperty(HTMLElement.prototype, 'offsetHeight', original)
    }
  })
})

describe('the files under the raw record', () => {
  let undo: (() => void) | null = null
  afterEach(() => { undo?.(); undo = null })
  const withFiles: TrajectoryDetailResult = {
    ...descriptor,
    blocks: [
      block({ id: 'model', renderer: 'key_values' }),
      block({ id: 'raw', renderer: 'json' }),
      block({ id: 'files', renderer: 'items', total_items: 20 }),
      block({ id: 'file', renderer: 'items', total_items: 20 }),
    ],
  }
  const dirItem = (index: number) => ({ index, key: `f${index}.artifact_path`, path: `/logs/f${index}.json`, size: 2048, cursor: `c${index}` })
  const directory = (n: number) => body('files', { items: Array.from({ length: n }, (_, k) => dirItem(k)), offset: 0 }, { renderer: 'items', total_items: n })
  const fileOf = (index: number, over: Record<string, unknown> = {}) =>
    body('file', { items: [{ index, key: `f${index}.artifact_path`, path: `/logs/f${index}.json`, size: 2048, kind: 'json', value: { n: index }, shown_bytes: 9, truncated: null, ...over }], offset: index }, { renderer: 'items', total_items: 20 })
  const rawBlock = () => withFiles.blocks[1]!

  async function openRaw(height: number, files: number, heights: () => number[] = () => []): Promise<HTMLElement> {
    detailResult = { ...withFiles, blocks: withFiles.blocks.map((b) => (b.id === 'files' || b.id === 'file' ? { ...b, total_items: files } : b)) }
    await ready()
    act(() => { details.setTab('r0', 'raw') })
    const pane = paneOf(height)
    undo = layOut(pane, heights)
    render(<BlockView block={rawBlock()} />, { container: pane })
    await flush()
    await settleCall('raw|', { ok: body('raw', { value: { attributes: { 'f0.artifact_path': '/logs/f0.json' } } }, { renderer: 'json' }) })
    await settleCall('files|', { ok: directory(files) })
    return pane
  }

  it('reads no file before the raw record above it has arrived, whatever lands first', async () => {
    detailResult = withFiles
    await ready()
    act(() => { details.setTab('r0', 'raw') })
    const pane = paneOf(300)
    undo = layOut(pane, () => [])
    render(<BlockView block={rawBlock()} />, { container: pane })
    await flush()
    /* The directory lands before the record: its headings are drawn, nothing is read yet. */
    await settleCall('files|', { ok: directory(3) })
    expect(document.querySelectorAll('.trajectory-file-h')).toHaveLength(3)
    expect(pendingFiles()).toEqual([])
    await settleCall('raw|', { ok: body('raw', { value: { attributes: {} } }, { renderer: 'json' }) })
    expect(pendingFiles()).toEqual(['file|c0', 'file|c1'])
  })

  it('heads every file, reads only the open ones in view two at a time, and reads the others when the pane scrolls to them', async () => {
    let folded = new Set<number>()
    const pane = await openRaw(300, 20, () => Array.from({ length: 20 }, (_, k) => (folded.has(k) ? 40 : 280)))
    expect([...document.querySelectorAll('.trajectory-file-h')].map((h) => h.textContent)).toEqual(Array.from({ length: 20 }, (_, k) => `f${k}.artifact_path`))
    expect(q('.trajectory-file-path')?.textContent).toBe('/logs/f0.json · 2.0 KiB')
    expect(pendingFiles()).toEqual(['file|c0', 'file|c1'])
    await settleCall('file|c0', { ok: fileOf(0) })
    expect(pendingFiles()).toEqual(['file|c1', 'file|c2'])
    await settleCall('file|c1', { ok: fileOf(1) })
    await settleCall('file|c2', { ok: fileOf(2) })
    /* Three sections fill the view: nothing past them is read on its own. */
    await flush()
    expect(pendingFiles()).toEqual([])
    expect(blockCalls.filter((c) => c.startsWith('file|'))).toEqual(['file|c0', 'file|c1', 'file|c2'])
    expect(q('[data-file-index="0"] .trajectory-json')).not.toBeNull()
    /* A folded section is not read; scrolling brings the next ones in. */
    act(() => { fireEvent.click(q('[data-file-index="3"] .trajectory-file-toggle') as HTMLElement) })
    folded = new Set([3])
    pane.scrollTop = 600
    act(() => { fireEvent.scroll(pane) })
    await flush()
    expect(pendingFiles()).toEqual(['file|c4', 'file|c5'])
  })

  it('says where a file failed, goes on with the others, and reads that file again only when the reader asks', async () => {
    await openRaw(300, 3)
    await settleCall('file|c0', { fail: new Error('file zero unavailable') })
    const alert = q('[data-file-index="0"] [role=alert]')
    expect(alert?.textContent).toContain('file zero unavailable')
    /* The others go on; the failed one is not asked for again on its own. */
    await settleCall('file|c1', { ok: fileOf(1) })
    await settleCall('file|c2', { ok: fileOf(2) })
    await flush()
    expect(pendingFiles()).toEqual([])
    act(() => { fireEvent.click(alert!.querySelector('.trajectory-link') as HTMLElement) })
    await flush()
    expect(pendingFiles()).toEqual(['file|c0'])
    await settleCall('file|c0', { ok: fileOf(0) })
    expect(q('[data-file-index="0"] [role=alert]')).toBeNull()
    expect(q('[data-file-index="0"] .trajectory-json')).not.toBeNull()
  })

  it('shows JSON as a tree and text as text, says why a file is cut and why one could not be read', async () => {
    await openRaw(30000, 4)
    await settleCall('file|c0', { ok: fileOf(0) })
    await settleCall('file|c1', { ok: fileOf(1, { kind: 'text', value: undefined, text: 'cut here', size: 600 * 1024, shown_bytes: 512 * 1024, truncated: 'file_limit' }) })
    await settleCall('file|c2', { ok: fileOf(2, { kind: 'text', value: undefined, text: 'squeezed', size: null, shown_bytes: 2048, truncated: 'response_limit' }) })
    await settleCall('file|c3', { ok: fileOf(3, { kind: 'none', value: undefined, reason: 'artifact_missing' }) })
    expect(q('[data-file-index="0"] .trajectory-json')).not.toBeNull()
    expect(q('[data-file-index="1"] .trajectory-text-body')?.textContent).toBe('cut here')
    expect(q('[data-file-index="1"] .trajectory-file-cut')?.textContent).toBe('gui.trajectory.details.file_cut_file {"size":"600.0 KiB"}')
    expect(q('[data-file-index="2"] .trajectory-file-cut')?.textContent).toBe('gui.trajectory.details.file_cut_response_nosize {"shown":"2.0 KiB"}')
    expect(q('[data-file-index="3"] .trajectory-sec-note')?.textContent).toBe('gui.trajectory.reason.artifact_missing')
  })

  it('lets the stalest contents go past the budget without reading them back, and reads one again on request', async () => {
    await openRaw(30000, 20)
    const big = 'b'.repeat(480 * 1024)
    let answered = 0
    while (pendingFiles().length && answered < 40) {
      const call = pendingFiles()[0]!
      const index = Number(call.slice('file|c'.length))
      await settleCall(call, { ok: fileOf(index, { value: big }) })
      answered += 1
    }
    /* Every file was read exactly once: the budget's releases never asked for anything again. */
    expect(blockCalls.filter((c) => c.startsWith('file|'))).toHaveLength(20)
    expect(new Set(blockCalls.filter((c) => c.startsWith('file|'))).size).toBe(20)
    const released = q('[data-file-index="0"] .trajectory-file-released')
    expect(released?.textContent).toBe('gui.trajectory.details.file_released')
    expect(q('[data-file-index="19"] .trajectory-json')).not.toBeNull()
    act(() => { fireEvent.click(released as HTMLElement) })
    await flush()
    expect(pendingFiles()).toEqual(['file|c0'])
    await settleCall('file|c0', { ok: fileOf(0, { value: big }) })
    expect(q('[data-file-index="0"] .trajectory-file-released')).toBeNull()
    expect(q('[data-file-index="0"] .trajectory-json')).not.toBeNull()
  })

  it('keeps an answer that lands after the reader changed tabs out of the tab on screen, and shows it on the way back', async () => {
    await openRaw(300, 2)
    cleanup()
    act(() => { details.setTab('r0', 'model') })
    render(<BlockView block={withFiles.blocks[0]!} />, { container: paneOf(300) })
    await flush()
    await settleCall('file|c0', { ok: fileOf(0) })
    expect(q('.trajectory-files')).toBeNull()
    expect(q('.trajectory-json')).toBeNull()
    cleanup()
    act(() => { details.setTab('r0', 'raw') })
    render(<BlockView block={rawBlock()} />, { container: paneOf(300) })
    await flush()
    expect(q('[data-file-index="0"] .trajectory-json')).not.toBeNull()
    expect(blockCalls.filter((c) => c === 'file|c0')).toHaveLength(1)
  })

  it('counts the list\'s gaps: at the bottom of a hundred files, the last ones are read', async () => {
    const pane = await openRaw(300, 100, () => Array.from({ length: 100 }, () => 100))
    /* The top is read first: the sections in view and the overscan, two at a time. */
    for (let guard = 0; pendingFiles().length && guard < 20; guard += 1) {
      const call = pendingFiles()[0]!
      await settleCall(call, { ok: fileOf(Number(call.slice('file|c'.length))) })
    }
    expect(blockCalls.filter((c) => c.startsWith('file|'))).toEqual(['file|c0', 'file|c1', 'file|c2', 'file|c3', 'file|c4', 'file|c5', 'file|c6'])
    /* The list's real bottom, gaps included: sections 93 to 99 are in view or within the overscan. */
    pane.scrollTop = 100 * 100 + 99 * 14 - 300
    act(() => { fireEvent.scroll(pane) })
    await flush()
    expect(pendingFiles()).toEqual(['file|c93', 'file|c94'])
  })

  it('reads what comes into view when a file\'s own tree folds, or the window\'s width reflows it, without a scroll', async () => {
    fakeResizeObserver()
    const heights = [4000, 100, 100]
    await openRaw(300, 3, () => heights)
    expect(pendingFiles()).toEqual(['file|c0'])
    await settleCall('file|c0', { ok: fileOf(0) })
    expect(pendingFiles()).toEqual([])
    /* The tree inside the first file folds: the list shrinks, the browser says so, and the next files are read. */
    heights[0] = 100
    act(() => { for (const o of [...observers]) o.fire() })
    await flush()
    expect(pendingFiles()).toEqual(['file|c1', 'file|c2'])
    await settleCall('file|c1', { ok: fileOf(1) })
    await settleCall('file|c2', { ok: fileOf(2) })
    expect(blockCalls.filter((c) => c.startsWith('file|'))).toEqual(['file|c0', 'file|c1', 'file|c2'])
  })

  it('does not read a file a reflow pushed out of view', async () => {
    fakeResizeObserver()
    /* Wide, the first file is short and the second is in view; narrowed before anything is read, the first wraps tall. */
    const heights = [100, 100, 100]
    detailResult = { ...withFiles, blocks: withFiles.blocks.map((b) => (b.id === 'files' || b.id === 'file' ? { ...b, total_items: 3 } : b)) }
    await ready()
    act(() => { details.setTab('r0', 'raw') })
    const pane = paneOf(300)
    undo = layOut(pane, () => heights)
    render(<BlockView block={rawBlock()} />, { container: pane })
    await flush()
    await settleCall('files|', { ok: directory(3) })
    heights[0] = 5000
    act(() => { for (const o of [...observers]) o.fire() })
    await settleCall('raw|', { ok: body('raw', { value: { attributes: {} } }, { renderer: 'json' }) })
    expect(pendingFiles()).toEqual(['file|c0'])
  })
})
