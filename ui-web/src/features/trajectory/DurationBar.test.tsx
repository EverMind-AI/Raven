// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { dispatch as escape } from '../../state/escapeOrder'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import { BUCKET_MAX_H, DurationBar, WHEEL_LINE_PX, paint } from './DurationBar'
import {
  BLOCK_H, BLOCK_TOP, DBL_MS, DOT_ABOVE_Y, DOT_BELOW_Y, DURATION_DETENTS, GAP, MIN_W, bandsFor, barEntries, capacity, hitTest,
  hitTestExact, initialViewport, layoutFor, toSegments,
} from './geometry'
import * as store from './store'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectorySource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The canvas is narrower than the bar it sits in: the bar also holds the
   sum and the tools. Every layout here is the canvas's. */
const WIDTH = 600
const BAR_W = 760
let canvasWidth = WIDTH

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const entry = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't', span_id: id,
  parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: `2026-01-01T00:00:${String(at % 60).padStart(2, '0')}Z`, preview: `p${id}`,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: `2026-01-01T00:00:${String(at % 60).padStart(2, '0')}Z`,
  operation_end: `2026-01-01T00:01:${String(at % 60).padStart(2, '0')}Z`, duration_ms: 1000, charged_ms: 1000, timing_basis: 'span_full',
  duration_owner: id, meta: {},
  ...over,
})

/* Three entries the fit lays out at 600px: a zero-width input, a thinking
   mark, and a one-second output that takes the rest. */
const THREE: TrajectoryEntry[] = [
  entry('in', 0, { kind: 'llm.input', charged_ms: 0, timing_basis: 'zero', operation_start: null, operation_end: null }),
  entry('think', 1, { kind: 'llm.thinking', charged_ms: 0, timing_basis: 'not_recorded', operation_start: null, operation_end: null }),
  entry('out', 2, { kind: 'llm.output', charged_ms: 1000, timing_basis: 'span_full', failure_entry: true }),
]

let rows: TrajectoryEntry[] = THREE

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async () => ({ epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, index_state: READY, complete: true }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: () => new Promise(() => {}),
  block: () => new Promise(() => {}),
}

const q = (sel: string): HTMLElement | null => document.querySelector<HTMLElement>(sel)
const canvas = (): HTMLCanvasElement => q('.trajectory-canvas') as HTMLCanvasElement

/* happy-dom lays nothing out, captures no pointers and observes no sizes:
   the canvas and the bar are given widths of their own, the capture calls
   somewhere to land, and a size observer the test drives by hand. */
const proto = HTMLElement.prototype as unknown as Record<string, unknown>
let widthDescriptor: PropertyDescriptor | undefined
const widths = (): void => {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    get(this: HTMLElement) { return this.tagName === 'CANVAS' ? canvasWidth : BAR_W },
    configurable: true,
  })
}

interface Observed { cb: ResizeObserverCallback; targets: Element[] }
const observers: Observed[] = []
class FakeResizeObserver {
  private readonly rec: Observed
  constructor(cb: ResizeObserverCallback) {
    this.rec = { cb, targets: [] }
    observers.push(this.rec)
  }
  observe(el: Element): void { this.rec.targets.push(el) }
  unobserve(el: Element): void { this.rec.targets = this.rec.targets.filter((x) => x !== el) }
  disconnect(): void { this.rec.targets = [] }
}
const resizeCanvasTo = (w: number): void => {
  canvasWidth = w
  act(() => {
    for (const o of observers) if (o.targets.length) o.cb([], o as unknown as ResizeObserver)
  })
}

beforeAll(() => {
  widthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  widths()
  proto.setPointerCapture = () => {}
  proto.releasePointerCapture = () => {}
  proto.hasPointerCapture = () => false
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
})

afterAll(() => {
  if (widthDescriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', widthDescriptor)
  else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
  delete proto.setPointerCapture
  delete proto.releasePointerCapture
  delete proto.hasPointerCapture
  vi.unstubAllGlobals()
})

async function ready(): Promise<void> {
  absorb(['trajectory-v1'])
  store.install()
  /* These fixtures lean on zero-length marks; the duration threshold would take them out. */
  store.setPrefs({ minChargedMs: 0 })
  details.install()
  unpitch()
  store.sessionChanged('gui:a')
  await store.refreshState()
  store.setView('trajectory')
  await store.load()
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve() }) }
const later = async (ms: number): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

/* A content x in a gap of the fitted layout: nobody's, so the bar speaks for itself there. */
const gapAfter = (n: number, width = WIDTH): number => {
  const layout = layoutFor(toSegments(store.get().visible), initialViewport(width))
  const b = layout.blocks[n]!
  return b.x + b.w + 0.5
}
const brief = (): string => q('.trajectory-hover-brief')?.textContent ?? ''

