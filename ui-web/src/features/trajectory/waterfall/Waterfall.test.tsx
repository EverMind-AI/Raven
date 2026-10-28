// @vitest-environment happy-dom
/* The waterfall draws what the model returns: one row per agent, one per turn,
 * and a turn's spans only when it is open. The arithmetic behind the rows is
 * tested in model.test.ts; what is tested here is that the page uses it. */

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { absorb, resetCapabilities } from '../../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../../state/session/conversation'
import { resetSources, setSources } from '../../../state/sources'
import * as details from '../detailStore'
import * as store from '../store'
import { buildWaterfall } from './model'
import { ROW_HEIGHT, Waterfall, displayRows } from './Waterfall'

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

/* happy-dom drops the modifier keys out of WheelEventInit, so they are written
   onto the event after construction; a browser sends them itself. */
const wheel = (init: WheelEventInit & { ctrlKey?: boolean; shiftKey?: boolean }): WheelEvent => {
  const event = new WheelEvent('wheel', init)
  if (init.ctrlKey) Object.defineProperty(event, 'ctrlKey', { value: true })
  if (init.shiftKey) Object.defineProperty(event, 'shiftKey', { value: true })
  return event
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
    expect(displayRows(w, new Set(['']), new Set(['T1'])).map((row) => row.kind)).toEqual(['agent'])
  })
})

describe('the page', () => {
  it('draws one agent row per agent and one turn row per turn', async () => {
    await draw([
      E('m1', 0),
      E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
    ])
    expect(count('[data-row="agent"]')).toBe(2)
    expect(count('[data-row="turn"]')).toBe(2)
  })

  it('reveals a turn’s spans when its row is clicked', async () => {
    await draw([E('m1', 0), E('m2', 1)])
    expect(count('[data-row="span"]')).toBe(0)
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(count('[data-row="span"]')).toBe(2)
  })

  it('selects a span across scopes and opens the details pane', async () => {
    await draw([E('m1', 0), E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' })])
    fireEvent.click(document.querySelectorAll('[data-row="turn"]')[1]!)
    const span = [...document.querySelectorAll<HTMLElement>('[data-row="span"]')].find((el) => el.dataset.entry === 's1')!
    fireEvent.click(span)
    expect(store.get().selectedId).toBe('s1')
    expect(store.get().scope).toBe(null)
    expect(details.get().open).toBe(true)
  })

  it('shows how long a turn and a span took', async () => {
    await draw([E('m1', 0, { operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:02.500Z' })])
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
  it('reveals the entries the index hides when the switch is turned on', async () => {
    await draw([E('shown', 0), E('folded', 1, { meta: { hidden: 'redundant_reply' } })])
    expect(document.querySelector('[data-entry="folded"]')).toBeNull()
    fireEvent.click(document.querySelector('[data-testid="show-hidden"]')!)
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(document.querySelector('[data-entry="folded"]')).not.toBeNull()
  })

  it('labels an idle break where it compresses one', async () => {
    const base = Date.parse('2026-10-28T10:00:00.000Z')
    const at2 = (seconds: number): string => new Date(base + seconds * 1000).toISOString()
    await draw([
      E('a', 0, { operation_start: at2(0), operation_end: at2(20) }),
      E('b', 1, { turn_span_id: 'T2', operation_start: at2(900), operation_end: at2(920) }),
    ])
    const idle = document.querySelector('.trajectory-ruler-idle')
    expect(idle).not.toBeNull()
    expect(idle!.textContent).toMatch(/\d/)
  })

  it('moves the selection with the arrow keys and opens a turn with Enter', async () => {
    await draw([E('m1', 0), E('m2', 1, { turn_span_id: 'T2' })])
    const box = document.querySelector('.trajectory-list') as HTMLElement
    act(() => { box.focus() })
    act(() => { fireEvent.keyDown(box, { key: 'ArrowDown' }) })
    expect(store.get().selectedId).not.toBeNull()
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    expect(document.querySelector('[data-row="turn"][data-open]')).not.toBeNull()
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

  it('puts a span’s icon in its own boxed cell', async () => {
    await draw([E('m1', 0), E('m2', 1)])
    fireEvent.click(document.querySelector('[data-row="turn"]')!)
    expect(document.querySelector('[data-entry="m1"] .trajectory-wf-ico')).not.toBeNull()
  })
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
