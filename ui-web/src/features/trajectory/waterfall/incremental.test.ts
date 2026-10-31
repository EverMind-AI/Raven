// @vitest-environment happy-dom
/* The incremental tree must equal the one-shot tree. Everything else about it
 * is a perf argument; this is the correctness one. */

import { expect, it } from 'vitest'

import { WaterfallIndex } from './incremental'
import { buildWaterfall } from './model'

import type { TrajectoryEntry } from '../types'

const T0 = Date.parse('2026-10-28T10:00:00.000Z')
const at = (seconds: number): string => new Date(T0 + seconds * 1000).toISOString()
const NOW = T0 + 20 * 60_000

const E = (id: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output',
  trace_id: 't', span_id: id, parent_span_id: null, turn_span_id: 'T1', turn_number: 1, turn_start: false,
  origin: 'main', subagent_scope: null, opens_subagent_scope: null, dispatched_by_subagent_scope: null,
  sort_key: [id, 0], event_time: at(0), preview: id, operation_status: 'ok', status_evidence: [],
  failure_entry: false, integrity: [], operation_start: at(0), operation_end: at(1),
  duration_ms: 1000, charged_ms: 1000, timing_basis: 'span_full', duration_owner: id, meta: {}, ...over,
})

const ROWS: TrajectoryEntry[] = [
  E('a', { operation_start: at(0), operation_end: at(5) }),
  E('b', { parent_span_id: 'a', operation_start: at(1), operation_end: at(2) }),
  E('spawn', { kind: 'tool.output', opens_subagent_scope: 't:sub', operation_start: at(3), operation_end: at(4) }),
  E('c', { turn_span_id: 'T2', operation_start: at(6), operation_end: at(9) }),
  E('bg', { turn_span_id: null, operation_start: at(10), operation_end: at(12) }),
  E('s1', { subagent_scope: 't:sub', turn_span_id: 'S1', operation_start: at(3), operation_end: at(30) }),
]

const build = (pages: TrajectoryEntry[][]): ReturnType<WaterfallIndex['waterfall']> => {
  const index = new WaterfallIndex(NOW)
  for (const page of pages) index.add(page)
  return index.waterfall()
}

it('equals the one-shot tree when the session arrives in one page', () => {
  expect(build([ROWS])).toEqual(buildWaterfall(ROWS, NOW))
})

it('equals the one-shot tree when the pages split a turn, an agent and the loose rows', () => {
  expect(build([ROWS.slice(0, 1), ROWS.slice(1, 3), ROWS.slice(3)])).toEqual(buildWaterfall(ROWS, NOW))
})

it('equals the one-shot tree when a child arrives before its parent', () => {
  const reordered = [ROWS[1]!, ROWS[0]!, ...ROWS.slice(2)]
  expect(build([reordered.slice(0, 2), reordered.slice(2)])).toEqual(buildWaterfall(ROWS, NOW))
})

it('re-cuts a running row when the clock moves, and only that row’s turn', () => {
  const running = E('live', { operation_end: null, operation_start: at(1) })
  const index = new WaterfallIndex(T0 + 10_000)
  index.add([running])
  const first = JSON.stringify(index.waterfall())
  index.setNow(T0 + 90_000)
  expect(JSON.stringify(index.waterfall())).not.toBe(first)
  expect(index.waterfall().agents[0]!.turns[0]!.end).toBe(T0 + 90_000)
})

it('re-cuts a running row that carries its snapshot’s end when the clock moves', () => {
  /* A running span's record carries the time it was last written as its end;
     while that is fresh the row runs to the present, so the clock moving must
     move it here exactly as it moves it in the one-shot tree. */
  const running = E('live', { operation_status: 'running', operation_start: at(1), operation_end: at(5) })
  const index = new WaterfallIndex(T0 + 10_000)
  index.add([running])
  index.waterfall()
  index.setNow(T0 + 90_000)
  expect(index.waterfall()).toEqual(buildWaterfall([running], T0 + 90_000))
})

it('keeps overlapping runs of one named agent apart across pages, as the one-shot tree does', () => {
  const run = (id: string, from: number, to: number): TrajectoryEntry[] => [
    E(`d-${id}`, { kind: 'subagent.run', opens_subagent_scope: `t:${id}`, meta: { agent: 'Raven-General' }, operation_start: at(from), operation_end: at(to) }),
    E(`w-${id}`, { subagent_scope: `t:${id}`, turn_span_id: `W-${id}`, operation_start: at(from + 1), operation_end: at(to - 1) }),
  ]
  const pages = [run('a', 0, 10), run('b', 5, 15), run('c', 20, 30)]
  const grown = build(pages)
  expect(grown).toEqual(buildWaterfall(pages.flat(), NOW))
  expect(grown.agents.slice(1).map((agent) => agent.scopes)).toEqual([['t:a', 't:c'], ['t:b']])
})

it('folds two pages of runs of one instance into the same agent', () => {
  const door = (id: string, scope: string, at2: number): TrajectoryEntry => E(id, {
    kind: 'tool.output', opens_subagent_scope: scope, meta: { instance: 'raven-design-1' },
    operation_start: at(at2), operation_end: at(at2 + 1),
  })
  const work = (id: string, scope: string, at2: number): TrajectoryEntry => E(id, {
    subagent_scope: scope, turn_span_id: `${scope}-turn`, operation_start: at(at2), operation_end: at(at2 + 1),
  })
  const first = [door('d1', 't:a', 0), work('a1', 't:a', 1)]
  const second = [door('d2', 't:b', 4), work('b1', 't:b', 5)]
  const index = new WaterfallIndex(NOW)
  index.add(first)
  index.add(second)
  const grown = index.waterfall()
  expect(grown).toEqual(buildWaterfall([...first, ...second], NOW))
  expect(grown.agents[1]!.scopes).toEqual(['t:a', 't:b'])
  expect(grown.agents[1]!.turns.map((turn) => turn.ordinal)).toEqual([1, 2])
})
