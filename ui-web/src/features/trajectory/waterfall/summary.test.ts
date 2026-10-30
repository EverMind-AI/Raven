/* The header's figures, worked out from the same tree the rows are drawn from.
 * Each figure is pinned against a session small enough to count by hand. */

import { describe, expect, it } from 'vitest'

import { buildWaterfall } from './model'
import { summarize } from './summary'

import type { TrajectoryEntry } from '../types'

const T0 = Date.parse('2026-10-28T10:00:00.000Z')

const E = (id: string, from: number, to: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
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
  sort_key: [String(from).padStart(6, '0'), 0],
  event_time: new Date(T0 + from * 1000).toISOString(),
  preview: id,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: new Date(T0 + from * 1000).toISOString(),
  operation_end: new Date(T0 + to * 1000).toISOString(),
  duration_ms: (to - from) * 1000,
  charged_ms: (to - from) * 1000,
  timing_basis: 'span_full',
  duration_owner: id,
  meta: {},
  ...over,
})

const tool = (id: string, from: number, to: number, name: string, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry =>
  E(id, from, to, { kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', meta: { tool: name }, ...over })

const figuresOf = (entries: TrajectoryEntry[]) => {
  const now = T0 + 86_400_000
  return summarize(buildWaterfall(entries, now), entries, now)
}

describe('the header figures', () => {
  it('spans the session from its first start to its last end', () => {
    const figures = figuresOf([E('a', 0, 10), E('b', 50, 70, { turn_span_id: 'T2' })])
    expect([figures.start, figures.end]).toEqual([T0, T0 + 70_000])
  })

  it('counts the main agent apart from its sub-agents, an external agent’s run among them', () => {
    const figures = figuresOf([
      E('m', 0, 100),
      E('door-x', 1, 2, { opens_subagent_scope: 'x' }),
      E('door-y', 3, 4, { opens_subagent_scope: 'y' }),
      E('x1', 5, 9, { subagent_scope: 'x', span_name: 'subagent.external', kind: 'subagent.external', meta: { agent: 'Claude Code' } }),
      E('y1', 5, 9, { subagent_scope: 'y' }),
    ])
    expect(figures.agents).toEqual({ main: 1, sub: 2 })
  })

  it('takes the median turn length, the middle pair averaged', () => {
    const figures = figuresOf([
      E('a', 0, 10), E('b', 20, 40, { turn_span_id: 'T2' }), E('c', 50, 90, { turn_span_id: 'T3' }),
      E('d', 100, 180, { turn_span_id: 'T4' }),
    ])
    expect(figures.turns).toBe(4)
    expect(figures.medianTurn).toBe(30_000)
  })

  it('gives the model calls their share of the turns’ time', () => {
    /* One 100 s turn: 30 s of model, 50 s of tool, 20 s nobody claimed. */
    const figures = figuresOf([E('llm', 0, 30), tool('run', 30, 80, 'bash'), E('end', 100, 100, { kind: 'turn.output', slot: 'turn.output', span_name: 'session.turn' })])
    expect(figures.llm).toBe(1)
    expect(figures.modelShare).toBeCloseTo(0.3, 5)
  })

  it('names the two tools called most, by their share of the tool calls', () => {
    const figures = figuresOf([
      tool('b1', 0, 1, 'bash'), tool('b2', 2, 3, 'bash'), tool('b3', 4, 5, 'bash'),
      tool('e1', 6, 7, 'edit_file'), tool('r1', 8, 9, 'read_file'), tool('r2', 10, 11, 'read_file'),
    ])
    expect(figures.tool).toBe(6)
    expect(figures.topTools).toEqual([{ name: 'bash', share: 0.5 }, { name: 'read_file', share: 2 / 6 }])
  })

  it('counts a tool call once whatever number of entries it wrote', () => {
    const figures = figuresOf([
      tool('in', 0, 1, 'bash', { kind: 'tool.input', slot: 'tool.input', span_id: 'call' }),
      tool('out', 0, 1, 'bash', { span_id: 'call' }),
    ])
    expect(figures.tool).toBe(1)
    expect(figures.topTools).toEqual([{ name: 'bash', share: 1 }])
  })

  it('finds the earliest failure, not the first one listed', () => {
    const figures = figuresOf([
      E('late', 50, 60, { failure_entry: true, operation_status: 'error', turn_span_id: 'T2' }),
      E('early', 10, 20, { failure_entry: true, operation_status: 'error' }),
    ])
    expect(figures.errors).toBe(2)
    expect(figures.firstError).toBe('early')
  })

  it('has no share and no median for a session with no time in it', () => {
    const figures = figuresOf([])
    expect([figures.modelShare, figures.medianTurn, figures.firstError]).toEqual([null, null, null])
  })
})
