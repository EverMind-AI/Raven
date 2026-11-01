// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/* This file forces many renders; under a full-suite run they share the CPU
   with every other worker, so the wall clock is not the thing under test. */
vi.setConfig({ testTimeout: 20_000 })

/* How often the rows are drawn: the real waterfall, counted. */
const renders = vi.hoisted(() => ({ waterfall: 0 }))
vi.mock('./waterfall/Waterfall', async (importOriginal) => {
  const real = await importOriginal<typeof import('./waterfall/Waterfall')>()
  return {
    ...real,
    Waterfall: () => {
      renders.waterfall += 1
      return real.Waterfall()
    },
  }
})

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { dispatch as escape } from '../../state/escapeOrder'
import { _resetForTests as resetBars, onShowBar } from '../../state/scrollbars'
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

/* Agents start folded when there are several, and turns until asked; a test
   that wants a row opens them all first -- the agents in an act of their own,
   so their turns are drawn by the time the second one looks for them. */
const openAgents = (): void => {
  for (const agent of document.querySelectorAll<HTMLElement>('[data-row="agent"][aria-expanded="false"]')) fireEvent.click(agent)
}
const openTurns = (): void => {
  for (const turn of document.querySelectorAll<HTMLElement>('[data-row="turn"]:not([data-open])')) fireEvent.click(turn)
}