const pointer = (type: string, clientX: number, pointerId = 1): void => {
  act(() => { fireEvent(canvas(), new PointerEvent(type, { clientX, clientY: 10, pointerId, button: 0, bubbles: true })) })
}
/* happy-dom's WheelEvent carries no pointer position; the browser's does. */
const wheel = (deltaY: number, clientX: number): WheelEvent => {
  const ev = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true })
  Object.defineProperty(ev, 'clientX', { value: clientX })
  return ev
}
const click = async (clientX: number): Promise<void> => {
  pointer('pointerdown', clientX)
  pointer('pointerup', clientX)
  await flush()
}

/* How many times the selection moved, counted off the store itself. */
function countSelections(): () => number {
  let n = 0
  let last = store.get().selectedId
  store.subscribe(() => {
    const now = store.get().selectedId
    if (now !== last) { n += 1; last = now }
  })
  return () => n
}

beforeEach(() => {
  vi.useFakeTimers()
  rows = THREE
  canvasWidth = WIDTH
  observers.length = 0
  store._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('the duration bar', () => {
  it('names its sum, with the unknowns counted apart, and reads the width it is given', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    expect(canvas().getAttribute('aria-label')).toBe('gui.trajectory.bar.canvas_label {"n":3,"known":"1s"}')
    /* No text beside the canvas any more: the sum is said where no block is. */
    expect(q('.trajectory-bar-sum')).toBeNull()
    pointer('pointermove', gapAfter(0))
    expect(brief()).toBe('gui.trajectory.bar.sum_known {"dur":"1s"} \u00b7 gui.trajectory.bar.sum_unknown {"n":1}')
    expect(q('.trajectory-bar')?.hasAttribute('data-fit')).toBe(true)
  })

  it('says the overlap when a turn and the calls inside it are both charged', async () => {
    rows = [
      entry('a', 0, { charged_ms: 2000 }),
      entry('reply', 1, { kind: 'turn.end', charged_ms: 4000 }),
    ]
    await ready()
    render(<DurationBar />)
    await flush()
    pointer('pointermove', gapAfter(0))
    expect(brief()).toContain('gui.trajectory.bar.sum_overlap')
    expect(brief()).toContain('"dur":"6s"')
  })

  it('selects the block under a click after the double-click wait, through the bar\'s own door, and opens the details', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const selections = countSelections()
    await click(WIDTH - 10)
    expect(store.get().selectedId).toBeNull()
    await later(DBL_MS)
    expect(store.get().selectedId).toBe('out')
    expect(store.get().selectedBy).toBe('bar')
    expect(details.get().open).toBe(true)
    expect(selections()).toBe(1)
  })

  it('keeps only the latest of two quick clicks', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const selections = countSelections()
    await click(1)
    await later(DBL_MS / 2)
    await click(WIDTH - 10)
    await later(DBL_MS)
    expect(store.get().selectedId).toBe('out')
    expect(selections()).toBe(1)
  })

  it('treats a double click as a reset and never as a click', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    expect(store.get().timeline.fit).toBe(false)
    await click(WIDTH - 10)
    act(() => { fireEvent.dblClick(canvas()) })
    await later(DBL_MS * 2)
    expect(store.get().selectedId).toBeNull()
    expect(store.get().timeline.fit).toBe(true)
    expect(store.get().timeline.scale).toBe(1)
  })

  it('drops a pending click when the world moves on under it', async () => {
    const cases: Array<[string, () => void]> = [
      ['the conversation view', () => store.setView('chat')],
      ['another conversation', () => store.sessionChanged('gui:b')],
      ['the switch going off', () => store.disabledByServer()],
      ['the target withdrawn', () => store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [], removed: [{ entry_id: 'out', revision: 101, replaced_by: null }],
        has_more: false, reset_required: false, index_state: READY,
      })],
    ]
    for (const [, move] of cases) {
      rows = THREE
      store._resetForTests()
      details._resetForTests()
      _resetFreshForTests()
      cleanup()
      document.body.innerHTML = '<div class="chat"></div>'
      await ready()
      render(<DurationBar />)
      await flush()
      await click(WIDTH - 10)
      act(() => { move() })
      await later(DBL_MS * 2)
      expect(store.get().selectedId).toBeNull()
      expect(details.get().open).toBe(false)
    }
  })

  it('pans on a drag past the threshold once zoomed, and a drag is never a click', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const zoomed = store.get().timeline
    expect(zoomed.fit).toBe(false)
    /* Three pixels: a click, not a drag. */
    pointer('pointerdown', 300)
    pointer('pointermove', 303)
    pointer('pointerup', 303)
    await flush()
    expect(store.get().timeline.offset).toBe(zoomed.offset)
    act(() => { fireEvent.dblClick(canvas()) })
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const before = store.get().timeline.offset
    /* Dragging left shows what was to the right: the offset grows. */
    pointer('pointerdown', 300)
    pointer('pointermove', 280)
    pointer('pointermove', 260)
    expect(q('.trajectory-bar')?.hasAttribute('data-drag')).toBe(true)
    pointer('pointerup', 260)
    await flush()
    expect(store.get().timeline.offset).toBeGreaterThan(before)
    expect(q('.trajectory-bar')?.hasAttribute('data-drag')).toBe(false)
    await later(DBL_MS * 2)
    expect(store.get().selectedId).toBeNull()
  })

  it('forgets a press released off the canvas before it moved, so a later hover does not pan', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const before = store.get().timeline.offset
    pointer('pointerdown', 300)
    pointer('pointermove', 302)
    act(() => { fireEvent.pointerLeave(canvas(), { pointerId: 1 }) })
    /* Back over the canvas with no button held. */
    pointer('pointermove', 250)
    pointer('pointermove', 200)
    expect(store.get().timeline.offset).toBe(before)
    expect(q('.trajectory-bar')?.hasAttribute('data-drag')).toBe(false)
  })

  it('does not click after a cancelled or lost pointer', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    pointer('pointerdown', WIDTH - 10)
    pointer('pointercancel', WIDTH - 10)
    pointer('pointerup', WIDTH - 10)
    await later(DBL_MS * 2)
    expect(store.get().selectedId).toBeNull()
    pointer('pointerdown', WIDTH - 10)
    act(() => { fireEvent(canvas(), new PointerEvent('lostpointercapture', { pointerId: 1, bubbles: true })) })
    pointer('pointerup', WIDTH - 10)
    await later(DBL_MS * 2)
    expect(store.get().selectedId).toBeNull()
  })

  it('zooms about the pointer on the wheel over the canvas alone, and prevents the page from scrolling there', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const ev = wheel(-200, 300)
    act(() => { canvas().dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(Number.isFinite(store.get().timeline.offset)).toBe(true)
    expect(store.get().timeline.fit).toBe(false)
    expect(store.get().timeline.scale).toBeGreaterThan(1)
    const elsewhere = new WheelEvent('wheel', { deltaY: -200, bubbles: true, cancelable: true })
    act(() => { (q('.trajectory-bar-tools') as HTMLElement).dispatchEvent(elsewhere) })
    expect(elsewhere.defaultPrevented).toBe(false)
  })

  it('zooms in, out and back to the fit from its three tools', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const tool = (name: string): HTMLElement => q(`.trajectory-bar-tool[aria-label="gui.trajectory.bar.${name}"]`) as HTMLElement
    expect((tool('zoom_out') as HTMLButtonElement).disabled).toBe(true)
    act(() => { fireEvent.click(tool('zoom_in')) })
    expect(store.get().timeline.scale).toBe(2)
    act(() => { fireEvent.click(tool('zoom_in')) })
    expect(store.get().timeline.scale).toBe(4)
    act(() => { fireEvent.click(tool('zoom_out')) })
    expect(store.get().timeline.scale).toBe(2)
    act(() => { fireEvent.click(tool('reset')) })
    expect(store.get().timeline.fit).toBe(true)
  })

  it('says three lines about the block under the pointer, one for each kind of entry', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    /* The layout at 600px: the two marks take three pixels each (with a gap
       after each), the output the rest. */
    pointer('pointermove', 1)
    const lines = (): string[] => [...document.querySelectorAll('.trajectory-hover-line')].map((l) => l.textContent ?? '')
    expect(lines()[0]).toContain('gui.trajectory.kind.llm_input')
    expect(lines()[1]).toBe('gui.trajectory.bar.event_at {"t":"2026-01-01T00:00:00Z"}')
    expect(lines()[2]).toBe('gui.trajectory.basis.zero')
    pointer('pointermove', MIN_W + 2)
    expect(lines()[0]).toContain('gui.trajectory.kind.llm_thinking')
    expect(lines()[1]).toBe('gui.trajectory.bar.thinking_range')
    expect(lines()[2]).toBe('gui.trajectory.basis.not_recorded')
    pointer('pointermove', WIDTH - 10)
    expect(lines()[0]).toContain('gui.trajectory.kind.llm_output')
    expect(lines()[0]).toContain('gui.trajectory.bar.failed')
    expect(lines()[1]).toBe('gui.trajectory.bar.span {"from":"2026-01-01T00:00:02Z","to":"2026-01-01T00:01:02Z"}')
    expect(lines()[2]).toBe('gui.trajectory.basis.span_full (1s)')
    act(() => { fireEvent.pointerLeave(canvas()) })
    expect(q('.trajectory-hover')).toBeNull()
  })

  it('refits to new rows while fitted, and holds its zoom and anchor when zoomed', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const more = { ...store.get(), ...store.rowsOf([...THREE, entry('late', 9, { charged_ms: 3000 })]) }
    act(() => { store.set(more) })
    expect(store.get().timeline.fit).toBe(true)
    expect(canvas().getAttribute('aria-label')).toContain('"n":4')
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const zoomed = store.get().timeline
    act(() => { store.set({ ...store.get(), ...store.rowsOf([...more.entries, entry('later', 10, { charged_ms: 500 })]) }) })
    expect(store.get().timeline.scale).toBe(zoomed.scale)
    expect(store.get().timeline.fit).toBe(false)
    expect(store.get().timeline.offset).toBe(zoomed.offset)
  })

  it('lays out, hit-tests and zooms in the canvas\'s own pixels, not the wider bar\'s, and follows the canvas as it resizes', async () => {
    rows = [entry('a', 0, { kind: 'tool.output', charged_ms: 1000 }), entry('b', 1, { kind: 'llm.output', charged_ms: 1000 })]
    await ready()
    render(<DurationBar />)
    await flush()
    expect(observers.some((o) => o.targets.includes(canvas()))).toBe(true)
    expect(observers.some((o) => o.targets.includes(q('.trajectory-bar') as Element))).toBe(false)
    const kindUnder = (x: number): string => {
      pointer('pointermove', x)
      return q('.trajectory-hover .trajectory-tag')?.textContent ?? ''
    }
    /* Two equal durations: the seam is at the canvas's middle, 300, not the bar's, 380. */
    expect(kindUnder(340)).toBe('gui.trajectory.kind.llm_output')
    expect(kindUnder(290)).toBe('gui.trajectory.kind.tool_output')
    /* The canvas grows -- the sum beside it got shorter -- and the seam moves with it. */
    resizeCanvasTo(800)
    expect(kindUnder(340)).toBe('gui.trajectory.kind.tool_output')
    expect(kindUnder(420)).toBe('gui.trajectory.kind.llm_output')
    /* A zoom about the canvas's right edge keeps that edge's entry under the pointer. */
    act(() => { canvas().dispatchEvent(wheel(-400, 790)) })
    const view = { ...store.get().timeline, width: 800 }
    const layout = layoutFor(toSegments(store.get().entries), view)
    expect(hitTest(layout, view.offset + 790)?.id).toBe('b')
    expect(view.offset + 800).toBeLessThanOrEqual(layout.contentWidth + 1e-6)
  })

  it('keeps a dragged view where the reader left it when rows append, rows arrive in front, or the canvas resizes', async () => {
    rows = Array.from({ length: 12 }, (_, k) => entry(`r${k}`, k, { charged_ms: 1000 * (1 + (k % 4)) }))
    await ready()
    render(<DurationBar />)
    await flush()
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const zoomed = store.get().timeline
    pointer('pointerdown', 300)
    pointer('pointermove', 280)
    pointer('pointermove', 200)
    pointer('pointerup', 200)
    await flush()
    const dragged = store.get().timeline
    expect(dragged.offset).toBeGreaterThan(zoomed.offset)
    expect(dragged.anchor).not.toEqual(zoomed.anchor)
    const atLeftEdge = (): string | undefined => {
      const s = store.get()
      return hitTest(layoutFor(toSegments(s.entries), { ...s.timeline, width: canvasWidth }), s.timeline.offset + 0.5)?.id
    }
    const edge = atLeftEdge()
    expect(edge).toBe(dragged.anchor!.id)
    /* Rows appended at the tail: nothing before them moves, so neither does the view. */
    const s1 = store.get()
    act(() => { store.set({ ...s1, ...store.rowsOf([...s1.entries, entry('tail', 20, { charged_ms: 3000 })], s1) }) })
    expect(store.get().timeline.offset).toBeCloseTo(dragged.offset, 6)
    expect(atLeftEdge()).toBe(edge)
    /* A row in front: the view slides by its slot, so the same entry stays at the edge. */
    const s2 = store.get()
    const early = entry('early', 0, { charged_ms: 2000 })
    act(() => { store.set({ ...s2, ...store.rowsOf([early, ...s2.entries], s2) }) })
    const slot = Math.max(MIN_W, dragged.frozenUnit! * dragged.scale * 2000) + GAP
    expect(store.get().timeline.offset).toBeCloseTo(dragged.offset + slot, 6)
    expect(atLeftEdge()).toBe(edge)
    /* The canvas narrows: the edge holds. */
    resizeCanvasTo(400)
    expect(atLeftEdge()).toBe(edge)
    expect(store.get().timeline.scale).toBe(dragged.scale)
  })

  it('does nothing without a width', async () => {
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { get() { return 0 }, configurable: true })
    try {
      await ready()
      render(<DurationBar />)
      await flush()
      pointer('pointermove', 10)
      expect(q('.trajectory-hover')).toBeNull()
      await click(10)
      await later(DBL_MS * 2)
      expect(store.get().selectedId).toBeNull()
    } finally {
      widths()
    }
  })
})

