// @vitest-environment happy-dom
/* Two vertical rules, and only two: the instant a worker was dispatched, and
 * the instant its report opened a turn. Everything else about the relationship
 * is carried by position. */

import { expect, it } from 'vitest'

import { crossLinks } from './links'
import { buildWaterfall } from './model'
import { displayRows } from './Waterfall'

import type { TrajectoryEntry } from '../types'

const E = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output',
  trace_id: 't', span_id: id, parent_span_id: null, turn_span_id: 'T1', turn_number: 1, turn_start: false,
  origin: 'main', subagent_scope: null, opens_subagent_scope: null, dispatched_by_subagent_scope: null,
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-10-28T10:00:00.000Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [],
  operation_start: new Date(Date.parse('2026-10-28T10:00:00.000Z') + at * 1000).toISOString(),
  operation_end: new Date(Date.parse('2026-10-28T10:00:00.000Z') + (at + 5) * 1000).toISOString(),
  duration_ms: 5000, charged_ms: 5000, timing_basis: 'span_full', duration_owner: id, meta: {}, ...over,
})

const NOW = Date.parse('2026-10-28T10:10:00.000Z')

it('a dispatch runs from the spawning row down to the worker’s agent row', () => {
  const w = buildWaterfall([
    E('spawn', 10, { kind: 'tool.output', opens_subagent_scope: 't:sub' }),
    E('s1', 12, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ], NOW)
  const rows = displayRows(w, new Set(), new Set(['0:T1']))
  const links = crossLinks(rows, w)
  const dispatch = links.filter((link) => link.kind === 'dispatch')
  expect(dispatch).toHaveLength(1)
  expect(dispatch[0]!.at).toBe(Date.parse('2026-10-28T10:00:10.000Z'))
  expect(rows[dispatch[0]!.fromRow]!.row!.entry.entry_id).toBe('spawn')
  expect(rows[dispatch[0]!.toRow]!.kind).toBe('agent')
  expect(rows[dispatch[0]!.toRow]!.agent).toBe(1)
})

it('a folded spawn anchors the dispatch rule at its turn row instead', () => {
  const w = buildWaterfall([
    E('spawn', 10, { kind: 'tool.output', opens_subagent_scope: 't:sub' }),
    E('s1', 12, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ], NOW)
  const rows = displayRows(w, new Set(), new Set())
  const dispatch = crossLinks(rows, w).filter((link) => link.kind === 'dispatch')
  expect(dispatch).toHaveLength(1)
  expect(rows[dispatch[0]!.fromRow]!.kind).toBe('turn')
  expect(rows[dispatch[0]!.fromRow]!.turn).toBe('T1')
})

it('a report runs from the worker’s agent row up to the turn it started', () => {
  const w = buildWaterfall([
    E('m1', 0, { turn_span_id: 'T2', turn_start: true, dispatched_by_subagent_scope: 't:sub' }),
    E('s1', 1, { subagent_scope: 't:sub', turn_span_id: 'S1' }),
  ], NOW)
  const rows = displayRows(w, new Set(), new Set())
  const reports = crossLinks(rows, w).filter((link) => link.kind === 'report')
  expect(reports).toHaveLength(1)
  expect(rows[reports[0]!.toRow]!.kind).toBe('turn')
  expect(rows[reports[0]!.toRow]!.turn).toBe('T2')
  expect(reports[0]!.at).toBe(Date.parse('2026-10-28T10:00:06.000Z'))
})

it('emits nothing when the worker has no rows of its own', () => {
  const w = buildWaterfall([E('spawn', 10, { kind: 'tool.output', opens_subagent_scope: 't:sub' })], NOW)
  expect(crossLinks(displayRows(w, new Set(), new Set()), w)).toEqual([])
})
