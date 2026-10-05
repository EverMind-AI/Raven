// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import { get as toasts } from '../../state/toast'
import { BlockView, copyText, JsonView, KeyValuesView } from './BlockView'
import * as details from './detailStore'
import * as list from './store'

import type {
  TrajectoryBlockDescriptor, TrajectoryBlockResult, TrajectoryDetailResult, TrajectoryEntry, TrajectoryIndexState,
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
let blockCalls: Array<string | null> = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: [row], next_cursor: null, index_state: READY, complete: true }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: async () => descriptor,
  block: (_k, _e, _r, _ep, _b, cursor) => { blockCalls.push(cursor ?? null); const d = deferred<TrajectoryBlockResult>(); blockQueue.push(d); return d.promise },
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }) }
const answer = async (r: TrajectoryBlockResult): Promise<void> => { await act(async () => { blockQueue.shift()!.resolve(r) }); await flush() }
const q = (sel: string): HTMLElement | null => document.querySelector<HTMLElement>(sel)

const messages = (from: number, n: number) => Array.from({ length: n }, (_, k) => ({ role: 'user', content: `m${from + k}` }))

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
  it('reads its first page when shown, pages on request in order, and copies only what it holds', async () => {
    await ready()
    const spec = descriptor.blocks[0]!
    const writeText = vi.fn((_text: string) => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<BlockView block={spec} />)
    await flush()
    expect(blockCalls).toEqual([null])
    await answer(body('messages', { items: messages(0, 20), offset: 0 }, { renderer: 'messages', next_cursor: 'c1', total_items: 45 }))
    expect(document.querySelectorAll('.trajectory-msg')).toHaveLength(20)
    expect(q('.trajectory-tool-warn')?.textContent).toBe('gui.trajectory.details.partial')
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(JSON.parse(writeText.mock.calls[0]![0])).toHaveLength(20)
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied_partial')
    act(() => { fireEvent.click(q('.trajectory-more .trajectory-link') as HTMLElement) })
    await flush()
    expect(blockCalls).toEqual([null, 'c1'])
    await answer(body('messages', { items: messages(20, 20), offset: 20 }, { renderer: 'messages', next_cursor: 'c2', total_items: 45 }))
    const texts = [...document.querySelectorAll('.trajectory-msg .trajectory-text-body')].map((m) => m.textContent)
    expect(texts[0]).toBe('m0')
    expect(texts[39]).toBe('m39')
    await act(async () => { fireEvent.click(q('.trajectory-more .trajectory-link') as HTMLElement) })
    await answer(body('messages', { items: messages(40, 5), offset: 40 }, { renderer: 'messages', next_cursor: null, total_items: 45 }))
    expect(q('.trajectory-more')).toBeNull()
    expect(q('.trajectory-tool-warn')).toBeNull()
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied')
    expect(JSON.parse(writeText.mock.calls[1]![0])).toHaveLength(45)
  })

  it('still says partial, and copies partial, when earlier pages were let go even though the last page is in', async () => {
    await ready()
    const writeText = vi.fn((_text: string) => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<BlockView block={descriptor.blocks[0]!} />)
    await flush()
    const total = (details.PAGE_WINDOW + 2) * 20
    for (let page = 0; page < details.PAGE_WINDOW + 2; page += 1) {
      const last = page === details.PAGE_WINDOW + 1
      await answer(body('messages', { items: messages(page * 20, 20), offset: page * 20 }, { renderer: 'messages', next_cursor: last ? null : `c${page + 1}`, total_items: total }))
      if (!last) await act(async () => { fireEvent.click(q('.trajectory-more .trajectory-link') as HTMLElement) })
    }
    const record = details.block('messages')!
    expect(record.nextCursor).toBeNull()
    expect(record.pages.every((p) => !p.truncated)).toBe(true)
    expect(record.droppedBefore).toBe(40)
    expect(q('.trajectory-tool-warn')?.textContent).toBe('gui.trajectory.details.partial')
    expect(document.body.textContent).toContain('gui.trajectory.details.earlier_unloaded {"n":40}')
    expect(q('.trajectory-msgs > div')?.getAttribute('data-offset')).toBe('40')
    act(() => { fireEvent.click(q('.trajectory-copy') as HTMLElement) })
    await flush()
    expect(toasts().at(-1)?.text).toBe('gui.trajectory.details.copied_partial')
    expect(JSON.parse(writeText.mock.calls[0]![0])).toHaveLength(details.PAGE_WINDOW * 20)
    /* Reading again from the start drops the window and asks for the first page. */
    const asked = blockCalls.length
    await act(async () => { fireEvent.click(q('.trajectory-tool-note .trajectory-link') as HTMLElement) })
    expect(blockCalls[asked]).toBeNull()
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
    expect(blockCalls).toEqual([null])
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
    expect(blockCalls).toEqual([null])
  })

  it('copies text as text and JSON as pretty JSON', () => {
    const id = { sessionKey: 'gui:a', epoch: 'e1', entryId: 'r0', revision: 1 }
    const page = (data: unknown) => ({ offset: 0, data: data as never, availability: 'available' as const, reason: null, integrity: [], truncated: false, total: null })
    expect(copyText({ identity: id, blockId: 'content', renderer: 'text', pages: [page({ text: 'plain' })], nextCursor: null, droppedBefore: 0, bytes: 0, at: 0 })).toBe('plain')
    expect(copyText({ identity: id, blockId: 'raw', renderer: 'json', pages: [page({ value: { a: 1 } })], nextCursor: null, droppedBefore: 0, bytes: 0, at: 0 })).toBe('{\n  "a": 1\n}')
  })
})
