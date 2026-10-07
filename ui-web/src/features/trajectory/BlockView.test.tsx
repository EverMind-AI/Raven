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

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: [row], next_cursor: null, index_state: READY, complete: true }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: async () => descriptor,
  block: (_k, _e, _r, _ep, blockId, cursor) => { blockCalls.push(`${blockId}|${cursor ?? ''}`); const d = deferred<TrajectoryBlockResult>(); blockQueue.push(d); return d.promise },
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }) }
const answer = async (r: TrajectoryBlockResult): Promise<void> => { await act(async () => { blockQueue.shift()!.resolve(r) }); await flush() }
const q = (sel: string): HTMLElement | null => document.querySelector<HTMLElement>(sel)

const messages = (from: number, n: number) => Array.from({ length: n }, (_, k) => ({ role: 'user', content: `m${from + k}` }))
const outlineItems = (from: number, n: number) => Array.from({ length: n }, (_, k) => ({
  index: from + k, role: 'user', bytes: 40, chars: 3, preview: `m${from + k}`, partial: false, missing: false, cursor: `o${from + k}`,
}))

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
    render(<BlockView block={spec} />)
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

  it('lets the earliest pages go past the window and folds their rows again, saying the copy is partial', async () => {
    await ready()
    act(() => { list.set({ ...list.get(), ...list.rowsOf([{ ...row, meta: { delta: 'first', new_from: 0, message_count: (details.PAGE_WINDOW + 2) * 20 } }]) }) })
    const writeText = vi.fn((_text: string) => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<BlockView block={descriptor.blocks[0]!} />)
    await flush()
    const total = (details.PAGE_WINDOW + 2) * 20
    await answer(body('outline', { items: outlineItems(0, total), offset: 0 }, { renderer: 'items', total_items: total }))
    expect(document.querySelectorAll('.trajectory-msg-open')).toHaveLength(total)
    /* Every row is in view here, as in a short pane: each page is asked for in turn. */
    for (let page = 0; page < details.PAGE_WINDOW + 2; page += 1) {
      const last = page === details.PAGE_WINDOW + 1
      expect(blockCalls.at(-1)).toBe(`messages|o${page * 20}`)
      await answer(body('messages', { items: messages(page * 20, 20), offset: page * 20 }, { renderer: 'messages', next_cursor: last ? null : `c${page + 1}`, total_items: total }))
    }
    /* The window is full and the first pages are gone: nothing asks for them again on its own. */
    const asked = blockCalls.length
    await flush()
    expect(blockCalls).toHaveLength(asked)
    const record = details.block('messages')!
    expect(record.pages).toHaveLength(details.PAGE_WINDOW)
    expect(record.pages[0]!.offset).toBe(40)
    /* The rows whose page was let go stand open with their skeleton again; a body still held is drawn. */
    expect(q('.trajectory-msg-open[data-index="239"] .trajectory-text-body')?.textContent).toBe('m239')
    expect(q('.trajectory-msg-open[data-index="0"] .trajectory-text-body')).toBeNull()
    expect(q('.trajectory-tool-warn')?.textContent).toBe('gui.trajectory.details.partial')
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied_partial')
    expect(JSON.parse(writeText.mock.calls[0]![0])).toHaveLength(details.PAGE_WINDOW * 20)
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
    const page = (data: unknown) => ({ offset: 0, data: data as never, availability: 'available' as const, reason: null, integrity: [], truncated: false, total: null })
    expect(copyText({ identity: id, blockId: 'content', renderer: 'text', pages: [page({ text: 'plain' })], nextCursor: null, droppedBefore: 0, bytes: 0, at: 0 })).toBe('plain')
    expect(copyText({ identity: id, blockId: 'raw', renderer: 'json', pages: [page({ value: { a: 1 } })], nextCursor: null, droppedBefore: 0, bytes: 0, at: 0 })).toBe('{\n  "a": 1\n}')
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
