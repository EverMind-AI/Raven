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
    expect(flat.length * ROW_HEIGHT).toBeLessThan(20000)
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

  it('marks a turn a worker’s report started', async () => {
    await draw([E('m1', 0, { turn_start: true, dispatched_by_subagent_scope: 't:sub' })])
    expect(document.querySelector('[data-from]')).not.toBeNull()
  })
})
