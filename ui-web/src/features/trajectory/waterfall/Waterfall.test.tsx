// @vitest-environment happy-dom
/* The waterfall draws what the model returns: one row per agent, one per turn,
 * and a turn's spans only when it is open. The arithmetic behind the rows is
 * tested in model.test.ts; what is tested here is that the page uses it. */

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { absorb, resetCapabilities } from '../../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../../state/session/conversation'
import { resetSources, setSources } from '../../../state/sources'
import * as details from '../detailStore'
import * as store from '../store'
import { dayOf, spanOf } from './clock'
import { buildWaterfall } from './model'
import { MAX_LANES } from './OverviewLanes'
import { GUIDE_X, ROW_HEIGHT, TURN_GUIDE_X, Waterfall, displayRows } from './Waterfall'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult, TrajectorySource } from '../types'

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const E = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id,
  revision: 1,
  kind: 'llm.output',
  span_name: 'llm.call',
  slot: 'llm.output',
  trace_id: 't',
  span_id: id,
  parent_span_id: null,
  turn_span_id: 'T1',
  turn_number: 1,
  turn_start: false,
  origin: 'main',
  subagent_scope: null,
  opens_subagent_scope: null,
  dispatched_by_subagent_scope: null,
  sort_key: [String(at).padStart(6, '0'), 0],
  event_time: '2026-10-28T10:00:00.000Z',
  preview: id,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: new Date(Date.parse('2026-10-28T10:00:00.000Z') + at * 1000).toISOString(),
  operation_end: new Date(Date.parse('2026-10-28T10:00:00.000Z') + (at + 1) * 1000).toISOString(),
  duration_ms: 1000,
  charged_ms: 1000,
  timing_basis: 'span_full',
  duration_owner: id,
  meta: {},
  ...over,
})

let rows: TrajectoryEntry[] = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async (): Promise<TrajectoryListResult> => ({
    epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, index_state: READY, complete: true,
  }),
  changes: async (_k, epoch, after) => ({
    epoch, from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
    reset_required: false, index_state: READY,
  }),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
}

async function draw(entries: TrajectoryEntry[]): Promise<void> {
  rows = entries
  render(<Waterfall />)
  await act(async () => { await store.load() })
}

const count = (selector: string): number => document.querySelectorAll(selector).length

/** Opens every agent the page drew folded: with several agents each starts
 *  as one row, and a test about a worker's turns opens it as a reader would. */
const openAgents = (): void => {
  for (const row of [...document.querySelectorAll<HTMLElement>('[data-row="agent"][aria-expanded="false"]')]) {
    act(() => { fireEvent.click(row) })
  }
}

/** The window the axis shows, as the head's label names it. */
const shownWindow = (): { from: number; to: number } => {
  const label = document.querySelector<HTMLElement>('.trajectory-wf-window')!
  return { from: Number(label.dataset.start), to: Number(label.dataset.end) }
}

/* happy-dom drops the modifier keys and the pointer's position out of
   WheelEventInit, so they are written onto the event after construction; a
   browser sends them itself. */
const wheel = (init: WheelEventInit & { ctrlKey?: boolean; shiftKey?: boolean }): WheelEvent => {
  const event = new WheelEvent('wheel', init)
  if (init.ctrlKey) Object.defineProperty(event, 'ctrlKey', { value: true })
  if (init.shiftKey) Object.defineProperty(event, 'shiftKey', { value: true })
  if (typeof init.clientX === 'number') Object.defineProperty(event, 'clientX', { value: init.clientX })
  return event
}

const boxAt = (el: Element, left: number, width: number): void => {
  Object.defineProperty(el, 'getBoundingClientRect', {
    value: () => ({ left, top: 0, right: left + width, bottom: 600, width, height: 600, x: left, y: 0, toJSON: () => ({}) }),
    configurable: true,
  })
}

