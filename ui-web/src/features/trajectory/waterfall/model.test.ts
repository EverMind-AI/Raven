/* Agents, their turns, and the rows inside them: the shape the waterfall
 * draws is the shape this module returns, so every rule about who owns a row
 * is a property stated here rather than a rule found in a component. */

import { expect, it } from 'vitest'

import { buildWaterfall, busyRuns, entryEnd, isLive } from './model'

import type { TrajectoryEntry } from '../types'

const E = (over: Partial<TrajectoryEntry>): TrajectoryEntry => ({
  entry_id: 'e',
  revision: 1,
  kind: 'llm.output',
  span_name: 'llm.call',
  slot: 'llm.output',
  trace_id: 't',
  span_id: 's',
  parent_span_id: null,
  turn_span_id: null,
  turn_number: null,
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

const NOW = Date.parse('2026-10-28T10:30:00.000Z')
const T0 = Date.parse('2026-10-28T10:00:00.000Z')
const at = (seconds: number): string => new Date(T0 + seconds * 1000).toISOString()

it('turns are numbered per agent, not by the wire turn_number', () => {
  const w = buildWaterfall([
    E({ entry_id: 'm1', span_id: 'a', turn_span_id: 'T1', turn_number: 1, operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:10.000Z' }),
    E({ entry_id: 'c1', span_id: 'b', subagent_scope: 't:sub', turn_span_id: 'ST1', turn_number: 2, operation_start: '2026-10-28T10:00:05.000Z', operation_end: '2026-10-28T10:01:00.000Z' }),
    E({ entry_id: 'm2', span_id: 'c', turn_span_id: 'T2', turn_number: 3, operation_start: '2026-10-28T10:01:00.000Z', operation_end: '2026-10-28T10:01:10.000Z' }),
  ], NOW)
  const main = w.agents[0]!
  expect(main.turns.map((turn) => turn.ordinal)).toEqual([1, 2])
  expect(w.agents[1]!.turns.map((turn) => turn.ordinal)).toEqual([1])
})

it('a turn ends where its own last row ends, not where a child outlives it', () => {
  const w = buildWaterfall([
    E({ entry_id: 'm1', span_id: 'a', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:20.800Z' }),
    E({ entry_id: 'spawn', span_id: 'b', kind: 'tool.output', span_name: 'subagent.run', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:16.000Z', operation_end: '2026-10-28T10:02:15.000Z' }),
    E({ entry_id: 'end', span_id: 'c', kind: 'turn.end', slot: 'turn.output', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:20.700Z', operation_end: '2026-10-28T10:00:20.800Z' }),
  ], NOW)
  expect(w.agents[0]!.turns[0]!.end).toBe(Date.parse('2026-10-28T10:00:20.800Z'))
})

it('an entry with no turn lands under its agent as a loose row', () => {
  const w = buildWaterfall([
    E({ entry_id: 'bg', span_id: 'z', turn_span_id: null, operation_start: '2026-10-28T10:01:00.000Z', operation_end: '2026-10-28T10:01:21.000Z' }),
  ], NOW)
  expect(w.agents[0]!.loose.map((row) => row.entry.entry_id)).toEqual(['bg'])
})

it('every row under a turn is a sibling, whatever parent_span_id says', () => {
  const w = buildWaterfall([
    E({ entry_id: 'root', span_id: 'r', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:10.000Z' }),
    E({ entry_id: 'mid', span_id: 'm', parent_span_id: 'r', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:01.000Z', operation_end: '2026-10-28T10:00:02.000Z' }),
    E({ entry_id: 'leaf', span_id: 'l', parent_span_id: 'm', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:01.100Z', operation_end: '2026-10-28T10:00:01.900Z' }),
  ], NOW)
  const rows = w.agents[0]!.turns[0]!.rows
  /* One flat list in time order: a row carries no level of its own to indent by. */
  expect(rows.map((row) => row.entry.entry_id)).toEqual(['root', 'mid', 'leaf'])
  expect(rows.every((row) => Object.keys(row).join() === 'entry')).toBe(true)
})

it('the host agent comes first and agents are ordered by first start', () => {
  const w = buildWaterfall([
    E({ entry_id: 'c', subagent_scope: 't:sub', span_id: 'b', operation_start: '2026-10-28T10:00:01.000Z', operation_end: '2026-10-28T10:00:02.000Z' }),
    E({ entry_id: 'm', span_id: 'a', operation_start: '2026-10-28T10:00:05.000Z', operation_end: '2026-10-28T10:00:06.000Z' }),
  ], NOW)
  expect(w.agents.map((agent) => agent.scope)).toEqual([null, 't:sub'])
})

it('a turn names the agent whose report started it', () => {
  const w = buildWaterfall([
    E({ entry_id: 'm1', span_id: 'a', turn_span_id: 'T2', turn_start: true, dispatched_by_subagent_scope: 't:sub', operation_start: '2026-10-28T10:05:00.000Z', operation_end: '2026-10-28T10:05:10.000Z' }),
  ], NOW)
  expect(w.agents[0]!.turns[0]!.reportedFrom).toBe('t:sub')
})

it('treats a span that never wrote an end as running only while it is recent', () => {
  const recent = E({ operation_start: new Date(NOW - 30_000).toISOString(), operation_end: null })
  const stale = E({ operation_start: new Date(NOW - 6 * 60 * 60_000).toISOString(), operation_end: null })
  expect(isLive(recent, NOW)).toBe(true)
  expect(isLive(stale, NOW)).toBe(false)
  expect(isLive(E({ operation_end: new Date(NOW).toISOString() }), NOW)).toBe(false)
})

it('treats a span the index reports running as running, though it carries a snapshot end', () => {
  const running = E({
    operation_status: 'running',
    operation_start: new Date(NOW - 60_000).toISOString(),
    operation_end: new Date(NOW - 50_000).toISOString(),
  })
  expect(isLive(running, NOW)).toBe(true)
  expect(entryEnd(running, NOW)).toBe(NOW)
  /* A run whose snapshot stopped arriving long ago is not kept ticking. */
  const abandoned = E({
    operation_status: 'running',
    operation_start: new Date(NOW - 5 * 60 * 60_000).toISOString(),
    operation_end: new Date(NOW - 4 * 60 * 60_000).toISOString(),
  })
  expect(isLive(abandoned, NOW)).toBe(false)
  expect(entryEnd(abandoned, NOW)).toBe(NOW - 4 * 60 * 60_000)
})

it('a turn previews the row that opened it, not whichever row started first', () => {
  const w = buildWaterfall([
    E({ entry_id: 'early', span_id: 'a', turn_span_id: 'T1', preview: 'early', operation_start: at(0), operation_end: at(5) }),
    E({ entry_id: 'open', span_id: 'b', turn_span_id: 'T1', turn_start: true, preview: 'the question', operation_start: at(1), operation_end: at(2) }),
  ], NOW)
  expect(w.agents[0]!.turns[0]!.preview).toBe('the question')
})

it('treats two runs of one instance as one agent, with one turn sequence', () => {
  const w = buildWaterfall([
    E({ entry_id: 'd1', span_id: 'd1', kind: 'tool.output', opens_subagent_scope: 't:a', meta: { instance: 'raven-design-1' }, operation_start: at(0), operation_end: at(1) }),
    E({ entry_id: 'a1', span_id: 'a1', subagent_scope: 't:a', turn_span_id: 'A1', operation_start: at(2), operation_end: at(3) }),
    E({ entry_id: 'd2', span_id: 'd2', kind: 'tool.output', opens_subagent_scope: 't:b', meta: { instance: 'raven-design-1' }, operation_start: at(4), operation_end: at(5) }),
    E({ entry_id: 'b1', span_id: 'b1', subagent_scope: 't:b', turn_span_id: 'B1', operation_start: at(6), operation_end: at(7) }),
  ], NOW)
  expect(w.agents).toHaveLength(2)
  const design = w.agents[1]!
  expect(design.key).toBe('agent:raven-design-1')
  expect(design.scopes).toEqual(['t:a', 't:b'])
  expect(design.turns.map((turn) => turn.ordinal)).toEqual([1, 2])
})

it('keeps two runs of one instance as two turns when both carry the turn that dispatched them', () => {
  const w = buildWaterfall([
    E({ entry_id: 'd1', span_id: 'd1', kind: 'tool.output', opens_subagent_scope: 't:a', meta: { instance: 'pr-fix' }, operation_start: at(0), operation_end: at(1) }),
    E({ entry_id: 'a1', span_id: 'a1', subagent_scope: 't:a', turn_span_id: 'T5', operation_start: at(2), operation_end: at(3) }),
    E({ entry_id: 'd2', span_id: 'd2', kind: 'tool.output', opens_subagent_scope: 't:b', meta: { instance: 'pr-fix' }, operation_start: at(4), operation_end: at(5) }),
    E({ entry_id: 'b1', span_id: 'b1', subagent_scope: 't:b', turn_span_id: 'T5', operation_start: at(6), operation_end: at(7) }),
  ], NOW)
  const fix = w.agents[1]!
  expect(fix.turns.map((turn) => turn.ordinal)).toEqual([1, 2])
  expect(new Set(fix.turns.map((turn) => turn.id)).size).toBe(2)
})

it('folds runs of one named agent together when no instance id reached the door', () => {
  const w = buildWaterfall([
    E({ entry_id: 'd1', span_id: 'd1', kind: 'tool.output', opens_subagent_scope: 't:a', meta: { agent: 'Claude Code-Mirasim' }, operation_start: at(0), operation_end: at(1) }),
    E({ entry_id: 'a1', span_id: 'a1', subagent_scope: 't:a', turn_span_id: 'A1', operation_start: at(2), operation_end: at(3) }),
    E({ entry_id: 'd2', span_id: 'd2', kind: 'tool.output', opens_subagent_scope: 't:b', meta: { agent: 'Claude Code-Mirasim' }, operation_start: at(4), operation_end: at(5) }),
    E({ entry_id: 'b1', span_id: 'b1', subagent_scope: 't:b', turn_span_id: 'B1', operation_start: at(6), operation_end: at(7) }),
  ], NOW)
  expect(w.agents).toHaveLength(2)
  expect(w.agents[1]!.scopes).toEqual(['t:a', 't:b'])
})

it('keeps runs of differently named agents apart', () => {
  const w = buildWaterfall([
    E({ entry_id: 'd1', span_id: 'd1', kind: 'tool.output', opens_subagent_scope: 't:a', meta: { agent: 'coder' }, operation_start: at(0), operation_end: at(1) }),
    E({ entry_id: 'a1', span_id: 'a1', subagent_scope: 't:a', turn_span_id: 'A1', operation_start: at(2), operation_end: at(3) }),
    E({ entry_id: 'd2', span_id: 'd2', kind: 'tool.output', opens_subagent_scope: 't:b', meta: { agent: 'writer' }, operation_start: at(4), operation_end: at(5) }),
    E({ entry_id: 'b1', span_id: 'b1', subagent_scope: 't:b', turn_span_id: 'B1', operation_start: at(6), operation_end: at(7) }),
  ], NOW)
  expect(w.agents).toHaveLength(3)
})

/* Three graph nodes on one preset, sent together: nothing names an instance,
   so only the name is shared -- and one agent is never in two runs at once. */
const parallel = (spans: Array<[string, number, number]>): TrajectoryEntry[] => spans.flatMap(([id, from, to]) => [
  E({ entry_id: `d-${id}`, span_id: `d-${id}`, kind: 'subagent.run', opens_subagent_scope: `t:${id}`, meta: { agent: 'Raven-General', label: id }, operation_start: at(from), operation_end: at(to) }),
  E({ entry_id: `w-${id}`, span_id: `w-${id}`, subagent_scope: `t:${id}`, turn_span_id: `W-${id}`, operation_start: at(from + 1), operation_end: at(to - 1) }),
])

it('keeps runs of one named agent apart while they overlap', () => {
  const w = buildWaterfall(parallel([['n1', 5, 65], ['n2', 5, 65], ['n3', 6, 60]]), NOW)
  expect(w.agents.map((agent) => agent.scopes)).toEqual([[null], ['t:n1'], ['t:n2'], ['t:n3']])
  expect(new Set(w.agents.map((agent) => agent.key)).size).toBe(4)
})

it('still folds a named agent’s later run into the lane it has finished in', () => {
  const w = buildWaterfall(parallel([['a', 0, 10], ['b', 5, 15], ['c', 20, 30]]), NOW)
  expect(w.agents.slice(1).map((agent) => agent.scopes)).toEqual([['t:a', 't:c'], ['t:b']])
})

it('keeps runs that name one instance together even when they overlap', () => {
  const runs = parallel([['a', 0, 10], ['b', 5, 15]]).map((entry) => (
    entry.opens_subagent_scope ? { ...entry, meta: { ...entry.meta, instance: 'raven-design-1' } } : entry
  ))
  expect(buildWaterfall(runs, NOW).agents.slice(1).map((agent) => agent.scopes)).toEqual([['t:a', 't:b']])
})

it('reads an agent’s busy time as its turns and loose rows merged, not its lifetime', () => {
  const T0 = Date.parse('2026-10-28T10:00:00.000Z')
  const at = (seconds: number): string => new Date(T0 + seconds * 1000).toISOString()
  const waterfall = buildWaterfall([
    E({ entry_id: 'a', span_id: 'a', turn_span_id: 'T1', operation_start: at(0), operation_end: at(3) }),
    E({ entry_id: 'b', span_id: 'b', turn_span_id: 'T2', operation_start: at(2), operation_end: at(5) }),
    E({ entry_id: 'c', span_id: 'c', turn_span_id: 'T3', operation_start: at(10), operation_end: at(11) }),
    E({ entry_id: 'd', span_id: 'd', turn_span_id: null, operation_start: at(20), operation_end: at(22) }),
  ], T0 + 60_000)
  const runs = busyRuns(waterfall.agents[0]!, T0 + 60_000)
  expect(runs).toEqual([
    { start: T0, end: T0 + 5000 }, { start: T0 + 10_000, end: T0 + 11_000 }, { start: T0 + 20_000, end: T0 + 22_000 },
  ])
})