describe('a dense block', () => {
  const many = Array.from({ length: capacity(WIDTH) + 50 }, (_, k) => entry(`d${k}`, k, { charged_ms: k % 3 === 0 ? 0 : 500, timing_basis: k % 3 === 0 ? 'zero' : 'span_full', turn_number: Math.floor(k / 40) + 1 }))

  it('opens a pick list on a click, picks by keyboard, and spreads out on request', async () => {
    rows = many
    await ready()
    render(<DurationBar />)
    await flush()
    pointer('pointermove', 1)
    expect(q('.trajectory-hover-line')?.textContent).toContain('gui.trajectory.bar.dense ')
    /* Recorded zeros are known: this block has none unknown. */
    expect(q('.trajectory-hover-line')?.textContent).toContain('"unknown":0')
    await click(1)
    await later(DBL_MS)
    const bucket = store.get().timeline.bucket
    expect(bucket).not.toBeNull()
    expect(bucket!.ids[0]).toBe('d0')
    const list = q('.trajectory-bucket') as HTMLElement
    expect(list.getAttribute('role')).toBe('listbox')
    const options = [...list.querySelectorAll<HTMLElement>('[role="option"]')]
    expect(options).toHaveLength(bucket!.ids.length)
    expect(document.activeElement).toBe(options[0])
    act(() => { fireEvent.keyDown(list, { key: 'ArrowDown' }) })
    expect(document.activeElement).toBe(options[1])
    act(() => { fireEvent.click(options[1]!) })
    expect(store.get().selectedId).toBe('d1')
    expect(store.get().selectedBy).toBe('bar')
    expect(details.get().open).toBe(true)
    expect(store.get().timeline.bucket).toBeNull()
    expect(document.activeElement).toBe(q('.trajectory-bar'))
    /* Open again and spread the block out instead. */
    await click(1)
    await later(DBL_MS)
    act(() => { fireEvent.click(q('.trajectory-bucket-head .trajectory-link') as HTMLElement) })
    expect(store.get().timeline.bucket).toBeNull()
    expect(store.get().timeline.fit).toBe(false)
    expect(store.get().timeline.scale).toBeGreaterThanOrEqual(1)
  })

  it('says how many of a block\'s entries have no recorded duration', async () => {
    rows = many.map((e, k) => (k % 3 === 1 ? { ...e, charged_ms: 0, timing_basis: 'not_recorded' as const } : e))
    await ready()
    render(<DurationBar />)
    await flush()
    pointer('pointermove', 1)
    const text = q('.trajectory-hover-line')?.textContent ?? ''
    const n = Number(/"n":(\d+)/.exec(text)![1])
    const unknown = Number(/"unknown":(\d+)/.exec(text)![1])
    /* The first block holds the first n rows; every third of them, from the second, is unrecorded. */
    expect(unknown).toBe([...Array(n).keys()].filter((k) => k % 3 === 1).length)
    expect(unknown).toBeGreaterThan(0)
    expect(unknown).toBeLessThan(n)
  })

  it('opens its pick list below the bar when the window has room there, above it otherwise, and never taller than the room', async () => {
    rows = many
    await ready()
    render(<DurationBar />)
    await flush()
    /* The bar's place and the window's height, then a re-render for the list to read them. */
    const place = (top: number, viewportHeight: number): CSSStyleDeclaration => {
      canvas().getBoundingClientRect = () => ({ left: 0, right: WIDTH, top, bottom: top + 32, width: WIDTH, height: 32, x: 0, y: top, toJSON: () => ({}) })
      Object.defineProperty(document.documentElement, 'clientHeight', { get: () => viewportHeight, configurable: true })
      act(() => { store.setTimeline({ bucket: { ...store.get().timeline.bucket! } }) })
      return (q('.trajectory-bucket') as HTMLElement).style
    }
    try {
      /* The bar at the bottom of a tall window: the list opens above it, at its full height. */
      await click(1)
      await later(DBL_MS)
      expect(store.get().timeline.bucket).not.toBeNull()
      let style = place(760, 800)
      expect(style.top).toBe(`${760 - 4 - BUCKET_MAX_H}px`)
      expect(style.maxHeight).toBe(`${BUCKET_MAX_H}px`)
      /* The bar near the top of a short window: below, but no taller than what is left. */
      style = place(100, 300)
      expect(style.top).toBe('136px')
      expect(style.maxHeight).toBe(`${300 - 4 - 136}px`)
      /* Room on neither side for the whole list: the larger side, clipped to it. */
      style = place(100, 200)
      expect(style.maxHeight).toBe('92px')
      expect(style.top).toBe(`${96 - 92}px`)
    } finally {
      delete (document.documentElement as unknown as Record<string, unknown>).clientHeight
    }
  })

  it('closes on Escape through the page\'s order, and when the conversation changes', async () => {
    rows = many
    await ready()
    render(<DurationBar />)
    await flush()
    await click(1)
    await later(DBL_MS)
    expect(store.get().timeline.bucket).not.toBeNull()
    act(() => { expect(escape()).toBe(true) })
    expect(store.get().timeline.bucket).toBeNull()
    expect(details.get().open).toBe(false)
    await click(1)
    await later(DBL_MS)
    expect(store.get().timeline.bucket).not.toBeNull()
    act(() => { store.sessionChanged('gui:b') })
    expect(store.get().timeline.bucket).toBeNull()
    expect(q('.trajectory-bucket')).toBeNull()
  })
})

