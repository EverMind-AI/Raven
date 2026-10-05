// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { dispatch as escape } from '../../state/escapeOrder'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import { shortName } from './Details'
import * as details from './detailStore'
import * as list from './store'
import { TrajectoryApp } from './TrajectoryApp'

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

const entry = (id: string, at: number, kind = 'tool.output'): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind, span_name: 'tool.call', slot: 'tool.output', trace_id: 't', span_id: id,
  parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: id, operation_status: 'ok',
  status_evidence: [], failure_entry: false, integrity: [], operation_start: null, operation_end: null,
  duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null, meta: {},
})

const block = (over: Partial<TrajectoryBlockDescriptor> & Pick<TrajectoryBlockDescriptor, 'id'>): TrajectoryBlockDescriptor => ({
  renderer: 'text', availability: 'available', preview: null, total_items: null, related_operation: null, reason: null, ...over,
})

const descriptor = (entryId: string, blocks: TrajectoryBlockDescriptor[], over: Partial<TrajectoryDetailResult> = {}): TrajectoryDetailResult => ({
  session_key: 'gui:a', epoch: 'e1', entry_id: entryId, entry_revision: 1, kind: 'tool.output', span_name: 'tool.call',
  slot: 'tool.output', operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], notes: [],
  blocks, revision_changed: false, truncated: false, ...over,
})

const body = (entryId: string, blockId: string, data: unknown, over: Partial<TrajectoryBlockResult> = {}): TrajectoryBlockResult => ({
  entry_id: entryId, entry_revision: 1, epoch: 'e1', block_id: blockId, renderer: 'text', availability: 'available', reason: null,
  data: data as TrajectoryBlockResult['data'], next_cursor: null, total_items: null, integrity: [], truncated: false, ...over,
})

let detailQueue: Array<Deferred<TrajectoryDetailResult>> = []
let blockQueue: Array<Deferred<TrajectoryBlockResult>> = []
let blockCalls: string[] = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({
    epoch: 'e1', snapshot_revision: 100, entries: [entry('r0', 0), entry('r1', 1, 'user.input'), entry('r2', 2)],
    next_cursor: null, index_state: READY, complete: true,
  }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: () => { const d = deferred<TrajectoryDetailResult>(); detailQueue.push(d); return d.promise },
  block: (_k, _e, _r, _ep, blockId) => { blockCalls.push(blockId); const d = deferred<TrajectoryBlockResult>(); blockQueue.push(d); return d.promise },
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }) }
const answerDetail = async (r: TrajectoryDetailResult): Promise<void> => { await act(async () => { detailQueue.shift()!.resolve(r) }); await flush() }
const failDetail = async (e: unknown): Promise<void> => { await act(async () => { detailQueue.shift()!.reject(e) }); await flush() }
const answerBlock = async (r: TrajectoryBlockResult): Promise<void> => { await act(async () => { blockQueue.shift()!.resolve(r) }); await flush() }

const q = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const pane = (): HTMLElement | null => q('.trajectory-details')
const tabs = (): string[] => [...document.querySelectorAll('.trajectory-tab')].map((b) => b.textContent ?? '')

async function draw(): Promise<void> {
  render(<TrajectoryApp />)
  await act(async () => {
    await list.refreshState()
    list.setView('trajectory')
    await list.load()
  })
}

const pick = async (id: string): Promise<void> => {
  act(() => { fireEvent.click(q(`[data-entry="${id}"]`) as HTMLElement) })
  await flush()
}