beforeEach(() => {
  rows = []
  store._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
  store.install()
  unpitch()
  store.sessionChanged('gui:a')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('displayRows', () => {
  it('is the agent, its turns, and the loose rows, flat and in order', () => {
    const w = buildWaterfall([
      E('a', 0),
      E('b', 2, { turn_span_id: 'T2' }),
      E('c', 5, { turn_span_id: null }),
    ], Date.parse('2026-10-28T10:01:00.000Z'))
    const flat = displayRows(w, new Set(), new Set())
    expect(flat.map((row) => row.kind)).toEqual(['agent', 'turn', 'turn', 'span'])
    expect(flat.at(-1)!.row!.entry.entry_id).toBe('c')
  })

  it('stays bounded for a 500-turn session', () => {
    const many = Array.from({ length: 500 }, (_, i) => E(`r${i}`, i, { turn_span_id: `T${i}` }))
    const flat = displayRows(buildWaterfall(many, Date.parse('2026-10-28T10:01:00.000Z')), new Set(), new Set())
    expect(flat.length).toBe(1 + 500)
    /* The model stays linear; the mounted bound is the acceptance test's. */
    expect(flat.length * ROW_HEIGHT).toBeLessThan(30_000)
  })

  it('hides a turn’s spans until the turn is open, and an agent’s rows until it is closed', () => {
    const w = buildWaterfall([E('a', 0), E('b', 1)], Date.parse('2026-10-28T10:01:00.000Z'))
    expect(displayRows(w, new Set(), new Set()).map((row) => row.kind)).toEqual(['agent', 'turn'])
    expect(displayRows(w, new Set(), new Set(['0:T1'])).map((row) => row.kind)).toEqual(['agent', 'turn', 'span', 'span'])
    expect(displayRows(w, new Set(['main']), new Set(['T1'])).map((row) => row.kind)).toEqual(['agent'])
  })
})

describe('the page', () => {
  it('draws one agent row per agent, and a turn row per turn once its agent is open', async () => {
    await draw([
      E('m1', 0),
      E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    openAgents()
    expect(count('[data-row="agent"]')).toBe(2)
    expect(count('[data-row="turn"]')).toBe(2)
  })

  it('reveals a turn’s spans when its row is clicked', async () => {
    await draw([E('m1', 0), E('m2', 1)])
    expect(count('[data-row="span"]')).toBe(0)
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(count('[data-row="span"]')).toBe(2)
  })

  it('hangs the trunk from the agent fold control and a branch from each turn’s', async () => {
    /* The guides are the row's own children: a line inside the label would be
     * only as tall as the label's text, and would stop at every row boundary. */
    const lefts = (row: Element): number[] => [...row.children]
      .filter((el) => el.classList.contains('trajectory-wf-guide'))
      .map((el) => Number.parseFloat((el as HTMLElement).style.left))
    await draw([E('m1', 0), E('m2', 1)])
    expect(lefts(document.querySelector('[data-row="agent"]')!)).toEqual([])
    expect(lefts(document.querySelector('[data-row="turn"]')!)).toEqual([GUIDE_X])
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    for (const span of document.querySelectorAll('[data-row="span"]')) {
      expect(lefts(span)).toEqual([GUIDE_X, TURN_GUIDE_X])
    }
  })

  it('selects a span across scopes and opens the details pane', async () => {
    await draw([E('m1', 0), E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' })])
    openAgents()
    fireEvent.click(document.querySelectorAll('[data-row="turn"]')[1]!)
    const span = [...document.querySelectorAll<HTMLElement>('[data-row="span"]')].find((el) => el.dataset.entry === 's1')!
    fireEvent.click(span)
    expect(store.get().selectedId).toBe('s1')
    expect(store.get().scope).toBe(null)
    expect(details.get().open).toBe(true)
  })

  it('shows how long a turn and a span took', async () => {
    await draw([E('m1', 0, { operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:03.000Z' })])
    expect(document.querySelector('[data-row="turn"]')!.textContent).toContain('3s')
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(document.querySelector('[data-entry="m1"]')!.textContent).toContain('3s')
  })

  it('marks a turn a worker’s report started, naming the worker', async () => {
    await draw([
      E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'coder', label: 'Fix the parser' } }),
      E('m1', 5, { turn_span_id: 'T2', turn_start: true, dispatched_by_subagent_scope: 't:sub' }),
      E('s1', 6, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    openAgents()
    const badge = document.querySelector('[data-from]')!
    expect(badge.getAttribute('data-from')).toBe('t:sub')
    expect(badge.textContent).toContain('coder')
    expect(badge.textContent).not.toContain('t:sub')
  })

  it('does not clip a span row to its turn: a worker that outlives the turn keeps its own extent', async () => {
    const base = Date.parse('2026-10-28T10:00:00.000Z')
    const span = (seconds: number): string => new Date(base + seconds * 1000).toISOString()
    await draw([
      E('m1', 0, { operation_start: span(0), operation_end: span(20.8) }),
      E('spawn', 15, { kind: 'tool.output', operation_start: span(15.7), operation_end: span(149.2) }),
      E('end', 20, { kind: 'turn.end', slot: 'turn.output', operation_start: span(20.7), operation_end: span(20.8) }),
    ])
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    const turnBar = document.querySelector('[data-row="turn"] .trajectory-wf-seg') as HTMLElement
    const spanBar = document.querySelector('[data-entry="spawn"] .trajectory-wf-spanbar') as HTMLElement
    expect(Number.parseFloat(spanBar.style.width)).toBeGreaterThan(Number.parseFloat(turnBar.style.width))
  })

  it('opens the turn that holds a row selected from elsewhere, so the row exists', async () => {
    await draw([E('m1', 0), E('m2', 1)])
    expect(count('[data-row="span"]')).toBe(0)
    act(() => { store.selectAcross('m2', { source: 'link' }) })
    expect(count('[data-row="span"]')).toBeGreaterThan(0)
    expect(document.querySelector('[data-entry="m2"]')).not.toBeNull()
  })
})

it('brings a row selected from elsewhere into view, opening its turn and its agent', async () => {
  const many = Array.from({ length: 60 }, (_, index) => E(`r${index}`, index, { turn_span_id: `T${index}` }))
  await draw(many)
  const box = document.querySelector('.trajectory-list') as HTMLElement
  Object.defineProperty(box, 'clientHeight', { value: 100, configurable: true })
  act(() => { store.selectAcross('r50', { source: 'link' }) })
  /* The scroller moves to where the row will be; the browser's own scroll event
     is what mounts it in a real page. */
  expect(box.scrollTop).toBeGreaterThan(0)
})

describe('the page’s controls', () => {
  it('draws the entries the index marked hidden, with no switch to turn on', async () => {
    await draw([E('shown', 0), E('folded', 1, { meta: { hidden: 'redundant_reply' } })])
    expect(document.querySelector('[data-testid="show-hidden"]')).toBeNull()
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(document.querySelector('[data-entry="folded"]')).not.toBeNull()
  })

  it('labels an idle break when the break has room for the word', async () => {
    const base = Date.parse('2026-10-28T10:00:00.000Z')
    const at2 = (seconds: number): string => new Date(base + seconds * 1000).toISOString()
    /* Short runs either side of a long gap: the collapsed break is a third of
       the axis and the word fits in it. */
    await draw([
      E('a', 0, { operation_start: at2(0), operation_end: at2(1) }),
      E('b', 1, { turn_span_id: 'T2', operation_start: at2(900), operation_end: at2(901) }),
    ])
    const idle = document.querySelector('.trajectory-ruler-idle')
    expect(idle).not.toBeNull()
    expect(idle!.textContent).toMatch(/\d/)
  })

  it('leaves a break squeezed between long runs unlabelled', async () => {
    const base = Date.parse('2026-10-28T10:00:00.000Z')
    const at2 = (seconds: number): string => new Date(base + seconds * 1000).toISOString()
    /* A 1,000px break inside a 41,000px axis: the word would set itself one
       letter per line, which is what it looked like before the rule. */
    await draw([
      E('a', 0, { operation_start: at2(0), operation_end: at2(20) }),
      E('b', 1, { turn_span_id: 'T2', operation_start: at2(900), operation_end: at2(920) }),
    ])
    expect(document.querySelector('.trajectory-ruler-idle')).toBeNull()
  })

  it('moves the selection with the arrow keys and opens a turn with Enter', async () => {
    await draw([E('m1', 0), E('m2', 1, { turn_span_id: 'T2' })])
    const box = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { box.focus() })
    act(() => { fireEvent.keyDown(box, { key: 'ArrowDown' }) })
    expect(store.get().selectedId).not.toBeNull()
    /* Selecting a row brings its turn open; Enter then folds it back. */
    expect(document.querySelector('[data-row="turn"][data-open]')).not.toBeNull()
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    expect(document.querySelector('[data-row="turn"][data-open]')).toBeNull()
  })

  it('zooms toward the pointer on the wheel and returns on a double click', async () => {
    await draw([E('m1', 0), E('m2', 1, { operation_start: new Date(Date.parse('2026-10-28T10:00:00.000Z') + 60_000).toISOString(), operation_end: new Date(Date.parse('2026-10-28T10:00:00.000Z') + 61_000).toISOString() })])
    const box = document.querySelector('.trajectory-list') as HTMLElement
    Object.defineProperty(box, 'clientWidth', { value: 800, configurable: true })
    const before = (document.querySelector('[data-row="turn"] .trajectory-wf-seg') as HTMLElement).style.width
    act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 100, ctrlKey: true, bubbles: true, cancelable: true })) })
    const after = (document.querySelector('[data-row="turn"] .trajectory-wf-seg') as HTMLElement).style.width
    expect(after).not.toBe(before)
    act(() => { fireEvent.doubleClick(box) })
    expect((document.querySelector('[data-row="turn"] .trajectory-wf-seg') as HTMLElement).style.width).toBe(before)
  })
})


it('draws two runs of one instance that share their dispatching turn as two separate turns', async () => {
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    await draw([
      E('d1', 0, { kind: 'tool.output', opens_subagent_scope: 't:a', meta: { instance: 'pr-fix' } }),
      E('a1', 1, { subagent_scope: 't:a', turn_span_id: 'T1' }),
      E('d2', 2, { kind: 'tool.output', opens_subagent_scope: 't:b', meta: { instance: 'pr-fix' } }),
      E('b1', 3, { subagent_scope: 't:b', turn_span_id: 'T1' }),
    ])
    openAgents()
    const turns = [...document.querySelectorAll<HTMLElement>('[data-row="turn"]')]
    expect(turns).toHaveLength(3)
    /* Opening the worker's first turn opens that run's rows and no other's. */
    act(() => { fireEvent.click(turns[1]!) })
    expect(document.querySelector('[data-entry="a1"]')).not.toBeNull()
    expect(document.querySelector('[data-entry="b1"]')).toBeNull()
    /* Two siblings under one key is what leaves a stale row painted over a
       live one once the list moves; React reports it as it renders. */
    const duplicate = errors.mock.calls.some((call) => call.some((arg) => typeof arg === 'string' && arg.includes('same key')))
    expect(duplicate).toBe(false)
  } finally {
    errors.mockRestore()
  }
})

describe('the marks', () => {
  it('wears the host’s own mark on the main agent and the vendor’s on a named worker', async () => {
    await draw([
      E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'claude-code', label: 'Review' } }),
      E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    const marks = [...document.querySelectorAll('[data-row="agent"] img')].map((img) => img.getAttribute('src'))
    expect(marks).toContain('assets/agents/raven.svg')
    expect(marks).toContain('assets/agents/claudecode-color.svg')
  })

  it('falls back to its own glyph for a worker the page ships no mark for', async () => {
    await draw([
      E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'a hand-written agent' } }),
      E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    expect(document.querySelector('[data-row="agent"] .trajectory-wf-glyph')).not.toBeNull()
  })

  it('wears Raven’s own mark on a Raven-* worker', async () => {
    await draw([
      E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'Raven-Research' } }),
      E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    const marks = [...document.querySelectorAll('[data-row="agent"] img')].map((img) => img.getAttribute('src'))
    expect(marks.filter((src) => src === 'assets/agents/raven.svg')).toHaveLength(2)
  })

  it('puts a span’s icon in its own boxed cell', async () => {
    await draw([E('m1', 0), E('m2', 1)])
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(document.querySelector('[data-entry="m1"] .trajectory-wf-ico')).not.toBeNull()
  })
})