describe('the switches, the bands and the dots', () => {
  const recorder = (): { ctx: CanvasRenderingContext2D; calls: Array<[string, unknown[]]> } => {
    const calls: Array<[string, unknown[]]> = []
    const ctx = new Proxy({} as CanvasRenderingContext2D, {
      get: (_t, name) => {
        if (name === 'fillStyle' || name === 'strokeStyle' || name === 'lineWidth') return undefined
        return (...args: unknown[]) => { calls.push([String(name), args]) }
      },
      set: (_t, name, value) => { calls.push([`set ${String(name)}`, [value]]); return true },
    })
    return { ctx, calls }
  }

  it('hides the recorded durations under the threshold set on its slider, never an unknown one or the user\'s input', async () => {
    rows = [
      entry('u', 0, { kind: 'user.input', charged_ms: 0, timing_basis: 'zero' }),
      entry('fast', 1, { kind: 'tool.output', charged_ms: 12 }),
      entry('unknown', 2, { kind: 'llm.thinking', charged_ms: 0, timing_basis: 'not_recorded' }),
      entry('mid', 3, { kind: 'llm.output', charged_ms: 400 }),
      entry('slow', 4, { kind: 'tool.output', charged_ms: 1500 }),
    ]
    await ready()
    store.setPrefs({ minChargedMs: 20 })
    render(<DurationBar />)
    await flush()
    expect(canvas().getAttribute('aria-label')).toContain('"n":4')
    const button = q('.trajectory-threshold-btn') as HTMLButtonElement
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(button.getAttribute('aria-label')).toBe('gui.trajectory.bar.threshold {"ms":"20 ms"}')
    /* The button opens the popover; the slider walks the detents. */
    act(() => { fireEvent.click(button) })
    expect(store.get().timeline.threshold).toBe(true)
    expect(button.getAttribute('aria-expanded')).toBe('true')
    const range = q('.trajectory-threshold-range') as HTMLInputElement
    expect(range.value).toBe(String(DURATION_DETENTS.indexOf(20)))
    act(() => { fireEvent.change(range, { target: { value: String(DURATION_DETENTS.indexOf(1000)) } }) })
    expect(store.get().prefs.minChargedMs).toBe(1000)
    expect(canvas().getAttribute('aria-label')).toContain('"n":3')
    expect(q('.trajectory-threshold-text')?.textContent).toBe('gui.trajectory.bar.threshold {"ms":"1 s"}')
    act(() => { fireEvent.change(range, { target: { value: '0' } }) })
    expect(store.get().prefs.minChargedMs).toBe(0)
    expect(canvas().getAttribute('aria-label')).toContain('"n":5')
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(q('.trajectory-threshold-text')?.textContent).toBe('gui.trajectory.bar.threshold_off')
    expect(JSON.parse(localStorage.getItem(store.PREFS_KEY) as string)).toEqual({ minChargedMs: 0, showHidden: false })
    /* A press elsewhere closes it; so does Escape through the page's order. */
    act(() => { document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })) })
    expect(q('.trajectory-threshold')).toBeNull()
    act(() => { fireEvent.click(button) })
    act(() => { fireEvent.pointerDown(q('.trajectory-threshold-range') as HTMLElement) })
    expect(q('.trajectory-threshold')).not.toBeNull()
    act(() => { expect(escape()).toBe(true) })
    expect(q('.trajectory-threshold')).toBeNull()
    store.setPrefs({ minChargedMs: 20 })
  })

  it('keeps its place on the entry after the one a new threshold took away, when zoomed', async () => {
    rows = [
      entry('a', 0, { charged_ms: 3000 }),
      entry('b', 1, { charged_ms: 30 }),
      entry('c', 2, { charged_ms: 2000 }),
      entry('d', 3, { charged_ms: 2000 }),
    ]
    await ready()
    render(<DurationBar />)
    await flush()
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    /* Put the view's left edge on b, then raise the threshold over it. */
    const v = { ...store.get().timeline, width: WIDTH }
    const before = layoutFor(toSegments(store.get().visible), v)
    const b = before.blocks.find((x) => x.id === 'b')!
    act(() => { store.setTimeline({ offset: b.x, anchor: { id: 'b', frac: 0 } }) })
    act(() => { store.setPrefs({ minChargedMs: 50 }) })
    await flush()
    const after = layoutFor(toSegments(barEntries(store.get().visible, 50)), { ...store.get().timeline, width: WIDTH })
    const c = after.blocks.find((x) => x.id === 'c')!
    expect(after.blocks.some((x) => x.id === 'b')).toBe(false)
    expect(store.get().timeline.offset).toBeCloseTo(Math.min(c.x, Math.max(0, after.contentWidth - WIDTH)), 6)
    store.setPrefs({ minChargedMs: 0 })
  })

  it('pans on a sideways wheel or a Shift wheel once zoomed, zooms on the vertical one, and stays put in the fit', async () => {
    await ready()
    render(<DurationBar />)
    await flush()
    const sideways = (deltaX: number, deltaY = 0, over: { deltaMode?: number; shiftKey?: boolean; ctrlKey?: boolean } = {}): WheelEvent => {
      const ev = new WheelEvent('wheel', { deltaX, deltaY, bubbles: true, cancelable: true })
      /* Set on the event itself: the test DOM does not carry every init field through. */
      for (const [key, value] of Object.entries({ clientX: 300, deltaMode: 0, shiftKey: false, ctrlKey: false, ...over })) {
        Object.defineProperty(ev, key, { value })
      }
      return ev
    }
    /* In the fit there is nothing to the side: the swipe changes nothing, but the page does not take it either. */
    const fitted = sideways(120, 4)
    act(() => { canvas().dispatchEvent(fitted) })
    expect(fitted.defaultPrevented).toBe(true)
    expect(store.get().timeline.fit).toBe(true)
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    act(() => { store.setTimeline({ offset: 200 }) })
    const scale = store.get().timeline.scale
    /* Sideways leads: a pan by the pixels, never a zoom. */
    act(() => { canvas().dispatchEvent(sideways(50, 3)) })
    expect(store.get().timeline.offset).toBeCloseTo(250, 6)
    expect(store.get().timeline.scale).toBe(scale)
    expect(store.get().timeline.anchor).not.toBeNull()
    /* Lines are sixteen pixels; Shift turns the vertical wheel sideways. */
    act(() => { canvas().dispatchEvent(sideways(-2, 0, { deltaMode: 1 })) })
    expect(store.get().timeline.offset).toBeCloseTo(250 - 2 * WHEEL_LINE_PX, 6)
    act(() => { canvas().dispatchEvent(sideways(0, 40, { shiftKey: true })) })
    expect(store.get().timeline.offset).toBeCloseTo(250 - 2 * WHEEL_LINE_PX + 40, 6)
    expect(store.get().timeline.scale).toBe(scale)
    /* A vertical wheel, and a trackpad's pinch (Ctrl with a vertical delta), zoom. */
    act(() => { canvas().dispatchEvent(wheel(-200, 300)) })
    expect(store.get().timeline.scale).toBeGreaterThan(scale)
    const zoomed = store.get().timeline.scale
    act(() => { canvas().dispatchEvent(sideways(0, 150, { ctrlKey: true })) })
    expect(store.get().timeline.scale).toBeLessThan(zoomed)
  })

  it('draws a hidden reply\'s time as a band behind its turn, says the turn\'s total there and lands a click on the turn\'s first row', async () => {
    rows = [
      entry('ask', 0, { kind: 'user.input', charged_ms: 0, timing_basis: 'zero', turn_number: 1, turn_start: true }),
      entry('out', 1, { kind: 'llm.output', charged_ms: 2000, turn_number: 1 }),
      entry('reply', 2, { kind: 'turn.end', slot: 'turn.output', charged_ms: 5000, turn_number: 1, meta: { hidden: 'redundant_reply' } }),
      entry('ask2', 3, { kind: 'user.input', charged_ms: 0, timing_basis: 'zero', turn_number: 2, turn_start: true }),
      entry('out2', 4, { kind: 'llm.output', charged_ms: 2000, turn_number: 2 }),
      entry('reply2', 5, { kind: 'turn.end', slot: 'turn.output', charged_ms: 3000, turn_number: 2 }),
    ]
    await ready()
    render(<DurationBar />)
    await flush()
    /* The hidden reply is not a block: three blocks for turn 1, three for turn 2 (its reply shows). */
    expect(canvas().getAttribute('aria-label')).toContain('"n":5')
    /* The switch that shows hidden entries says how many there are. */
    const showHidden = q('.trajectory-bar-switch[aria-label^="gui.trajectory.bar.show_hidden"]') as HTMLButtonElement
    expect(showHidden.getAttribute('aria-label')).toBe('gui.trajectory.bar.show_hidden {"n":1}')
    expect(showHidden.getAttribute('aria-pressed')).toBe('false')
    const { ctx, calls } = recorder()
    const view = { ...store.get().timeline, width: WIDTH }
    const segments = toSegments(store.get().visible)
    const layout = layoutFor(segments, view)
    const bySegment = new Map(segments.map((s) => [s.id, s]))
    const bands = bandsFor(layout, bySegment, new Map([[1, 5000]]))
    expect(bands).toHaveLength(1)
    const [ask, out] = layout.blocks
    expect(bands[0]).toEqual({ turn: 1, x0: ask!.x, x1: out!.x + out!.w, total: 5000 })
    paint(ctx, layout, view, 'out', bySegment, 1, bands)
    const fills = calls.filter(([name]) => name === 'fillRect').map(([, args]) => args as number[])
    /* The band is drawn first, under the blocks, and the blocks are half the row's height. */
    expect(fills[0]![0]).toBe(bands[0]!.x0)
    expect(fills[0]![2]).toBeCloseTo(bands[0]!.x1 - bands[0]!.x0, 6)
    expect(fills.slice(1).every((f) => f[1] === BLOCK_TOP && f[3] === BLOCK_H)).toBe(true)
    /* One green dot over the selected block, drawn above the blocks, none below since nothing failed. */
    const arcs = calls.filter(([name]) => name === 'arc').map(([, args]) => args as number[])
    expect(arcs).toHaveLength(1)
    expect(arcs[0]![0]).toBeCloseTo(out!.x + out!.w / 2, 6)
    expect(arcs[0]![1]).toBe(DOT_ABOVE_Y)
    /* The gap between turn 1's blocks lies in the band: the hover names the turn, the click selects its first row. */
    const inBand = gapAfter(0)
    pointer('pointermove', inBand)
    const lines = [...document.querySelectorAll('.trajectory-hover-brief .trajectory-hover-line')].map((l) => l.textContent)
    expect(lines[0]).toBe('gui.trajectory.bar.turn_total {"n":1,"dur":"5s"}')
    /* The sum rides along under the turn's line, and the tools say it as their title, so it is never only in a gap. */
    expect(lines[1]).toContain('gui.trajectory.bar.sum_known')
    expect(q('.trajectory-bar-tools')?.getAttribute('title')).toBe(lines[1])
    await click(inBand)
    expect(store.get().selectedId).toBe('ask')
    expect(store.get().selectedBy).toBe('bar')
    /* A gap outside any band says the sum instead. */
    pointer('pointermove', gapAfter(3))
    expect(brief()).toContain('gui.trajectory.bar.sum_known')
    /* Showing the internal steps brings the reply back as a block and takes the band away. */
    act(() => { store.setPrefs({ showHidden: true }) })
    await flush()
    expect(canvas().getAttribute('aria-label')).toContain('"n":6')
    pointer('pointermove', gapAfter(0))
    expect(brief()).toContain('gui.trajectory.bar.sum_known')
  })

  it('marks a failed block with a dot below it', async () => {
    rows = [entry('ok', 0, { charged_ms: 1000 }), entry('bad', 1, { charged_ms: 1000, failure_entry: true })]
    await ready()
    const { ctx, calls } = recorder()
    const view = { ...store.get().timeline, width: WIDTH }
    const segments = toSegments(store.get().visible)
    const layout = layoutFor(segments, view)
    paint(ctx, layout, view, null, new Map(segments.map((s) => [s.id, s])), 1)
    const arcs = calls.filter(([name]) => name === 'arc').map(([, args]) => args as number[])
    expect(arcs).toHaveLength(1)
    expect(arcs[0]![1]).toBe(DOT_BELOW_Y)
    expect(arcs[0]![0]).toBeCloseTo(layout.blocks[1]!.x + layout.blocks[1]!.w / 2, 6)
  })

  it('hits only a block\'s own slot: a gap is nobody\'s', () => {
    const layout = layoutFor(toSegments([entry('a', 0, { charged_ms: 1000 }), entry('b', 1, { charged_ms: 1000 })]), initialViewport(600))
    const [a, b] = layout.blocks
    expect(hitTestExact(layout, a!.x + 1)?.id).toBe('a')
    expect(hitTestExact(layout, a!.x + a!.w + 0.5)).toBeNull()
    expect(hitTest(layout, a!.x + a!.w + 0.5)?.id).toBe('a')
    expect(hitTestExact(layout, b!.x + b!.w - 0.01)?.id).toBe('b')
    expect(hitTestExact(layout, b!.x + b!.w + 1)).toBeNull()
  })
})