beforeEach(() => {
  detailQueue = []
  blockQueue = []
  blockCalls = []
  list._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
  list.install()
  details.install()
  unpitch()
  list.sessionChanged('gui:a')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the details pane', () => {
  it('opens on a row, reads the descriptor, and draws the overview and a tab per block', async () => {
    await draw()
    expect(pane()).toBeNull()
    await pick('r1')
    expect(pane()).not.toBeNull()
    expect(q('.trajectory-skel')).not.toBeNull()
    await answerDetail(descriptor('r1', [
      block({ id: 'content', preview: 'hello there' }),
      block({ id: 'origin', renderer: 'key_values', preview: [{ key: 'channel', value: 'gui' }], total_items: 1 }),
      block({ id: 'timing', renderer: 'key_values' }),
      block({ id: 'raw', renderer: 'json' }),
    ], { kind: 'user.input', span_name: 'session.turn' }))
    expect(tabs()).toEqual([
      'gui.trajectory.details.overview', 'gui.trajectory.block.content', 'gui.trajectory.block.origin',
      'gui.trajectory.block.timing', 'gui.trajectory.block.raw',
    ])
    const headings = [...document.querySelectorAll('.trajectory-h')].map((h) => h.textContent)
    expect(headings).toEqual(['gui.trajectory.block.content', 'gui.trajectory.block.origin', 'gui.trajectory.block.timing', 'gui.trajectory.block.raw'])
    expect(q('.trajectory-sec-body .trajectory-text-body')?.textContent).toBe('hello there')
    expect(q('.trajectory-head-name')?.textContent).toBe('session.turn')
    expect(q('.trajectory-info .trajectory-status-ok')?.textContent).toBe('gui.trajectory.status.ok')
    /* The overview asks for no body. */
    expect(blockCalls).toEqual([])
  })

  it('jumps to a tab from its heading, reads that block once, and keeps each tab\'s scroll', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result', preview: 'short' }), block({ id: 'timing', renderer: 'key_values' })]))
    act(() => { fireEvent.click(q('#trajectory-h-result') as HTMLElement) })
    await flush()
    expect(details.tabOf('r0')).toBe('result')
    expect(q('#trajectory-tab-result')?.getAttribute('aria-selected')).toBe('true')
    expect(blockCalls).toEqual(['result'])
    expect(q('.trajectory-block .trajectory-skel')).not.toBeNull()
    await answerBlock(body('r0', 'result', { text: 'the whole result' }))
    expect(q('.trajectory-block .trajectory-text-body')?.textContent).toBe('the whole result')
    const scroller = q('.trajectory-pane') as HTMLElement
    Object.defineProperty(scroller, 'scrollHeight', { value: 2000, configurable: true })
    scroller.scrollTop = 120
    act(() => { fireEvent.scroll(scroller) })
    expect(details.tabScroll('r0', 'result')).toBe(120)
    act(() => { fireEvent.click(q('#trajectory-tab-overview') as HTMLElement) })
    await flush()
    act(() => { fireEvent.click(q('#trajectory-tab-result') as HTMLElement) })
    await flush()
    expect(blockCalls).toEqual(['result'])
    expect((q('.trajectory-pane') as HTMLElement).scrollTop).toBe(120)
  })

  it('moves between tabs with the arrow keys, Home and End', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' }), block({ id: 'params', renderer: 'json' })]))
    const strip = q('.trajectory-tabs') as HTMLElement
    act(() => { fireEvent.keyDown(strip, { key: 'ArrowRight' }) })
    expect(details.tabOf('r0')).toBe('result')
    act(() => { fireEvent.keyDown(strip, { key: 'End' }) })
    expect(details.tabOf('r0')).toBe('params')
    act(() => { fireEvent.keyDown(strip, { key: 'ArrowLeft' }) })
    expect(details.tabOf('r0')).toBe('result')
    act(() => { fireEvent.keyDown(strip, { key: 'Home' }) })
    expect(details.tabOf('r0')).toBe('overview')
    expect(q('.trajectory-tabs')?.getAttribute('role')).toBe('tablist')
  })

  it('says why a block has no body, offers see-all for a cut preview, and shows a false and an empty value as values', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [
      block({ id: 'schema', renderer: 'json', availability: 'not_recorded', reason: 'schema_unproven', related_operation: 'llm.input' }),
      block({ id: 'result', preview: 'a long text that was cut…' }),
      block({ id: 'tools', renderer: 'items', preview: ['a', 'b', 'c'], total_items: 12 }),
      block({ id: 'finish', renderer: 'key_values', preview: [{ key: 'truncated', value: false }, { key: 'reason', value: '' }, { key: 'n', value: 0 }], total_items: 3 }),
    ]))
    expect(q('.trajectory-sec[aria-labelledby="trajectory-h-schema"] .trajectory-sec-note')?.textContent).toBe('gui.trajectory.reason.schema_unproven')
    expect(q('.trajectory-sec[aria-labelledby="trajectory-h-schema"] .trajectory-h-from')?.textContent).toBe('gui.trajectory.details.from_operation {"op":"llm.input"}')
    const seeAll = [...document.querySelectorAll('.trajectory-see-all')].map((b) => b.textContent)
    expect(seeAll).toEqual(['gui.trajectory.details.see_all', 'gui.trajectory.details.see_all_n {"n":12}'])
    const finish = q('.trajectory-sec[aria-labelledby="trajectory-h-finish"]') as HTMLElement
    /* false, the empty string and zero are values, each said as itself. */
    expect([...finish.querySelectorAll('.trajectory-kv-v')].map((v) => v.textContent)).toEqual([
      'false', 'gui.trajectory.details.empty_string', '0',
    ])
    expect(q('#trajectory-tab-schema .trajectory-tab-dot')).not.toBeNull()
  })

  it('keeps the same tab across entries that have it and falls back to the overview otherwise', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' }), block({ id: 'params', renderer: 'json' })]))
    act(() => { fireEvent.click(q('#trajectory-tab-params') as HTMLElement) })
    await flush()
    await pick('r2')
    expect(q('.trajectory-skel')).not.toBeNull()
    await answerDetail(descriptor('r2', [block({ id: 'result' }), block({ id: 'params', renderer: 'json' })]))
    /* Each entry keeps its own tab; a first visit starts on the overview. */
    expect(details.tabOf('r2')).toBe('overview')
    expect(details.tabOf('r0')).toBe('params')
    await pick('r0')
    expect(q('#trajectory-tab-params')?.getAttribute('aria-selected')).toBe('true')
  })

  it('shows the later pick when two are made in quick succession, never the earlier one\'s answer', async () => {
    await draw()
    await pick('r0')
    const first = detailQueue.shift()!
    await pick('r2')
    await act(async () => { first.resolve(descriptor('r0', [block({ id: 'result', preview: 'r0 body' })])) })
    await flush()
    expect(q('.trajectory-skel')).not.toBeNull()
    expect(document.body.textContent).not.toContain('r0 body')
    await answerDetail(descriptor('r2', [block({ id: 'result', preview: 'r2 body' })]))
    expect(q('.trajectory-sec-body .trajectory-text-body')?.textContent).toBe('r2 body')
  })

  it('closes with the button and with Escape only while it holds the focus, handing the focus back to the list', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    const listBox = q('.trajectory-list') as HTMLElement
    listBox.focus()
    expect(escape()).toBe(false)
    expect(details.get().open).toBe(true)
    ;(q('#trajectory-tab-overview') as HTMLElement).focus()
    act(() => { expect(escape()).toBe(true) })
    expect(details.get().open).toBe(false)
    expect(pane()).toBeNull()
    expect(list.get().selectedId).toBe('r0')
    expect(document.activeElement).toBe(listBox)
    /* Clicking the selected row again reopens it. */
    await pick('r0')
    expect(pane()).not.toBeNull()
    act(() => { fireEvent.click(q('.trajectory-close') as HTMLElement) })
    expect(pane()).toBeNull()
  })

  it('lays the pane over the list under 800px with a back button, and beside it with a grip otherwise', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { details.setAreaWidth(1000) })
    expect(q('.trajectory-grip')).not.toBeNull()
    expect(pane()?.className).toBe('trajectory-details')
    expect(pane()?.style.width).toBe('480px')
    const grip = q('.trajectory-grip') as HTMLElement
    act(() => { fireEvent.keyDown(grip, { key: 'ArrowLeft' }) })
    expect(pane()?.style.width).toBe(`${480 + details.RESIZE_STEP}px`)
    act(() => { fireEvent.keyDown(grip, { key: 'ArrowRight' }) })
    act(() => { fireEvent.keyDown(grip, { key: 'ArrowRight' }) })
    expect(pane()?.style.width).toBe(`${480 - details.RESIZE_STEP}px`)
    act(() => { details.setAreaWidth(700) })
    expect(q('.trajectory-grip')).toBeNull()
    expect(pane()?.className).toBe('trajectory-details trajectory-details-over')
    expect(q('.trajectory-close')?.textContent).toBe('gui.trajectory.details.back')
    expect(q('.trajectory-body')?.hasAttribute('data-narrow')).toBe(true)
    /* The list is still there, under the pane. */
    expect(q('.trajectory-list')).not.toBeNull()
  })

  it('says when the descriptor could not be read and lets the reader retry', async () => {
    await draw()
    await pick('r0')
    await failDetail(new RpcError(-32603, 'boom'))
    expect(q('.trajectory-fault')?.textContent).toContain('gui.trajectory.details.failed {"detail":"boom"}')
    act(() => { fireEvent.click(q('.trajectory-fault .trajectory-link') as HTMLElement) })
    await flush()
    expect(detailQueue).toHaveLength(1)
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    expect(q('.trajectory-fault')).toBeNull()
    expect(tabs()).toHaveLength(2)
  })
})

