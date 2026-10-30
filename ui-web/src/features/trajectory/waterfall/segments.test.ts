/* A turn's bar, as the activity inside it: model, tool, sub-agent, failure and
 * the waiting between them, swept once so overlapping children resolve the
 * same way every time. */

import { expect, it } from 'vitest'

import { segmentOf, turnSegments } from './segments'

import type { TrajectoryEntry } from '../types'
import type { WaterfallRow, WaterfallTurn } from './model'

const E = (over: Partial<TrajectoryEntry>): TrajectoryEntry => ({
  entry_id: 'e',
  revision: 1,
  kind: 'llm.output',
  span_name: 'llm.call',
  slot: 'llm.output',
  trace_id: 't',
  span_id: 's',
  parent_span_id: null,
  turn_span_id: 'T',
  turn_number: 1,
  turn_start: false,
  origin: 'main',
  subagent_scope: null,
  opens_subagent_scope: null,
  dispatched_by_subagent_scope: null,
  sort_key: [],
  event_time: '2026-10-28T10:00:00.000Z',
  preview: null,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: null,
  operation_end: null,
  duration_ms: null,
  charged_ms: null,
  timing_basis: 'unknown',
  duration_owner: null,
  meta: {},
  ...over,
})

const at = (seconds: number): string => new Date(Date.parse('2026-10-28T10:00:00.000Z') + seconds * 1000).toISOString()

const row = (over: Partial<TrajectoryEntry>): WaterfallRow => ({ entry: E(over) })

const turn = (rows: WaterfallRow[], start: number, end: number): WaterfallTurn => ({
  id: 'T', ordinal: 1, preview: null, start, end, reportedFrom: null, failure: false, rows,
})

const T0 = Date.parse('2026-10-28T10:00:00.000Z')
const sec = (n: number): number => T0 + n * 1000

it('classifies a row by what it is, a spawn as a sub-agent and an error as a failure', () => {
  expect(segmentOf(row({ kind: 'llm.output' }))).toBe('model')
  expect(segmentOf(row({ kind: 'tool.output' }))).toBe('tool')
  expect(segmentOf(row({ kind: 'subagent.run', opens_subagent_scope: 't:sub' }))).toBe('subagent')
  expect(segmentOf(row({ kind: 'llm.output', failure_entry: true }))).toBe('failure')
})

it('a turn with model and tool rows paints both and fills the rest with waiting', () => {
  const bar = turnSegments(turn([
    row({ entry_id: 'm', kind: 'llm.output', operation_start: at(0), operation_end: at(1) }),
    row({ entry_id: 't', kind: 'tool.output', operation_start: at(2), operation_end: at(2.4) }),
  ], sec(0), sec(4)), sec(4))
  expect(bar).toEqual([
    { start: sec(0), end: sec(1), kind: 'model' },
    { start: sec(1), end: sec(2), kind: 'waiting' },
    { start: sec(2), end: sec(2.4), kind: 'tool' },
    { start: sec(2.4), end: sec(4), kind: 'waiting' },
  ])
})

it('overlapping sub-agent and model rows give the span to the sub-agent', () => {
  const bar = turnSegments(turn([
    row({ entry_id: 'm', kind: 'llm.output', operation_start: at(0), operation_end: at(3) }),
    row({ entry_id: 's', kind: 'subagent.run', opens_subagent_scope: 't:sub', operation_start: at(1), operation_end: at(2) }),
  ], sec(0), sec(3)), sec(3))
  expect(bar.find((part) => part.start === sec(1))!.kind).toBe('subagent')
})

it('a failed row wins over everything it overlaps', () => {
  const bar = turnSegments(turn([
    row({ entry_id: 'm', kind: 'llm.output', operation_start: at(0), operation_end: at(3) }),
    row({ entry_id: 'f', kind: 'tool.output', failure_entry: true, operation_start: at(1.5), operation_end: at(1.6) }),
  ], sec(0), sec(3)), sec(3))
  expect(bar.find((part) => part.start === sec(1.5))!.kind).toBe('failure')
})

it('rows are clipped to the turn window', () => {
  const bar = turnSegments(turn([
    row({ entry_id: 's', kind: 'subagent.run', opens_subagent_scope: 't:sub', operation_start: at(0.5), operation_end: at(9) }),
  ], sec(0), sec(2)), sec(9))
  expect(bar.at(-1)!.end).toBe(sec(2))
})

it('a turn with no timed rows is one waiting segment', () => {
  expect(turnSegments(turn([], sec(0), sec(5)), sec(5))).toEqual([{ start: sec(0), end: sec(5), kind: 'waiting' }])
})

it('hands back the same bar for the same turn and clock, so a scroll frame computes nothing', () => {
  const t = turn([row({ entry_id: 'm', kind: 'llm.output', operation_start: at(0), operation_end: at(1) })], sec(0), sec(2))
  const first = turnSegments(t, sec(2))
  expect(turnSegments(t, sec(2))).toBe(first)
  expect(turnSegments(t, sec(3))).not.toBe(first)
})

it('paints every stretch the way a stretch-by-stretch reading of the rows would', () => {
  /* The reading the sweep must agree with: at each instant, the
     highest-priority kind of the rows open there, else waiting. */
  const kinds = ['llm.output', 'tool.output', 'subagent.run', 'failure'] as const
  let seed = 7
  const next = (): number => (seed = (seed * 16807) % 2147483647) / 2147483647
  for (let round = 0; round < 40; round++) {
    const rows = Array.from({ length: 1 + Math.floor(next() * 12) }, (_, i) => {
      const kind = kinds[Math.floor(next() * kinds.length)]!
      const from = Math.floor(next() * 20)
      const to = from + Math.floor(next() * 6)
      return row({
        entry_id: `r${i}`,
        kind: kind === 'failure' ? 'tool.output' : kind,
        failure_entry: kind === 'failure',
        opens_subagent_scope: kind === 'subagent.run' ? 't:sub' : null,
        operation_start: at(from),
        operation_end: at(to),
      })
    })
    const t = turn(rows, sec(0), sec(24))
    const bar = turnSegments(t, sec(24))
    for (let s = 0; s < 24; s++) {
      const open = rows.filter((r) => Date.parse(r.entry.operation_start!) <= sec(s) && Date.parse(r.entry.operation_end!) > sec(s))
      const want = (['failure', 'subagent', 'model', 'tool'] as const).find((k) => open.some((r) => segmentOf(r) === k)) ?? 'waiting'
      const got = bar.find((part) => part.start <= sec(s) && part.end > sec(s))?.kind
      expect(got, `round ${round}, second ${s}`).toBe(want)
    }
  }
})