const pick = async (id: string): Promise<void> => {
  act(openAgents)
  act(openTurns)
  await flush()
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

  it('floats the pane over the list, sized by its corner alone, and fills the area under 800px with a back button', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { details.setAreaWidth(1000) })
    /* Nothing stands between the list and the pane, which floats over the
       list rather than beside it; like the desk palette, it sizes by its
       corner and has no edge to pull. */
    expect([...(q('.trajectory-body')?.children ?? [])].map((el) => el.className.split(' ')[0])).toEqual(['trajectory-wf', 'trajectory-details'])
    expect(pane()?.className).toBe('trajectory-details')
    expect(pane()?.style.width).toBe('480px')
    expect(pane()?.querySelector(':scope > .trajectory-corner')).not.toBeNull()
    expect(pane()?.querySelector('[role="separator"]')).toBeNull()
    act(() => { details.setAreaWidth(700) })
    expect(q('.trajectory-corner')).toBeNull()
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

/* The head moves the pane once a press travels past its threshold, and the
   corner sizes it. happy-dom lays nothing out, so the area and the
   pane are given the rectangles the stylesheet would give them, and the pane
   the header height it opens under. */
describe('moving and sizing the pane', () => {
  const card = (): HTMLElement => pane() as HTMLElement
  const head = (): HTMLElement => q('.trajectory-head') as HTMLElement
  const corner = (): HTMLElement | null => q('.trajectory-corner')
  /* Where the stylesheet opens a 480px pane in a 1200x800 area: against the
     right side, below the whole header -- the 96px stats, the 92px activity
     strip and the 56px ruler, then the 10px gap (`--wf-card-top`, 254px). */
  const HOME = { x: 710, y: 254, w: 480, h: 536 }
  let captured = false
  const rect = (x: number, y: number, width: number, height: number): DOMRect =>
    ({ x, y, left: x, top: y, width, height, right: x + width, bottom: y + height, toJSON: () => ({}) }) as DOMRect
  const lay = (at: { x: number; y: number; w: number; h: number }): void => {
    q<HTMLElement>('.trajectory-body')!.getBoundingClientRect = () => rect(0, 0, 1200, 800)
    card().getBoundingClientRect = () => rect(at.x, at.y, at.w, at.h)
    card().style.setProperty('--wf-card-top', '254px')
    for (const el of [head(), corner()]) {
      if (!el) continue
      el.setPointerCapture = () => { captured = true }
      el.hasPointerCapture = () => captured
      el.releasePointerCapture = () => { captured = false }
    }
  }
  const press = async (on: Element, x: number, y: number): Promise<void> => {
    captured = false
    await act(async () => {
      on.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerId: 1, clientX: x, clientY: y }))
    })
  }
  const to = async (x: number, y: number): Promise<void> => {
    await act(async () => { window.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 1, clientX: x, clientY: y })) })
  }
  const release = async (x: number, y: number): Promise<void> => {
    await act(async () => { window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 1, clientX: x, clientY: y })) })
  }
  const click = async (on: Element): Promise<void> => {
    await act(async () => { on.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })) })
  }
  const open = async (): Promise<void> => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => {
      details.setAreaWidth(1200)
      details.setAreaHeight(800)
    })
  }

  it('moves by its head once the press travels, and the click that ends the drag closes nothing', async () => {
    await open()
    lay(HOME)
    const close = q('.trajectory-close') as HTMLElement
    /* The drag starts on the head's name: a press on one of its buttons belongs
       to that button. */
    await press(q('.trajectory-head-name') as HTMLElement, 900, 120)
    await to(600, 64)
    expect(captured).toBe(true)
    expect(card().style.transform).toBe('translate(-300px, -56px)')
    await release(600, 64)
    await click(close)
    expect(details.get().open).toBe(true)
    expect(card().style.transform).toBe('')
    expect([card().style.left, card().style.top, card().style.height]).toEqual(['410px', '198px', '536px'])
    expect(card().hasAttribute('data-moved')).toBe(true)
  })

  /* A drag that ended without a click leaves its swallow flag set; the next
     press on a button must clear it, or that button's click is eaten. */
  it('gives the next button its click after a drag that ended without one', async () => {
    await open()
    lay(HOME)
    await press(q('.trajectory-head-name') as HTMLElement, 900, 120)
    await to(600, 64)
    await release(600, 64)
    expect(card().hasAttribute('data-moved')).toBe(true)
    const close = q('.trajectory-close') as HTMLElement
    await press(close, 900, 120)
    await click(close)
    expect(details.get().open).toBe(false)
  })

  it('leaves the head\'s buttons their click when the press barely moves', async () => {
    await open()
    lay(HOME)
    const close = q('.trajectory-close') as HTMLElement
    await press(close, 900, 120)
    await to(901, 121)
    await release(901, 121)
    expect(captured).toBe(false)
    await click(close)
    expect(details.get().open).toBe(false)
  })

  it('goes back where it opens when let go near there, filling the room under the header again', async () => {
    await open()
    lay(HOME)
    const name = q('.trajectory-head-name') as HTMLElement
    await press(name, 900, 120)
    await to(600, 64)
    await release(600, 64)
    expect(card().style.left).toBe('410px')
    lay({ x: 410, y: 198, w: 480, h: 536 })
    await press(name, 600, 64)
    await to(880, 100)
    await release(880, 100)
    expect(card().hasAttribute('data-moved')).toBe(false)
    expect([card().style.left, card().style.top, card().style.height, card().style.transform]).toEqual(['', '', '', ''])
  })

  it('sizes by its corner, pulled left where it opens and right once moved, never past the area', async () => {
    await open()
    lay(HOME)
    await press(corner()!, 712, 788)
    await to(672, 688)
    await release(672, 688)
    expect([card().style.width, card().style.height]).toEqual(['520px', '436px'])
    act(() => { details.dropDetails({ x: 100, y: 100 }, 436, 254) })
    lay({ x: 100, y: 100, w: 520, h: 436 })
    await press(corner()!, 618, 682)
    await to(658, 702)
    expect([card().style.width, card().style.height]).toEqual(['560px', '456px'])
    /* Pulled past the bottom, it stops there rather than climbing. */
    await to(658, 2000)
    await release(658, 2000)
    expect([card().style.left, card().style.top, card().style.height]).toEqual(['100px', '100px', '690px'])
  })

  it('sizes from the keyboard too: the corner takes the focus and each arrow moves a free edge 16px', async () => {
    await open()
    lay(HOME)
    const grip = corner()!
    expect(grip.tabIndex).toBe(0)
    expect(grip.getAttribute('aria-label')).toBeTruthy()
    const key = async (name: string): Promise<void> => {
      await act(async () => { fireEvent.keyDown(grip, { key: name }) })
    }
    /* Where it opens the left edge is the free one, and the bottom already
       meets the area, so the pane grows left and only shrinks upward. */
    await key('ArrowLeft')
    await key('ArrowUp')
    expect([card().style.width, card().style.height]).toEqual(['496px', '520px'])
    act(() => { details.dropDetails({ x: 100, y: 100 }, 520, 254) })
    lay({ x: 100, y: 100, w: 496, h: 520 })
    /* Moved, the right edge is the free one. */
    await key('ArrowRight')
    await key('ArrowDown')
    expect([card().style.width, card().style.height]).toEqual(['512px', '536px'])
  })

  it('tells assistive technology both sizes the corner sets, as a control rather than a one-way range', async () => {
    await open()
    lay(HOME)
    const grip = corner()!
    /* A focusable separator is a range along one axis; this handle sets two. */
    expect(grip.getAttribute('role')).toBe('button')
    const described = (): string => (grip.getAttribute('aria-describedby') ?? '').split(' ')
      .map((id) => document.getElementById(id)?.textContent ?? '').join(' ')
    const spoken = (): string => grip.querySelector('[aria-live="polite"]')?.textContent ?? ''
    /* Where it opens, the card fills the room under the header. */
    expect(described()).toBe('gui.trajectory.details.size_full {"width":480} gui.trajectory.details.resize_keys')
    /* The hand's pulls are not read out, move by move. */
    await press(grip, 712, 788)
    await to(702, 778)
    await release(702, 778)
    expect(spoken()).toBe('')
    lay({ ...HOME, x: 700, w: 490, h: 526 })
    await act(async () => { fireEvent.keyDown(grip, { key: 'ArrowUp' }) })
    expect(described()).toBe('gui.trajectory.details.size {"width":490,"height":510} gui.trajectory.details.resize_keys')
    expect(spoken()).toBe('gui.trajectory.details.size {"width":490,"height":510}')
  })

  it('neither moves nor sizes under 800px, where it covers the area', async () => {
    await open()
    act(() => { details.setAreaWidth(700) })
    expect(corner()).toBeNull()
    lay(HOME)
    await press(head(), 300, 40)
    await to(100, 200)
    /* Watched mid-drag: let go, a narrow area's clamp lands any card on its home. */
    expect([card().style.transform, captured]).toEqual(['', false])
    await release(100, 200)
    expect(details.detailsPlace()).toBeNull()
  })

  /* The bars are parked in a fixed layer and placed from the scroller's box,
     so a card that moves or changes size has to ask for its pane's bar again. */
  it('takes the pane\'s bar along when it moves or changes size', async () => {
    const asked: HTMLElement[] = []
    onShowBar((el) => { asked.push(el) })
    try {
      await open()
      lay(HOME)
      const bars = (): number => asked.filter((el) => el.classList.contains('trajectory-pane')).length
      let before = bars()
      await press(head(), 900, 120)
      await to(880, 130)
      expect(bars()).toBeGreaterThan(before)
      /* Let go near where it opened: the place is unchanged, the bar has moved. */
      before = bars()
      await release(880, 130)
      expect(details.detailsPlace()).toBeNull()
      expect(bars()).toBeGreaterThan(before)
      before = bars()
      await press(corner()!, 712, 788)
      await to(672, 688)
      await release(672, 688)
      expect(bars()).toBeGreaterThan(before)
    } finally {
      resetBars()
    }
  })

  /* The rows do not depend on the card's size, and the card's corner writes the
     store on every pointer move: redrawing every row each frame was waste. */
  it('redraws only the card while its corner is pulled, not the rows under it', async () => {
    await open()
    lay(HOME)
    const before = renders.waterfall
    await press(corner()!, 712, 788)
    await to(692, 768)
    await to(672, 748)
    await release(672, 748)
    expect(card().style.width).toBe('520px')
    expect(renders.waterfall).toBe(before)
  })

  it('lets a drag go when the pane closes under it', async () => {
    await open()
    lay(HOME)
    await press(head(), 900, 120)
    await to(600, 64)
    act(() => { details.closeDetails() })
    await release(600, 64)
    expect(details.detailsPlace()).toBeNull()
  })

  /* A press on one of the head's buttons belongs to that button, not to the
     drag: it must not be canceled -- canceling a pointerdown suppresses the
     mousedown that focuses the button -- and the pane has to hold the focus, or
     Escape falls past it to the turn behind. */
  it('leaves the focus on a head button when it is pressed, so Escape takes back the pane', async () => {
    await open()
    lay(HOME)
    const pin = q('.trajectory-pin') as HTMLElement
    await press(pin, 900, 130)
    expect(document.activeElement).toBe(pin)
    expect(details.hasFocus()).toBe(true)
    act(() => { expect(escape()).toBe(true) })
    expect(details.get().open).toBe(false)
  })

  it('takes the focus itself when its head background is pressed, so Escape reaches it there too', async () => {
    await open()
    lay(HOME)
    await press(head(), 300, 270)
    expect(document.activeElement).toBe(card())
    expect(details.hasFocus()).toBe(true)
    act(() => { expect(escape()).toBe(true) })
    expect(details.get().open).toBe(false)
  })

  it('takes the focus when its corner is pressed, so the arrows land on the handle', async () => {
    await open()
    lay(HOME)
    await press(corner()!, 712, 788)
    expect(document.activeElement).toBe(corner())
    expect(details.hasFocus()).toBe(true)
  })

  /* A pull that only moves the width must not freeze the height the card was
     filling: a stored number reads as the reader's own, and the card would stop
     growing with the room under the header. */
  it('keeps an auto height through a pull that only moves its width', async () => {
    await open()
    lay(HOME)
    await press(corner()!, 712, 788)
    await to(672, 788)
    await release(672, 788)
    expect(card().style.width).toBe('520px')
    expect(details.detailsHeightPx()).toBeNull()
    expect(card().style.height).toBe('')
    act(() => { details.setAreaHeight(1000) })
    expect(details.detailsHeightPx()).toBeNull()
    expect(card().style.height).toBe('')
  })

  it('keeps an auto height through an arrow that only moves its width', async () => {
    await open()
    lay(HOME)
    const grip = corner()!
    await act(async () => { fireEvent.keyDown(grip, { key: 'ArrowLeft' }) })
    expect(card().style.width).toBe('496px')
    expect(details.detailsHeightPx()).toBeNull()
    act(() => { details.setAreaHeight(1000) })
    expect(details.detailsHeightPx()).toBeNull()
    expect(card().style.height).toBe('')
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
    expect(details.get().pending).toEqual({ epoch: 'e1', revision: 2 })
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

describe('the details pane across a rebuilt index', () => {
  it('takes the new epoch\'s descriptor once a page from the old epoch had pushed the bound up, then reads the block once', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', [block({ id: 'result' })]))
    act(() => { fireEvent.click(q('#trajectory-h-result') as HTMLElement) })
    await flush()
    expect(blockCalls).toEqual(['result'])
    await act(async () => { blockQueue.shift()!.reject(new RpcError(-32023, 'moved', { current_revision: 50, current_epoch: 'e1' })) })
    await flush()
    expect(detailQueue).toHaveLength(1)
    await answerDetail(descriptor('r0', [block({ id: 'result' })], { epoch: 'e2', entry_revision: 1 }))
    expect(details.get().waitingEpoch).toBe('e2')
    await flush()
    await flush()
    expect(detailQueue).toHaveLength(0)
    expect(blockCalls).toEqual(['result'])
    /* The list arrives at the new epoch with the row at revision one again. */
    act(() => { list.set({ ...list.get(), epoch: 'e2', revision: 1 }) })
    await flush()
    expect(detailQueue).toHaveLength(1)
    await answerDetail(descriptor('r0', [block({ id: 'result' })], { epoch: 'e2', entry_revision: 1 }))
    expect(details.get().current).toEqual({ sessionKey: 'gui:a', epoch: 'e2', entryId: 'r0', revision: 1 })
    expect(details.get().stale).toBe(false)
    await flush()
    await flush()
    expect(detailQueue).toHaveLength(0)
    expect(blockCalls).toEqual(['result', 'result'])
    await answerBlock({ ...body('r0', 'result', { text: 'rebuilt' }), epoch: 'e2', entry_revision: 1 })
    expect(q('.trajectory-block .trajectory-text-body')?.textContent).toBe('rebuilt')
    await flush()
    expect(blockCalls).toEqual(['result', 'result'])
  })
})

describe('shortName', () => {
  it('prefers the tool name the row carries, then a skill, model or plugin name a block carries, over the span name', () => {
    const toolRow = { meta: { tool: 'read_file' } } as unknown as TrajectoryEntry
    expect(shortName(descriptor('x', [block({ id: 'content' })]), toolRow)).toBe('read_file')
    expect(shortName(descriptor('x', [block({ id: 'skill', renderer: 'key_values', preview: [{ key: 'skill.name', value: 'pdf' }] })]))).toBe('pdf')
    expect(shortName(descriptor('x', [block({ id: 'model', renderer: 'key_values', preview: [{ key: 'model', value: 'gpt-x' }] })]))).toBe('gpt-x')
    expect(shortName(descriptor('x', [block({ id: 'content' })]))).toBe('tool.call')
  })
})

describe('the pin', () => {
  const blocks = (...ids: string[]): TrajectoryBlockDescriptor[] => ids.map((id) => block({ id }))
  const pinButton = (): HTMLButtonElement | null => q<HTMLButtonElement>('.trajectory-pin')
  const click = (sel: string): void => { act(() => { fireEvent.click(q(sel) as HTMLElement) }) }
  /* Where the pin sits against the close button among the head's children. */
  const offset = (): number => {
    const head = [...q('.trajectory-head')!.children]
    return head.indexOf(q('.trajectory-close')!) - head.indexOf(pinButton()!)
  }

  it('sits just left of the close button once the descriptor is in, wide and narrow', async () => {
    await draw()
    await pick('r0')
    expect(pinButton()).toBeNull()
    await answerDetail(descriptor('r0', blocks('relations', 'usage')))
    expect(offset()).toBe(1)
    act(() => { details.setAreaWidth(700) })
    expect(q('.trajectory-details-over')).not.toBeNull()
    expect(q('.trajectory-close')!.textContent).toBe('gui.trajectory.details.back')
    expect(offset()).toBe(1)
  })

  it('pins the tab on screen, reads unpinned on any other tab, and unpins from the pinned one', async () => {
    await draw()
    await pick('r0')
    await answerDetail(descriptor('r0', blocks('relations', 'usage')))
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('false')
    expect(pinButton()!.getAttribute('aria-label')).toBe('gui.trajectory.details.pin')
    click('#trajectory-tab-relations')
    click('.trajectory-pin')
    expect(details.pinned()).toBe('relations')
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('true')
    expect(pinButton()!.getAttribute('aria-label')).toBe('gui.trajectory.details.unpin')
    click('#trajectory-tab-usage')
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('false')
    expect(details.pinned()).toBe('relations')
    click('#trajectory-tab-relations')
    click('.trajectory-pin')
    expect(details.pinned()).toBeNull()
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('false')
  })

  it('opens another row on the pinned tab, and leaves a row without that tab where the reader left it', async () => {
    await draw()
    await pick('r1')
    await answerDetail(descriptor('r1', blocks('usage', 'origin')))
    click('#trajectory-tab-origin')
    await pick('r0')
    await answerDetail(descriptor('r0', blocks('relations', 'usage')))
    click('#trajectory-tab-relations')
    click('.trajectory-pin')
    await pick('r2')
    await answerDetail(descriptor('r2', blocks('relations', 'usage')))
    expect(q('#trajectory-tab-relations')!.getAttribute('aria-selected')).toBe('true')
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('true')
    await pick('r1')
    expect(q('#trajectory-tab-origin')!.getAttribute('aria-selected')).toBe('true')
    expect(pinButton()!.getAttribute('aria-pressed')).toBe('false')
    expect(q('[data-pinned]')).toBeNull()
  })
})

describe('the way to a Sub-agent Scope from the head', () => {
  const A = 't:runA'
  const ROWS: TrajectoryEntry[] = [
    { ...entry('door', 0, 'subagent.run'), span_name: 'subagent.run', opens_subagent_scope: A, meta: { agent: 'coder', label: 'Fix the parser' } },
    { ...entry('a0', 1), subagent_scope: A },
    { ...entry('back', 2, 'user.input'), turn_start: true, turn_number: 2, origin: 'subagent', dispatched_by_subagent_scope: A },
    { ...entry('backEnd', 3, 'turn.end'), turn_number: 2, origin: 'subagent', dispatched_by_subagent_scope: A },
    entry('plain', 4),
    { ...entry('lonely', 5, 'subagent.run'), span_name: 'subagent.run', opens_subagent_scope: 't:runEmpty', meta: { agent: 'coder' } },
  ]

  beforeEach(() => {
    setSources({ trajectory: { ...source, list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: ROWS, next_cursor: null, index_state: READY, complete: true }) } })
  })

  const jump = (): HTMLButtonElement | null => q<HTMLButtonElement>('.trajectory-head .trajectory-scope-jump')
  /* A click on a row that opens a tab waits out a double click before the pane opens. */
  const pickDoor = async (id: string): Promise<void> => {
    await pick(id)
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 260)) })
  }

  it('offers to open a door\'s trajectory, and lands on the run with the pane still open', async () => {
    await draw()
    await pickDoor('door')
    expect(jump()?.textContent).toBe('gui.trajectory.scope.open')
    expect(jump()?.title).toBe('gui.trajectory.scope.open')
    /* It sits before the pin and the close button, and does not wait for the descriptor. */
    expect([...q('.trajectory-head')!.children].slice(1).map((c) => c.className)).toEqual([
      'trajectory-head-name', 'trajectory-scope-jump', 'trajectory-close',
    ])
    act(() => { fireEvent.click(jump()!) })
    await flush()
    /* Every agent is drawn at once, so the jump selects the run's own row and
       the pane follows it there: nothing is filtered, nothing is closed. */
    expect([list.get().scope, list.get().selectedId]).toEqual([null, 'a0'])
    expect(pane()).not.toBeNull()
  })

  it('lands again on a second jump from the same door', async () => {
    await draw()
    await pickDoor('door')
    act(() => { fireEvent.click(jump()!) })
    await flush()
    await pickDoor('door')
    act(() => { fireEvent.click(jump()!) })
    await flush()
    expect(list.get().selectedId).toBe('a0')
    expect(pane()).not.toBeNull()
  })

  it('names the sub-agent a report turn came from, and lands on its run', async () => {
    await draw()
    await pickDoor('back')
    expect(jump()?.textContent).toBe('gui.trajectory.scope.from {"title":"coder · Fix the parser"}')
    act(() => { fireEvent.click(jump()!) })
    await flush()
    expect([list.get().scope, list.get().selectedId]).toEqual([null, 'a0'])
    expect(pane()).not.toBeNull()
  })

  it('offers no jump into a run that has nothing on record yet', async () => {
    await draw()
    await pickDoor('lonely')
    expect(pane()).not.toBeNull()
    expect(jump()).toBeNull()
  })

  it('offers nothing on a row that leads nowhere', async () => {
    await draw()
    for (const id of ['plain', 'backEnd']) {
      await pick(id)
      expect(pane()).not.toBeNull()
      expect(jump()).toBeNull()
    }
  })
})