describe('the details pane when it must wait', () => {
  it('asks once for a descriptor the list has not reached, says it is waiting, and asks again when the list arrives', async () => {
    await draw()
    await pick('r0')
    expect(detailQueue).toHaveLength(1)
    await answerDetail(descriptor('r0', [block({ id: 'result' })], { epoch: 'e2' }))
    expect(details.get().waitingEpoch).toBe('e2')
    expect(q('.trajectory-sec-note')?.textContent).toBe('gui.trajectory.details.waiting_list')
    await flush()
    await flush()
    expect(detailQueue).toHaveLength(0)
    /* The list reaches the epoch: the wait ends and the descriptor is read again. */
    act(() => { list.set({ ...list.get(), epoch: 'e2', revision: 1 }) })
    await flush()
    expect(details.get().waitingEpoch).toBeNull()
    expect(detailQueue).toHaveLength(1)
    await answerDetail(descriptor('r0', [block({ id: 'result' })], { epoch: 'e2' }))
    expect(tabs()).toHaveLength(2)
  })

  it('asks for nothing while the conversation view is up, and picks up again on return', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { fireEvent.click(q('#trajectory-h-result') as HTMLElement) })
    await flush()
    const pending = blockQueue.shift()!
    act(() => { list.setView('chat') })
    await act(async () => { pending.resolve(body('r0', 'result', { text: 'landed late' })) })
    await flush()
    await flush()
    expect(blockCalls).toEqual(['result'])
    expect(details.get().open).toBe(true)
    act(() => { list.setView('trajectory') })
    await flush()
    /* The body that landed is held, so coming back asks for nothing either. */
    expect(blockCalls).toEqual(['result'])
    expect(q('.trajectory-block .trajectory-text-body')?.textContent).toBe('landed late')
  })

  it('asks for nothing once the switch is off, and keeps what it had for when it is on again', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' }), block({ id: 'params', renderer: 'json' })]))
    act(() => { list.disabledByServer() })
    expect(list.get().view).toBe('chat')
    act(() => { details.setTab('r0', 'params') })
    await flush()
    expect(blockCalls).toEqual([])
    expect(details.descriptor()).not.toBeNull()
  })
})

