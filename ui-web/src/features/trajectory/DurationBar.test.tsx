// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { dispatch as escape } from '../../state/escapeOrder'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import { DurationBar } from './DurationBar'
import { DBL_MS, MIN_W, capacity } from './geometry'
import * as store from './store'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectorySource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const WIDTH = 600

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
  entry('think', 1, { kind: 'llm.thinking', charged_ms: null, timing_basis: 'not_recorded', operation_start: null, operation_end: null }),
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

/* happy-dom lays nothing out and captures no pointers: the bar is given a
   width, and the capture calls somewhere to land. */
const proto = HTMLElement.prototype as unknown as Record<string, unknown>
let widthDescriptor: PropertyDescriptor | undefined

beforeAll(() => {
  widthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { get() { return WIDTH }, configurable: true })
  proto.setPointerCapture = () => {}
  proto.releasePointerCapture = () => {}
  proto.hasPointerCapture = () => false
})

afterAll(() => {
  if (widthDescriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', widthDescriptor)
  else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
  delete proto.setPointerCapture
  delete proto.releasePointerCapture
  delete proto.hasPointerCapture
})

async function ready(): Promise<void> {
  absorb(['trajectory-v1'])
  store.install()
  details.install()
  unpitch()
  store.sessionChanged('gui:a')
  await store.refreshState()
  store.setView('trajectory')
  await store.load()
}

const flush = async (): Promise<void> => { await act(async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve() }) }
const later = async (ms: number): Promise<void> => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

const pointer = (type: string, clientX: number, pointerId = 1): void => {
  act(() => { fireEvent(canvas(), new PointerEvent(type, { clientX, clientY: 10, pointerId, button: 0, bubbles: true })) })
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
    expect(q('.trajectory-bar-sum')?.textContent).toBe('gui.trajectory.bar.sum_known {"dur":"1s"}gui.trajectory.bar.sum_unknown {"n":1}')
    expect(q('.trajectory-bar')?.hasAttribute('data-fit')).toBe(true)
  })

  it('says the overlap when a turn and the calls inside it are both charged', async () => {
    rows = [
      entry('a', 0, { charged_ms: 2000 }),
      entry('reply', 1, { kind: 'agent.reply', charged_ms: 4000 }),
    ]
    await ready()
    render(<DurationBar />)
    await flush()
    expect(q('.trajectory-bar-sum')?.textContent).toContain('gui.trajectory.bar.sum_overlap')
    expect(q('.trajectory-bar-sum')?.textContent).toContain('"dur":"6s"')
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
    const ev = new WheelEvent('wheel', { deltaY: -200, clientX: 300, bubbles: true, cancelable: true })
    act(() => { canvas().dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(store.get().timeline.fit).toBe(false)
    expect(store.get().timeline.scale).toBeGreaterThan(1)
    const elsewhere = new WheelEvent('wheel', { deltaY: -200, bubbles: true, cancelable: true })
    act(() => { (q('.trajectory-bar-sum') as HTMLElement).dispatchEvent(elsewhere) })
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
    const more = { ...store.get(), entries: [...THREE, entry('late', 9, { charged_ms: 3000 })], index: { in: 0, think: 1, out: 2, late: 3 } }
    act(() => { store.set(more) })
    expect(store.get().timeline.fit).toBe(true)
    expect(canvas().getAttribute('aria-label')).toContain('"n":4')
    act(() => { fireEvent.click(q('.trajectory-bar-tool[aria-label="gui.trajectory.bar.zoom_in"]') as HTMLElement) })
    const zoomed = store.get().timeline
    act(() => { store.set({ ...store.get(), entries: [...more.entries, entry('later', 10, { charged_ms: 500 })], index: { ...more.index, later: 4 } }) })
    expect(store.get().timeline.scale).toBe(zoomed.scale)
    expect(store.get().timeline.fit).toBe(false)
    expect(store.get().timeline.offset).toBe(zoomed.offset)
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
      Object.defineProperty(HTMLElement.prototype, 'clientWidth', { get() { return WIDTH }, configurable: true })
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