it('zooms about the instant under the pointer, read against the axis and not the whole row', async () => {
  await draw([E('m1', 0), E('m2', 30)])
  const box = document.querySelector('.trajectory-list') as HTMLElement
  const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
  /* A 1000px column: the labels take the first 340px, the axis the rest. */
  boxAt(box, 0, 1000)
  boxAt(ruler, 340, 660)
  const whole = shownWindow()
  /* On the axis's left edge, the instant there -- the session's start -- stays put. */
  act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 340, ctrlKey: true, bubbles: true, cancelable: true })) })
  expect(shownWindow().from).toBe(whole.from)
  expect(shownWindow().to).toBeLessThan(whole.to)
  act(() => { fireEvent.doubleClick(box) })
  expect(shownWindow()).toEqual(whole)
  /* In its middle, the instant under the pointer keeps its place: the window
     narrows about it. */
  act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 670, ctrlKey: true, bubbles: true, cancelable: true })) })
  const zoomed = shownWindow()
  expect(zoomed.from).toBeGreaterThan(whole.from)
  expect(Math.abs((zoomed.from + zoomed.to) / 2 - (whole.from + whole.to) / 2)).toBeLessThan(1)
})

it('leaves a plain wheel to the list, and takes only ctrl+wheel or shift+wheel', async () => {
  await draw([E('m1', 0), E('m2', 1)])
  const box = document.querySelector('.trajectory-list') as HTMLElement
  Object.defineProperty(box, 'clientWidth', { value: 800, configurable: true })
  const plain = wheel({ deltaY: -120, clientX: 100, bubbles: true, cancelable: true })
  act(() => { box.dispatchEvent(plain) })
  expect(plain.defaultPrevented).toBe(false)
  const zoom = wheel({ deltaY: -120, clientX: 100, ctrlKey: true, bubbles: true, cancelable: true })
  act(() => { box.dispatchEvent(zoom) })
  expect(zoom.defaultPrevented).toBe(true)
  const pan = wheel({ deltaY: -120, clientX: 100, shiftKey: true, bubbles: true, cancelable: true })
  act(() => { box.dispatchEvent(pan) })
  expect(pan.defaultPrevented).toBe(true)
})

it('scrolls the list for a wheel anywhere over the island, not just on the list', async () => {
  await draw([E('m1', 0), E('m2', 1), E('m3', 2, { turn_span_id: 'T2' })])
  const box = document.querySelector('.trajectory-list') as HTMLElement
  const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
  Object.defineProperty(box, 'clientHeight', { value: 40, configurable: true })
  Object.defineProperty(box, 'scrollHeight', { value: 400, configurable: true })
  const overHeader = wheel({ deltaY: 120, bubbles: true, cancelable: true })
  ruler.dispatchEvent(overHeader)
  expect(overHeader.defaultPrevented).toBe(true)
  expect(box.scrollTop).toBe(120)
  const overList = wheel({ deltaY: 120, bubbles: true, cancelable: true })
  box.dispatchEvent(overList)
  expect(overList.defaultPrevented).toBe(false)
})

