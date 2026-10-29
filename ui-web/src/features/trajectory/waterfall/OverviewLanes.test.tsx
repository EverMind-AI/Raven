// @vitest-environment happy-dom
/* The overview strip: one thin lane per agent, so a reader can see who was
 * busy across the whole session before reading any of it. */

import { fireEvent, render } from '@testing-library/react'
import { expect, it } from 'vitest'

import { createAxis } from './axis'
import { buildWaterfall } from './model'
import { MAX_LANES, OverviewLanes } from './OverviewLanes'

import type { TrajectoryEntry } from '../types'

const E = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output',
  trace_id: 't', span_id: id, parent_span_id: null, turn_span_id: 'T1', turn_number: 1, turn_start: false,
  origin: 'main', subagent_scope: null, opens_subagent_scope: null, dispatched_by_subagent_scope: null,
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-10-28T10:00:00.000Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [],
  operation_start: new Date(Date.parse('2026-10-28T10:00:00.000Z') + at * 1000).toISOString(),
  operation_end: new Date(Date.parse('2026-10-28T10:00:00.000Z') + (at + 30) * 1000).toISOString(),
  duration_ms: 30_000, charged_ms: 30_000, timing_basis: 'span_full', duration_owner: id, meta: {}, ...over,
})

it('draws one lane per agent, in the waterfall order', () => {
  const now = Date.parse('2026-10-28T10:05:00.000Z')
  const waterfall = buildWaterfall([
    E('m', 0),
    E('s', 10, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ], now)
  const axis = createAxis([{ start: now - 300_000, end: now }])
  const { container } = render(<OverviewLanes waterfall={waterfall} axis={axis} viewport={{ from: 0, to: axis.length }} />)
  const lanes = container.querySelectorAll('[data-lane]')
  expect(lanes).toHaveLength(2)
  expect([...lanes].map((lane) => lane.getAttribute('data-lane'))).toEqual(['main', 'scope:t:sub'])
})

it('places a stretch as a fraction of the viewport', () => {
  const now = Date.parse('2026-10-28T10:05:00.000Z')
  const waterfall = buildWaterfall([E('m', 0)], now)
  /* The axis is wider than the agent's own span, so the bar has room either side. */
  const axis = createAxis([{ start: now - 600_000, end: now }])
  const { container } = render(<OverviewLanes waterfall={waterfall} axis={axis} viewport={{ from: 0, to: axis.length }} />)
  const bar = container.querySelector<HTMLElement>('[data-lane] i')!
  const left = Number(bar.style.left.replace('%', ''))
  const width = Number(bar.style.width.replace('%', ''))
  expect(left).toBeGreaterThan(0)
  expect(left + width).toBeLessThanOrEqual(100.0001)
})

it('draws an agent whose work is all outside any turn', () => {
  const now = Date.parse('2026-10-28T10:05:00.000Z')
  const waterfall = buildWaterfall([
    E('bg', 0, { turn_span_id: null }),
  ], now)
  const axis = createAxis([{ start: now - 600_000, end: now }])
  const { container } = render(<OverviewLanes waterfall={waterfall} axis={axis} viewport={{ from: 0, to: axis.length }} />)
  expect(container.querySelectorAll('[data-lane] i').length).toBe(1)
})

it('keeps the strip to a handful of lanes and counts the rest', () => {
  const now = Date.parse('2026-10-28T10:05:00.000Z')
  const many = Array.from({ length: 30 }, (_, index) => E(`s${index}`, index, { subagent_scope: `t:s${index}`, turn_span_id: `S${index}` }))
  const waterfall = buildWaterfall([E('m', 0), ...many], now)
  const axis = createAxis([{ start: now - 60_000, end: now }])
  const { container } = render(<OverviewLanes waterfall={waterfall} axis={axis} viewport={{ from: 0, to: axis.length }} />)
  expect(container.querySelectorAll('[data-lane]').length).toBe(6)
  expect(container.querySelector('[data-lane-more]')).not.toBeNull()
  expect(container.textContent).toContain('25')
})

it('opens the rest of the roster from the count, and folds it again from the row it leaves', () => {
  const now = Date.parse('2026-10-28T10:05:00.000Z')
  const entries = [E('m', 0)]
  for (let i = 0; i < MAX_LANES + 3; i += 1) {
    entries.push(E(`s${i}`, 10 + i, { subagent_scope: `t:s${i}`, turn_span_id: `S${i}` }))
  }
  const waterfall = buildWaterfall(entries, now)
  const axis = createAxis([{ start: now - 300_000, end: now }])
  const { container } = render(<OverviewLanes waterfall={waterfall} axis={axis} viewport={{ from: 0, to: axis.length }} />)
  /* Past the shape the lane count is a button, not a caption. */
  expect(container.querySelectorAll('[data-lane]')).toHaveLength(MAX_LANES)
  const more = container.querySelector<HTMLElement>('[data-lane-more]')!
  expect(more.getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(more)
  expect(container.querySelectorAll('[data-lane]')).toHaveLength(MAX_LANES + 4)
  /* And the row it leaves is the way back. */
  const fewer = container.querySelector<HTMLElement>('[data-lane-fewer]')!
  expect(fewer.getAttribute('aria-expanded')).toBe('true')
  fireEvent.click(fewer)
  expect(container.querySelectorAll('[data-lane]')).toHaveLength(MAX_LANES)
  expect(container.querySelector('[data-lane-more]')).not.toBeNull()
})