describe('the details pane when the entry moves on', () => {
  it('reads the new descriptor first and the new revision\'s block after, never the old block again', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { fireEvent.click(q('#trajectory-h-result') as HTMLElement) })
    await flush()
    expect(blockCalls).toEqual(['result'])
    const moved = new RpcError(-32023, 'moved', { current_revision: 2, current_epoch: 'e1' })
    await act(async () => { blockQueue.shift()!.reject(moved) })
    await flush()
    await flush()
    /* The new descriptor is in the air; no block is asked for meanwhile. */
    expect(detailQueue).toHaveLength(1)
    expect(blockCalls).toEqual(['result'])
    expect(details.get().stale).toBe(true)
    expect(details.get().pendingRevision).toBe(2)
    await answerDetail(descriptor('r0', [block({ id: 'result' })], { entry_revision: 2 }))
    expect(details.get().current?.revision).toBe(2)
    expect(details.get().stale).toBe(false)
    expect(blockCalls).toEqual(['result', 'result'])
    await answerBlock({ ...body('r0', 'result', { text: 'at two' }), entry_revision: 2 })
    expect(q('.trajectory-block .trajectory-text-body')?.textContent).toBe('at two')
  })

  it('keeps the requests bounded when the entry keeps moving, and stops at the limit', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { fireEvent.click(q('#trajectory-h-result') as HTMLElement) })
    await flush()
    let revision = 1
    for (let hop = 0; hop < details.REVISION_RETRIES + 2; hop += 1) {
      if (!blockQueue.length) break
      revision += 1
      await act(async () => { blockQueue.shift()!.reject(new RpcError(-32023, 'moved', { current_revision: revision, current_epoch: 'e1' })) })
      await flush()
      if (detailQueue.length) await answerDetail(descriptor('r0', [block({ id: 'result' })], { entry_revision: revision }))
    }
    expect(details.get().unstable).toBe(true)
    expect(detailQueue).toHaveLength(0)
    expect(blockQueue).toHaveLength(0)
    /* One descriptor and one block per accepted hop, and then nothing. */
    expect(blockCalls.length).toBeLessThanOrEqual(details.REVISION_RETRIES + 2)
    await flush()
    await flush()
    expect(blockCalls.length).toBeLessThanOrEqual(details.REVISION_RETRIES + 2)
    expect(q('.trajectory-fault')?.textContent).toContain('gui.trajectory.details.unstable')
  })
})

describe('shortName', () => {
  it('prefers the tool, skill, model or plugin name a block carries over the span name', () => {
    expect(shortName(descriptor('x', [block({ id: 'tool', renderer: 'key_values', preview: [{ key: 'name', value: 'read_file' }] })]))).toBe('read_file')
    expect(shortName(descriptor('x', [block({ id: 'model', renderer: 'key_values', preview: [{ key: 'model', value: 'gpt-x' }] })]))).toBe('gpt-x')
    expect(shortName(descriptor('x', [block({ id: 'content' })]))).toBe('tool.call')
  })
})