it('selects the interval dragged across the ruler without moving the axis', async () => {
  await draw([E('m1', 0), E('m2', 60), E('m3', 120)])
  const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
  Object.defineProperty(ruler, 'getBoundingClientRect', {
    value: () => ({ left: 100, top: 0, right: 500, bottom: 56, width: 400, height: 56, x: 100, y: 0, toJSON: () => ({}) }),
    configurable: true,
  })
  const before = shownWindow()
  fireEvent.pointerDown(ruler, { clientX: 200, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerMove(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
  fireEvent.pointerUp(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
  /* The interval stays picked, with a grip at each edge ... */
  expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(1)
  expect(document.querySelectorAll('.trajectory-ruler-grip')).toHaveLength(2)
  /* ... and the axis keeps the window it had: selecting does not zoom. */
  expect(shownWindow()).toEqual(before)
  /* A click takes the pick back. */
  fireEvent.pointerDown(ruler, { clientX: 300, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerUp(ruler, { clientX: 300, clientY: 10, pointerId: 1 })
  expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(0)
})

it('moves one edge of the picked interval from its handle', async () => {
  await draw([
    E('a', 0, { turn_span_id: 'T1' }),
    E('b', 60, { turn_span_id: 'T2' }),
    E('c', 120, { turn_span_id: 'T3' }),
  ])
  const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
  Object.defineProperty(ruler, 'getBoundingClientRect', {
    value: () => ({ left: 100, top: 0, right: 500, bottom: 56, width: 400, height: 56, x: 100, y: 0, toJSON: () => ({}) }),
    configurable: true,
  })
  fireEvent.pointerDown(ruler, { clientX: 250, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerMove(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
  fireEvent.pointerUp(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
  const band = (): HTMLElement => document.querySelector('.trajectory-ruler-pick') as HTMLElement
  const width = (): number => Number.parseFloat(band().style.width)
  const left = band().style.left
  const grips = document.querySelectorAll<HTMLElement>('.trajectory-ruler-grip')
  expect(grips).toHaveLength(2)
  /* Only the right edge moves; the left one stays where it was. */
  fireEvent.pointerDown(grips[1]!, { clientX: 350, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerMove(grips[1]!, { clientX: 305, clientY: 10, pointerId: 1 })
  fireEvent.pointerUp(grips[1]!, { clientX: 305, clientY: 10, pointerId: 1 })
  expect(band().style.left).toBe(left)
  expect(width()).toBeLessThan(25)
  /* And the rows are filtered to what the edge now leaves. */
  expect(count('[data-row="turn"]')).toBe(1)
})

describe('a live session growing under the reader', () => {
  const grow = (entry: TrajectoryEntry): void => {
    act(() => {
      store.applyChanges({
        epoch: 'e1', from_revision: 100, to_revision: 101, upserts: [entry], removed: [], has_more: false,
        reset_required: false, index_state: READY,
      })
    })
  }
  const zoomIn = (): void => {
    const box = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 100, ctrlKey: true, bubbles: true, cancelable: true })) })
  }
  const pickOnRuler = (): void => {
    const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
    Object.defineProperty(ruler, 'getBoundingClientRect', {
      value: () => ({ left: 100, top: 0, right: 500, bottom: 56, width: 400, height: 56, x: 100, y: 0, toJSON: () => ({}) }),
      configurable: true,
    })
    fireEvent.pointerDown(ruler, { clientX: 200, clientY: 10, button: 0, pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
    fireEvent.pointerUp(ruler, { clientX: 350, clientY: 10, pointerId: 1 })
  }

  it('keeps the reader’s zoom when a step lands past the session’s end', async () => {
    await draw([E('m1', 0), E('m2', 30)])
    const whole = shownWindow()
    zoomIn()
    const zoomed = shownWindow()
    expect(zoomed.to - zoomed.from).toBeLessThan(whole.to - whole.from)
    grow(E('m3', 40))
    expect(store.get().entries).toHaveLength(3)
    expect(shownWindow()).toEqual(zoomed)
  })

  it('keeps the picked interval when a step lands past the session’s end', async () => {
    await draw([E('m1', 0), E('m2', 30)])
    pickOnRuler()
    expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(1)
    grow(E('m3', 40))
    expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(1)
  })

  it('drops the picked interval when the reader zooms', async () => {
    await draw([E('m1', 0), E('m2', 30)])
    pickOnRuler()
    zoomIn()
    expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(0)
  })

  it('drops the zoom and the pick when the conversation changes', async () => {
    await draw([E('m1', 0), E('m2', 30)])
    zoomIn()
    pickOnRuler()
    rows = [E('n1', 0), E('n2', 30)]
    await act(async () => {
      store.sessionChanged('gui:b')
      await store.load()
    })
    expect(document.querySelector('.trajectory-wf-window')!.textContent).toContain('gui.trajectory.window.whole')
    expect(document.querySelectorAll('.trajectory-ruler-pick')).toHaveLength(0)
  })
})

it('keeps only what was running inside the picked interval', async () => {
  await draw([
    E('a', 0, { turn_span_id: 'T1' }),
    E('b', 60, { turn_span_id: 'T2' }),
    E('c', 120, { turn_span_id: 'T3' }),
  ])
  const ruler = document.querySelector('.trajectory-ruler') as HTMLElement
  Object.defineProperty(ruler, 'getBoundingClientRect', {
    value: () => ({ left: 100, top: 0, right: 500, bottom: 56, width: 400, height: 56, x: 100, y: 0, toJSON: () => ({}) }),
    configurable: true,
  })
  expect(count('[data-row="turn"]')).toBe(3)
  /* The middle of an axis that runs 0..121s plus its trailing scale: only the
     middle turn. */
  fireEvent.pointerDown(ruler, { clientX: 250, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerMove(ruler, { clientX: 305, clientY: 10, pointerId: 1 })
  fireEvent.pointerUp(ruler, { clientX: 305, clientY: 10, pointerId: 1 })
  expect(count('[data-row="turn"]')).toBe(1)
  expect(document.querySelector('[data-row="turn"]')!.textContent).toContain('"n":2')
  /* A click takes the pick back, and the rows with it. */
  fireEvent.pointerDown(ruler, { clientX: 120, clientY: 10, button: 0, pointerId: 1 })
  fireEvent.pointerUp(ruler, { clientX: 120, clientY: 10, pointerId: 1 })
  expect(count('[data-row="turn"]')).toBe(3)
})

it('lets the opened overview take the wheel while it still has lanes to show', async () => {
  const entries = [E('m', 0)]
  for (let i = 0; i < MAX_LANES + 2; i += 1) {
    entries.push(E(`s${i}`, 10 + i, { subagent_scope: `t:s${i}`, turn_span_id: `S${i}` }))
  }
  await draw(entries)
  fireEvent.click(document.querySelector('[data-lane-more]')!)
  const ov = document.querySelector('.trajectory-ov') as HTMLElement
  /* The sheet states the overflow; happy-dom applies no stylesheet. */
  ov.style.overflowY = 'auto'
  Object.defineProperty(ov, 'clientHeight', { value: 100, configurable: true })
  Object.defineProperty(ov, 'scrollHeight', { value: 300, configurable: true })
  let top = 0
  Object.defineProperty(ov, 'scrollTop', { get: () => top, set: (value: number) => { top = value }, configurable: true })
  const lane = ov.querySelector('[data-lane]') as HTMLElement
  const down = wheel({ deltaY: 120, bubbles: true, cancelable: true })
  lane.dispatchEvent(down)
  expect(down.defaultPrevented).toBe(false)
  /* At the end of its own scroll, the wheel goes on to the list below. */
  top = 200
  const box = document.querySelector('.trajectory-list') as HTMLElement
  const end = wheel({ deltaY: 120, bubbles: true, cancelable: true })
  lane.dispatchEvent(end)
  expect(end.defaultPrevented).toBe(true)
  expect(box.scrollTop).toBe(120)
})

it('sends the door’s arrow to the worker’s own first row', async () => {
  await draw([
    E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'coder' } }),
    E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ])
  openAgents()
  fireEvent.click(document.querySelector('[data-row="turn"]')!)
  const arrow = document.querySelector('[data-entry="door"] .trajectory-scope-opens') as HTMLElement
  act(() => { fireEvent.click(arrow) })
  expect(store.get().selectedId).toBe('s1')
})

it('sends a later run’s door to that run, not to the first run of the agent it folds into', async () => {
  await draw([
    E('d1', 0, { kind: 'tool.output', opens_subagent_scope: 't:a', meta: { agent: 'claude-code' } }),
    E('a1', 1, { subagent_scope: 't:a', turn_span_id: 'TA' }),
    E('d2', 10, { kind: 'tool.output', opens_subagent_scope: 't:b', meta: { agent: 'claude-code' } }),
    E('b1', 11, { subagent_scope: 't:b', turn_span_id: 'TB' }),
  ])
  openAgents()
  /* The host and one worker: both runs fold into the agent their name gives. */
  expect(document.querySelectorAll('[data-row="agent"]')).toHaveLength(2)
  fireEvent.click(document.querySelector('[data-row="turn"]')!)
  act(() => { fireEvent.click(document.querySelector('[data-entry="d2"] .trajectory-scope-opens') as HTMLElement) })
  expect(store.get().selectedId).toBe('b1')
  act(() => { fireEvent.click(document.querySelector('[data-entry="d1"] .trajectory-scope-opens') as HTMLElement) })
  expect(store.get().selectedId).toBe('a1')
})

it('draws the rows as a tree the keyboard can reach, each row at its level and fold state', async () => {
  await draw([E('a', 0), E('b', 2)])
  const tree = document.querySelector('[role="tree"]') as HTMLElement
  expect(tree).not.toBeNull()
  expect(tree.tabIndex).toBe(0)
  expect(tree.getAttribute('aria-label')).toBe('gui.trajectory.agents')
  const agent = document.querySelector('[data-row="agent"]') as HTMLElement
  const turn = document.querySelector('[data-row="turn"]') as HTMLElement
  expect([agent.getAttribute('role'), agent.getAttribute('aria-level'), agent.getAttribute('aria-expanded')]).toEqual(['treeitem', '1', 'true'])
  expect([turn.getAttribute('role'), turn.getAttribute('aria-level'), turn.getAttribute('aria-expanded')]).toEqual(['treeitem', '2', 'false'])
  act(() => { fireEvent.click(turn) })
  expect(document.querySelector('[data-row="turn"]')!.getAttribute('aria-expanded')).toBe('true')
  const span = document.querySelector('[data-entry="b"]') as HTMLElement
  expect([span.getAttribute('role'), span.getAttribute('aria-level')]).toEqual(['treeitem', '3'])
  /* Every drawn row is an item of the tree; the grid lines are not. */
  expect(tree.querySelectorAll('[role="treeitem"]')).toHaveLength(document.querySelectorAll('.trajectory-wf-row').length)
  expect(tree.querySelector('.trajectory-wf-links')!.getAttribute('aria-hidden')).toBe('true')
})

it('marks the selected row for the eye and for a screen reader, and moves the mark with the keys', async () => {
  await draw([E('a', 0), E('b', 2)])
  act(() => { fireEvent.click(document.querySelector('[data-row="turn"]')!) })
  const tree = document.querySelector('[role="tree"]') as HTMLElement
  const row = (id: string): HTMLElement => document.querySelector(`[data-entry="${id}"]`) as HTMLElement
  expect(tree.hasAttribute('aria-activedescendant')).toBe(false)
  act(() => { fireEvent.click(row('b')) })
  expect([row('a').getAttribute('aria-selected'), row('b').getAttribute('aria-selected')]).toEqual(['false', 'true'])
  expect(tree.getAttribute('aria-activedescendant')).toBe(row('b').id)
  act(() => { fireEvent.keyDown(tree, { key: 'ArrowUp' }) })
  expect([row('a').getAttribute('aria-selected'), row('b').getAttribute('aria-selected')]).toEqual(['true', 'false'])
  expect(tree.getAttribute('aria-activedescendant')).toBe(row('a').id)
})

it('folds a group from its own button, and Enter on that button is the button’s, not the selected row’s', async () => {
  await draw([E('a', 0), E('b', 2, { turn_span_id: 'T2' })])
  const buttons = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('[data-row] button.trajectory-wf-chev')]
  /* One fold button per agent and per turn, each saying whether it is open. */
  expect(buttons().map((b) => b.getAttribute('aria-expanded'))).toEqual(['true', 'false', 'false'])
  act(() => { fireEvent.click(buttons()[1]!) })
  expect(buttons().map((b) => b.getAttribute('aria-expanded'))).toEqual(['true', 'true', 'false'])
  act(() => { fireEvent.click(document.querySelector('[data-entry="a"]') as HTMLElement) })
  /* Enter pressed on the second turn's button: the first turn, which holds the
     selection, must not fold under it. */
  act(() => { fireEvent.keyDown(buttons()[2]!, { key: 'Enter' }) })
  expect(buttons()[1]!.getAttribute('aria-expanded')).toBe('true')
  expect(document.querySelector('[data-entry="a"]')).not.toBeNull()
  /* The arrows are still the tree's: from a fold button they move the selection on. */
  act(() => { fireEvent.keyDown(buttons()[2]!, { key: 'ArrowDown' }) })
  expect(store.get().selectedId).toBe('b')
})

it('marks a failed span, and the turn that holds it while it is folded', async () => {
  await draw([
    E('ok1', 0),
    E('bad', 2, { kind: 'error', span_name: 'tool.call', slot: 'error', failure_entry: true, operation_status: 'error' }),
    E('ok2', 4, { turn_span_id: 'T2' }),
  ])
  const turns = [...document.querySelectorAll<HTMLElement>('[data-row="turn"]')]
  expect(turns.map((turn) => turn.hasAttribute('data-failed'))).toEqual([true, false])
  expect(turns[0]!.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('gui.trajectory.status.error')
  act(() => { fireEvent.click(turns[0]!) })
  const bad = document.querySelector('[data-entry="bad"]') as HTMLElement
  const ok = document.querySelector('[data-entry="ok1"]') as HTMLElement
  expect(bad.hasAttribute('data-failed')).toBe(true)
  expect(bad.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('gui.trajectory.status.error')
  expect(bad.querySelector('.trajectory-wf-spanbar')?.classList.contains('trajectory-wf-kind-failure')).toBe(true)
  expect(ok.hasAttribute('data-failed')).toBe(false)
  expect(ok.querySelector('[role="img"]')).toBeNull()
})

it('counts a failed operation once, not once per entry it wrote', async () => {
  /* A failed call writes its input and output under the failed status, and the
     index marks one entry of the span as the failure. */
  await draw([
    E('in', 0, { kind: 'tool.input', span_name: 'tool.call', slot: 'tool.input', span_id: 'call', operation_status: 'error' }),
    E('out', 0, { kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', span_id: 'call', operation_status: 'error' }),
    E('err', 0, { kind: 'error', span_name: 'tool.call', slot: 'error', span_id: 'call', operation_status: 'error', failure_entry: true }),
  ])
  expect(document.querySelector('[data-bad]')?.textContent).toBe('1')
})

it('counts a model or tool call once, not once per entry it wrote', async () => {
  /* One model call writes its input, its thinking and its output as three
     entries of one span; one tool call writes two. */
  await draw([
    E('ask', 0, { kind: 'llm.input', span_name: 'llm.call', slot: 'llm.input', span_id: 'model-1' }),
    E('think', 0, { kind: 'llm.thinking', span_name: 'llm.call', slot: 'llm.thinking', span_id: 'model-1' }),
    E('say', 0, { kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output', span_id: 'model-1' }),
    E('in', 1, { kind: 'tool.input', span_name: 'tool.call', slot: 'tool.input', span_id: 'tool-1' }),
    E('out', 1, { kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', span_id: 'tool-1' }),
  ])
  const stat = (key: string) =>
    [...document.querySelectorAll('.trajectory-wf-stat')]
      .find((cell) => cell.querySelector('.trajectory-wf-statk')?.textContent === key)
      ?.querySelector('.trajectory-wf-statv')?.textContent
  expect(stat('gui.trajectory.stat.llm')).toBe('1')
  expect(stat('gui.trajectory.stat.tool')).toBe('1')
})

it('re-renders a scroll without walking every row of the session', async () => {
  /* The list re-renders on every scroll event; what it does then must scale
     with the rows on screen, not with the session. Reading a row's clock is
     what a walk over the whole session costs, so that is what is counted. */
  const n = 2000
  await draw(Array.from({ length: n }, (_, i) => E(`e${i}`, i, { turn_span_id: `T${Math.floor(i / 10)}` })))
  const box = document.querySelector('.trajectory-list') as HTMLElement
  /* Two turns open where the scroll lands, so the rows it draws read their
     own clocks -- the calls the control below counts. The header's figures
     have swept every turn's bar already, so a folded turn reads none. */
  for (const id of ['T3', 'T4']) {
    const turn = [...document.querySelectorAll<HTMLElement>('[data-row="turn"]')].find((el) => el.textContent?.includes(`"n":${Number(id.slice(1)) + 1}}`))
    act(() => { fireEvent.click(turn!) })
  }
  const parse = vi.spyOn(Date, 'parse')
  try {
    act(() => { box.scrollTop = 300; fireEvent.scroll(box) })
    expect(parse.mock.calls.length).toBeGreaterThan(0)
    expect(parse.mock.calls.length).toBeLessThan(n / 2)
  } finally {
    parse.mockRestore()
  }
})

it('brings a selected row that belongs to no turn into view, opening its agent', async () => {
  const turns = Array.from({ length: 40 }, (_, i) => E(`r${i}`, i * 2, { turn_span_id: `T${i}` }))
  await draw([...turns, E('loose', 100, { turn_span_id: null })])
  const box = document.querySelector('.trajectory-list') as HTMLElement
  Object.defineProperty(box, 'clientHeight', { value: 300, configurable: true })
  act(() => { fireEvent.click(document.querySelector('[data-row="agent"]')!) })
  expect(document.querySelector('[data-row="agent"]')!.getAttribute('aria-expanded')).toBe('false')
  act(() => { fireEvent.keyDown(box, { key: 'End' }) })
  expect(store.get().selectedId).toBe('loose')
  expect(document.querySelector('[data-row="agent"]')!.getAttribute('aria-expanded')).toBe('true')
  /* The agent's row, its forty turns, then the row outside them. */
  expect(box.scrollTop).toBe(41 * ROW_HEIGHT)
})

it('brings a row selected from elsewhere into view through its folded agent and its closed turn', async () => {
  await draw([
    E('door', 0, { kind: 'tool.output', opens_subagent_scope: 't:sub', meta: { agent: 'coder' } }),
    E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ])
  expect(document.querySelector('[data-entry="s1"]')).toBeNull()
  act(() => { store.selectAcross('s1', { source: 'link' }) })
  expect(document.querySelector('[data-entry="s1"]')).not.toBeNull()
})

it('leaves a turn folded when the reader folds it, even with the selection inside', async () => {
  await draw([E('m1', 0), E('m2', 1)])
  fireEvent.click(document.querySelector('[data-row="turn"]')!)
  const span = document.querySelector('[data-entry="m2"]') as HTMLElement
  act(() => { fireEvent.click(span) })
  expect(store.get().selectedId).toBe('m2')
  expect(document.querySelectorAll('[data-row="span"]')).toHaveLength(2)
  act(() => { fireEvent.click(document.querySelector('[data-row="turn"]')!) })
  expect(document.querySelectorAll('[data-row="span"]')).toHaveLength(0)
})

it('prints a second line under the header’s figures', async () => {
  await draw([
    E('ask', 0, { span_id: 'model-1' }),
    E('run', 1, { kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', span_id: 'tool-1', meta: { tool: 'bash' } }),
    E('door', 2, { opens_subagent_scope: 'x' }),
    E('x1', 3, { subagent_scope: 'x', span_name: 'subagent.external', kind: 'subagent.external', meta: { agent: 'Claude Code' } }),
  ])
  const sub = (key: string): string | null | undefined =>
    [...document.querySelectorAll('.trajectory-wf-stat')]
      .find((cell) => cell.querySelector('.trajectory-wf-statk')?.textContent === key)
      ?.querySelector('.trajectory-wf-statsub')?.textContent
  const start = Date.parse('2026-10-28T10:00:00.000Z')
  const span = spanOf(start, start + 4000)
  expect(sub('gui.trajectory.stat.duration')).toBe(`gui.trajectory.stat.span ${JSON.stringify(span)}`)
  expect(sub('gui.trajectory.stat.agents')).toBe('gui.trajectory.stat.main {"n":1} · gui.trajectory.stat.external {"n":1}')
  /* The main turn runs 3 s and the worker's 1 s: the middle pair, averaged. */
  expect(sub('gui.trajectory.stat.turns')).toBe('gui.trajectory.stat.median {"duration":"2s"}')
  expect(sub('gui.trajectory.stat.llm')).toMatch(/^gui\.trajectory\.stat\.model_share \{"percent":\d+\}$/)
  expect(sub('gui.trajectory.stat.tool')).toBe('gui.trajectory.stat.tool_share {"name":"bash","percent":100}')
})

it('jumps from the error count to the earliest failure, opening what holds it', async () => {
  await draw([
    E('ok', 0),
    E('late', 4, { turn_span_id: 'T2', failure_entry: true, operation_status: 'error' }),
    E('early', 2, { failure_entry: true, operation_status: 'error' }),
  ])
  const jump = document.querySelector<HTMLButtonElement>('button.trajectory-wf-jump')
  expect(jump?.textContent).toBe('gui.trajectory.stat.jump')
  act(() => { fireEvent.click(jump!) })
  expect(store.get().selectedId).toBe('early')
  expect(details.get().open).toBe(true)
  expect(document.querySelector('[data-entry="early"]')).not.toBeNull()
})

it('offers no jump when nothing failed', async () => {
  await draw([E('ok', 0)])
  expect(document.querySelector('.trajectory-wf-jump')).toBeNull()
})

describe('the clock ruler', () => {
  const local = (h: number, m = 0): string => new Date(2026, 9, 28, h, m).toISOString()
  const session = (): TrajectoryEntry[] => [E('long', 0, { operation_start: local(10), operation_end: local(13, 10) })]

  it('labels the ruler in clock time, the whole hours major', async () => {
    await draw(session())
    const labels = [...document.querySelectorAll<HTMLElement>('.trajectory-ruler b')]
    expect(labels.map((b) => b.textContent)).toEqual([
      '10:15', '10:30', '10:45', '11:00', '11:15', '11:30', '11:45', '12:00', '12:15', '12:30', '12:45', '13:00',
    ])
    expect(labels.filter((b) => b.hasAttribute('data-major')).map((b) => b.textContent)).toEqual(['11:00', '12:00', '13:00'])
  })

  it('draws a grid line under every mark, the major ones set apart', async () => {
    await draw(session())
    expect(count('.trajectory-wf-grid')).toBe(13)
    expect(count('.trajectory-wf-grid[data-major]')).toBe(4)
  })

  it('names the window it shows: its day, both ends, and that it is the whole session', async () => {
    await draw(session())
    const label = document.querySelector<HTMLElement>('.trajectory-wf-window')!
    expect(label.querySelector('b')?.textContent).toBe(dayOf(Date.parse(local(10)), { weekday: true }))
    expect(label.textContent).toContain('gui.trajectory.window.whole {"from":"10:00","to":"13:10"}')
  })

  it('names a zoomed window by its length', async () => {
    await draw(session())
    const box = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 100, ctrlKey: true, bubbles: true, cancelable: true })) })
    const label = document.querySelector<HTMLElement>('.trajectory-wf-window')!
    expect(label.textContent).toMatch(/gui\.trajectory\.window\.zoomed \{"from":"\d\d:\d\d","to":"\d\d:\d\d","length":"[^"]+"\}/)
    expect(shownWindow().to - shownWindow().from).toBeLessThan(Date.parse(local(13, 10)) - Date.parse(local(10)))
  })
})

describe('the window presets and the fold-all button', () => {
  const local = (h: number, m = 0): string => new Date(2026, 9, 28, h, m).toISOString()
  const preset = (name: string): HTMLButtonElement =>
    [...document.querySelectorAll<HTMLButtonElement>('.trajectory-wf-zoom button')].find((b) => b.textContent === `gui.trajectory.zoom.${name}`)!
  const pressed = (): string[] =>
    [...document.querySelectorAll<HTMLButtonElement>('.trajectory-wf-zoom button[aria-pressed="true"]')].map((b) => b.textContent ?? '')

  it('narrows the window to a preset about its middle, and Fit gives the whole session back', async () => {
    await draw([E('long', 0, { operation_start: local(9), operation_end: local(19) })])
    expect(pressed()).toEqual(['gui.trajectory.zoom.fit'])
    act(() => { fireEvent.click(preset('1h')) })
    const narrowed = shownWindow()
    expect(narrowed.to - narrowed.from).toBe(3_600_000)
    /* The middle of 09:00 to the axis's end, its 2% trailing scale included:
       14:06, so the hour opens at 13:36. */
    expect(narrowed.from).toBe(Date.parse(local(13, 36)))
    expect(pressed()).toEqual(['gui.trajectory.zoom.1h'])
    act(() => { fireEvent.click(preset('fit')) })
    expect(pressed()).toEqual(['gui.trajectory.zoom.fit'])
    expect(document.querySelector('.trajectory-wf-window')!.textContent).toContain('gui.trajectory.window.whole')
  })

  it('presses no preset for a window the wheel made', async () => {
    await draw([E('long', 0, { operation_start: local(9), operation_end: local(19) })])
    expect(document.querySelectorAll('.trajectory-wf-zoom button')).toHaveLength(4)
    const box = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { box.dispatchEvent(wheel({ deltaY: -240, clientX: 100, ctrlKey: true, bubbles: true, cancelable: true })) })
    expect(pressed()).toEqual([])
  })

  it('folds every agent and closes every turn from the one button', async () => {
    await draw([
      E('m1', 0), E('door', 1, { opens_subagent_scope: 'x' }),
      E('x1', 2, { subagent_scope: 'x', turn_span_id: 'X1' }),
    ])
    openAgents()
    act(() => { fireEvent.click(document.querySelector('[data-row="turn"]')!) })
    expect(count('[data-row="span"]')).toBeGreaterThan(0)
    act(() => { fireEvent.click(document.querySelector('button.trajectory-wf-foldall')!) })
    expect(count('[data-row="span"]')).toBe(0)
    expect(count('[data-row="turn"]')).toBe(0)
    expect([...document.querySelectorAll('[data-row="agent"]')].map((row) => row.getAttribute('aria-expanded'))).toEqual(['false', 'false'])
    /* Opened again, the agent shows its turns closed: the turns were folded too. */
    act(() => { fireEvent.click(document.querySelector('[data-row="agent"]')!) })
    expect(count('[data-row="turn"]')).toBeGreaterThan(0)
    expect(count('[data-row="span"]')).toBe(0)
  })
})

describe('the busy column', () => {
  const base = Date.parse('2026-10-28T10:00:00.000Z')
  const at2 = (seconds: number): string => new Date(base + seconds * 1000).toISOString()
  const busy = (row: Element | null | undefined): string | null | undefined => row?.querySelector('.trajectory-wf-busy')?.textContent

  it('prints each row’s busy time in a column of its own, not beside its bar', async () => {
    await draw([
      E('a', 0, { operation_start: at2(0), operation_end: at2(3) }),
      E('b', 10, { turn_span_id: 'T2', operation_start: at2(10), operation_end: at2(11) }),
    ])
    /* The agent worked 3 s and then 1 s; it was alive for 11. */
    expect(busy(document.querySelector('[data-row="agent"]'))).toBe('4s')
    expect([...document.querySelectorAll('[data-row="turn"]')].map(busy)).toEqual(['3s', '1s'])
    act(() => { fireEvent.click(document.querySelector('[data-row="turn"]')!) })
    expect(busy(document.querySelector('[data-entry="a"]'))).toBe('3s')
    expect(count('.trajectory-wf-time')).toBe(0)
  })

  it('heads the column above the rows', async () => {
    await draw([E('a', 0)])
    expect(document.querySelector('.trajectory-wf-head .trajectory-wf-busyhead')?.textContent).toBe('gui.trajectory.busy')
  })
})

describe('a folded agent', () => {
  const local = (h: number, m = 0): string => new Date(2026, 9, 28, h, m).toISOString()
  const step = (id: string, from: string, to: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    E(id, 0, { operation_start: from, operation_end: to, ...over })
  const agentRow = (index: number): HTMLElement => document.querySelectorAll<HTMLElement>('[data-row="agent"]')[index]!

  it('starts folded when the session has several agents', async () => {
    await draw([step('m', local(10), local(10, 5)), step('door', local(10, 1), local(10, 2), { opens_subagent_scope: 'x' }),
      step('x1', local(10, 2), local(10, 4), { subagent_scope: 'x', turn_span_id: 'X1' })])
    expect([agentRow(0), agentRow(1)].map((row) => row.getAttribute('aria-expanded'))).toEqual(['false', 'false'])
    expect(count('[data-row="turn"]')).toBe(0)
  })

  it('starts open when it is the session’s only agent', async () => {
    await draw([step('m', local(10), local(10, 5))])
    expect(agentRow(0).getAttribute('aria-expanded')).toBe('true')
    expect(count('[data-row="turn"]')).toBe(1)
  })

  it('draws a capsule per run of work, on a faint line from its first to its last', async () => {
    await draw([step('a', local(10), local(10, 20)), step('b', local(11), local(11, 30), { turn_span_id: 'T2' })])
    const row = agentRow(0)
    expect(row.querySelectorAll('.trajectory-wf-cap')).toHaveLength(2)
    expect(row.querySelectorAll('.trajectory-wf-life')).toHaveLength(1)
    expect(row.querySelector('.trajectory-wf-agentbar')).toBeNull()
  })

  it('wears the main agent’s colour on the main row only', async () => {
    await draw([step('m', local(10), local(10, 5)), step('door', local(10, 1), local(10, 2), { opens_subagent_scope: 'x' }),
      step('x1', local(10, 2), local(10, 4), { subagent_scope: 'x', turn_span_id: 'X1' })])
    expect([agentRow(0), agentRow(1)].map((row) => row.hasAttribute('data-main'))).toEqual([true, false])
  })

  it('marks where a failure happened on its agent’s row', async () => {
    await draw([step('a', local(10), local(10, 20)),
      step('bad', local(10, 5), local(10, 6), { failure_entry: true, operation_status: 'error' })])
    expect(agentRow(0).querySelectorAll('.trajectory-wf-diamond')).toHaveLength(1)
  })

  it('names when it last worked once all of its work lies before the window', async () => {
    await draw([
      step('m', local(9), local(17)),
      step('door', local(9), local(9), { opens_subagent_scope: 'x' }),
      step('x1', local(9), local(9, 10), { subagent_scope: 'x', turn_span_id: 'X1' }),
    ])
    const preset = [...document.querySelectorAll<HTMLButtonElement>('.trajectory-wf-zoom button')].find((b) => b.textContent === 'gui.trajectory.zoom.1h')!
    act(() => { fireEvent.click(preset) })
    /* Wholly before the window, the worker waits in the earlier fold. */
    act(() => { fireEvent.click(document.querySelector('[data-row="fold"]')!) })
    const chip = agentRow(1).querySelector('.trajectory-wf-next[data-side="start"]')
    expect(chip?.textContent).toBe('gui.trajectory.last {"time":"09:10"}')
  })

  it('points past an end of the window at work it cannot show, and names when it works next', async () => {
    await draw([
      step('a', local(9), local(9, 30)),
      step('door', local(9, 5), local(9, 6), { opens_subagent_scope: 'x' }),
      step('c', local(16), local(16, 30), { turn_span_id: 'T3' }),
      step('x1', local(11), local(14), { subagent_scope: 'x', turn_span_id: 'X1' }),
    ])
    /* The idle stretches either side of the worker fold away, so an hour about
       the axis's middle lands inside the worker's three: 12:02 to 13:02, with
       nothing of the main agent in it and its work on either side. */
    const preset = [...document.querySelectorAll<HTMLButtonElement>('.trajectory-wf-zoom button')].find((b) => b.textContent === 'gui.trajectory.zoom.1h')!
    act(() => { fireEvent.click(preset) })
    const row = agentRow(0)
    expect(row.querySelector('.trajectory-wf-edge[data-side="start"]')).not.toBeNull()
    expect(row.querySelector('.trajectory-wf-edge[data-side="end"]')).not.toBeNull()
    expect(row.querySelectorAll('.trajectory-wf-cap')).toHaveLength(0)
    expect(row.querySelector('.trajectory-wf-next')?.textContent).toBe('gui.trajectory.next {"time":"16:00"}')
  })
})

describe('the agents outside the window', () => {
  const local = (h: number, m = 0): string => new Date(2026, 9, 28, h, m).toISOString()
  const step = (id: string, from: string, to: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
    E(id, 0, { operation_start: from, operation_end: to, ...over })
  const worker = (scope: string, from: string, to: string, name: string): TrajectoryEntry[] => [
    step(`door-${scope}`, from, from, { opens_subagent_scope: scope, meta: { instance: name, agent: name } }),
    step(`${scope}-1`, from, to, { subagent_scope: scope, turn_span_id: `${scope}-T` }),
  ]
  /* Main works all day; one worker early, one in the hour the preset picks
     (12:34 to 13:34, the axis's middle), one late. */
  const session = (): TrajectoryEntry[] => [
    step('m', local(9), local(17)),
    ...worker('a', local(9), local(9, 10), 'early'),
    ...worker('b', local(12, 40), local(13), 'inside'),
    ...worker('c', local(16), local(16, 20), 'late'),
  ]
  const preset = (name: string): HTMLButtonElement =>
    [...document.querySelectorAll<HTMLButtonElement>('.trajectory-wf-zoom button')].find((b) => b.textContent === `gui.trajectory.zoom.${name}`)!
  const folds = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('[data-row="fold"]')]

  it('folds the agents wholly before the window into one row, and those after it into another', async () => {
    await draw(session())
    expect(folds()).toHaveLength(0)
    act(() => { fireEvent.click(preset('1h')) })
    expect(folds().map((row) => row.textContent)).toEqual([
      'gui.trajectory.fold.earlier_one {"time":"09:10"}',
      'gui.trajectory.fold.later_one {"time":"16:00"}',
    ])
    expect(count('[data-row="agent"]')).toBe(2)
    /* The earlier row stands after the main agent, the later one at the end. */
    expect([...document.querySelectorAll('[data-row]')].map((row) => row.getAttribute('data-row'))).toEqual(['agent', 'fold', 'agent', 'fold'])
  })

  it('opens a fold in place, its agents beneath it', async () => {
    await draw(session())
    act(() => { fireEvent.click(preset('1h')) })
    const earlier = folds()[0]!
    expect(earlier.getAttribute('aria-expanded')).toBe('false')
    act(() => { fireEvent.click(earlier) })
    expect(folds()[0]!.getAttribute('aria-expanded')).toBe('true')
    expect(count('[data-row="agent"]')).toBe(3)
    expect([...document.querySelectorAll('[data-row]')].map((row) => row.getAttribute('data-row'))).toEqual(['agent', 'fold', 'agent', 'agent', 'fold'])
  })

  it('keeps the main agent in a row of its own, even with all of its work outside the window', async () => {
    await draw([
      step('m', local(9), local(9, 30)),
      ...worker('b', local(12), local(17), 'afternoon'),
    ])
    act(() => { fireEvent.click(preset('1h')) })
    expect(folds()).toHaveLength(0)
    expect(document.querySelector('[data-row="agent"][data-main]')).not.toBeNull()
  })

  it('opens the fold that holds a row selected from elsewhere', async () => {
    await draw(session())
    act(() => { fireEvent.click(preset('1h')) })
    act(() => { store.selectAcross('c-1', { source: 'link' }) })
    expect(document.querySelector('[data-entry="c-1"]')).not.toBeNull()
  })
})

describe('an agent’s name', () => {
  const door = (id: string, scope: string, meta: Record<string, string>): TrajectoryEntry =>
    E(id, 0, { kind: 'tool.output', opens_subagent_scope: scope, meta })
  const agentRow = (index: number): HTMLElement => document.querySelectorAll<HTMLElement>('[data-row="agent"]')[index]!

  it('names a folded worker by its task, and leaves its runner to the mark and the hover', async () => {
    await draw([door('d', 'x', { agent: 'Claude Code', label: 'Fix the parser' }), E('x1', 1, { subagent_scope: 'x', turn_span_id: 'X1' })])
    const name = agentRow(1).querySelector<HTMLElement>('.trajectory-wf-name')!
    expect(name.textContent).toBe('Fix the parser')
    expect(name.title).toBe('Claude Code \u00b7 Fix the parser')
    expect(agentRow(1).querySelector('.trajectory-wf-sub')).toBeNull()
  })

  it('gives an open worker a second line: its runner, the turn that sent it, and how many turns it took', async () => {
    await draw([
      E('m0', 0),
      E('m1', 5, { turn_span_id: 'T2' }),
      door('d', 'x', { agent: 'Claude Code', label: 'Fix the parser' }),
      E('x1', 6, { subagent_scope: 'x', turn_span_id: 'X1' }),
      E('x2', 8, { subagent_scope: 'x', turn_span_id: 'X2' }),
    ].map((entry) => (entry.entry_id === 'd' ? { ...entry, turn_span_id: 'T2', operation_start: E('m1', 5).operation_start } : entry)))
    act(() => { fireEvent.click(agentRow(1)) })
    expect(agentRow(1).querySelector('.trajectory-wf-agentsub')?.textContent)
      .toBe('Claude Code \u00b7 gui.trajectory.agent.from_turn {"n":2} \u00b7 gui.trajectory.agent.turns_n {"n":2}')
  })

  it('tags a worker that ran inside the process as a sub-agent, and an external one not', async () => {
    await draw([
      door('d1', 'x', { label: 'Look it up' }), E('x1', 1, { subagent_scope: 'x', turn_span_id: 'X1' }),
      door('d2', 'y', { agent: 'Codex', label: 'Review it', instance: 'codex-1' }),
      E('y1', 2, { subagent_scope: 'y', turn_span_id: 'Y1', span_name: 'subagent.external', kind: 'subagent.external' }),
    ])
    expect([agentRow(1), agentRow(2)].map((row) => row.querySelector('.trajectory-wf-tag')?.textContent ?? null))
      .toEqual(['gui.trajectory.tag.sub', null])
  })

  it('keeps the main agent’s name and its model on one line', async () => {
    await draw([E('m', 0, { meta: { model: 'deepseek/deepseek-flash' } })])
    expect(agentRow(0).querySelector('.trajectory-wf-name')?.textContent).toBe('gui.trajectory.scope.main')
    expect(agentRow(0).querySelector('.trajectory-wf-sub')?.textContent).toBe('deepseek/deepseek-flash')
    expect(agentRow(0).querySelector('.trajectory-wf-agentsub')).toBeNull()
  })
})
