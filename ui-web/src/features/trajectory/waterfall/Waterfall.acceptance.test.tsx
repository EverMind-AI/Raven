// @vitest-environment happy-dom
/* The design's acceptance list, one test per line. These are the properties a
 * reader would notice first if they broke, so they are stated here rather than
 * left implied by the unit tests beside them. */

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../../i18n/t'
import { absorb, resetCapabilities } from '../../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../../state/session/conversation'
import { resetSources, setSources } from '../../../state/sources'
import * as details from '../detailStore'
import * as store from '../store'
import { barOf, createAxis, IDLE_MS } from './axis'
import { buildWaterfall } from './model'
import { Waterfall } from './Waterfall'

import type { TrajectoryEntry, TrajectoryListResult, TrajectorySource } from '../types'

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

/* A host turn of 20.8s that dispatches a worker which runs for 133.2s. */
const HOST_AND_WORKER: TrajectoryEntry[] = [
  E('host', { turn_span_id: 'T1', operation_start: at(0), operation_end: at(20.8) }),
  E('end', { kind: 'turn.end', slot: 'turn.output', turn_span_id: 'T1', operation_start: at(20.7), operation_end: at(20.8) }),
  E('spawn', { kind: 'tool.output', turn_span_id: 'T1', opens_subagent_scope: 't:sub', operation_start: at(15.7), operation_end: at(149.2) }),
  E('w1', { subagent_scope: 't:sub', turn_span_id: 'S1', operation_start: at(19), operation_end: at(111) }),
  E('w2', { subagent_scope: 't:sub', turn_span_id: 'S2', operation_start: at(111), operation_end: at(149.1) }),
]

it('a sub-agent outliving its turn draws past the turn end and the idle break', () => {
  const w = buildWaterfall(HOST_AND_WORKER, NOW)
  const host = w.agents[0]!
  const worker = w.agents[1]!
  expect(host.turns[0]!.end).toBe(Date.parse(at(20.8)))
  expect(worker.turns.at(-1)!.end).toBeGreaterThan(host.turns[0]!.end)
  /* The worker's tail is 20 minutes after the host turn; the axis collapses that
   * stretch, and the worker's bar still reaches its end across the break. */
  const axis = createAxis([
    { start: host.start, end: host.end },
    { start: worker.start, end: worker.end },
  ])
  expect(axis.gaps).toHaveLength(0)
  const bar = barOf(axis, { from: 0, to: axis.length }, worker.start, worker.end)!
  expect(bar.left).toBeGreaterThan(0)
  /* The axis ends on the spawn row's own end, a tenth of a second past the worker's. */
  expect(bar.left + bar.width).toBeGreaterThan(0.999)
})

it('the turn a report started names the agent that started it', () => {
  const w = buildWaterfall([
    E('m1', { turn_span_id: 'T2', turn_start: true, dispatched_by_subagent_scope: 't:sub', operation_start: at(150), operation_end: at(158) }),
    E('s1', { subagent_scope: 't:sub', turn_span_id: 'S1', operation_start: at(19), operation_end: at(149.1) }),
  ], NOW)
  expect(w.agents[0]!.turns[0]!.reportedFrom).toBe('t:sub')
})

it('an idle break is labelled and is not compressed while any agent runs', () => {
  const lone = createAxis([{ start: 0, end: 1000 }, { start: 1000 + IDLE_MS * 5, end: 1000 + IDLE_MS * 5 + 1000 }])
  expect(lone.gaps).toHaveLength(1)
  expect(lone.gaps[0]!.end - lone.gaps[0]!.start).toBe(IDLE_MS * 5)
  /* The same stretch, covered by another agent's work, is occupied time. */
  const shared = createAxis([
    { start: 0, end: 1000 },
    { start: 1000 + IDLE_MS * 5, end: 1000 + IDLE_MS * 5 + 1000 },
    { start: 1000, end: 1000 + IDLE_MS * 5 },
  ])
  expect(shared.gaps).toHaveLength(0)
})

it('turn ordinals are contiguous within each agent', () => {
  const w = buildWaterfall([
    E('a', { turn_span_id: 'T1', turn_number: 1 }),
    E('b', { turn_span_id: 'T2', turn_number: 4 }),
    E('c', { subagent_scope: 't:sub', turn_span_id: 'S1', turn_number: 2 }),
  ], NOW)
  expect(w.agents.map((agent) => agent.turns.map((turn) => turn.ordinal))).toEqual([[1, 2], [1]])
})

it('work outside a turn appears under its agent', () => {
  const w = buildWaterfall([
    E('turned', { turn_span_id: 'T1' }),
    E('background', { turn_span_id: null, operation_start: at(30), operation_end: at(51) }),
  ], NOW)
  expect(w.agents[0]!.loose.map((row) => row.entry.entry_id)).toEqual(['background'])
})

it('the details pane opens the same blocks for a selected entry', () => {
  store._resetForTests()
  details._resetForTests()
  store.set({ ...store.get(), index: { e1: 0 }, entries: [E('e1')] })
  store.selectAcross('e1', { source: 'link' })
  details.openDetails()
  expect(store.get().selectedId).toBe('e1')
  expect(details.get().open).toBe(true)
})

/* ── the rendered page ─────────────────────────────────────────────── */

let rows: TrajectoryEntry[] = []

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async (): Promise<TrajectoryListResult> => ({
    epoch: 'e1', snapshot_revision: 100, entries: rows, next_cursor: null, complete: true,
    index_state: {
      phase: 'ready', scanned_bytes: 1, total_bytes: 1, head_truncated: 0, recovering_traces: 0,
      unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
    },
  }),
  changes: async (_k, epoch, after) => ({
    epoch, from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
    reset_required: false,
    index_state: {
      phase: 'ready', scanned_bytes: 1, total_bytes: 1, head_truncated: 0, recovering_traces: 0,
      unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
    },
  }),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
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

it('a 500-turn session mounts a bounded number of rows', async () => {
  rows = Array.from({ length: 500 }, (_, index) => E(`r${index}`, { turn_span_id: `T${index}`, sort_key: [String(index).padStart(6, '0'), 0] }))
  render(<Waterfall />)
  await act(async () => { await store.load() })
  expect(store.get().entries).toHaveLength(500)
  expect(document.querySelectorAll('[data-row]').length).toBeLessThan(60)
})
