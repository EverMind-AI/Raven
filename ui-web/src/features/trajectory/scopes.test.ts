// @vitest-environment happy-dom
/* The reader's moves into a Sub-agent Scope: which scope a row leads to, and
   where a move into one lands -- its external agent's entry, open on the
   recorded block, else its first entry. */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as details from './detailStore'
import * as scopes from './scopes'
import * as list from './store'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult, TrajectorySource } from './types'

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const entry = (id: string, at: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
  span_id: id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
  sort_key: [String(at).padStart(6, '0'), 0], event_time: '2026-01-01T00:00:00Z', preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: null,
  operation_end: null, duration_ms: null, charged_ms: null, timing_basis: 'not_recorded', duration_owner: null,
  meta: {},
  ...over,
})

const RUN = 't:run'
const EXT = 't:ext'

/* A turn that spawned a run and an external agent, then the turn the run's report started. */
const ROWS: TrajectoryEntry[] = [
  entry('m0', 0, { turn_start: true, kind: 'user.input', slot: 'turn.input' }),
  entry('doorRun', 1, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: RUN }),
  entry('doorExt', 2, { kind: 'subagent.run', span_name: 'subagent.run', slot: 'subagent.run', opens_subagent_scope: EXT }),
  entry('r0', 3, { subagent_scope: RUN }),
  entry('r1', 4, { subagent_scope: RUN }),
  entry('e0', 5, { subagent_scope: EXT, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output' }),
  entry('ext', 6, { subagent_scope: EXT, kind: 'subagent.external.summary', span_name: 'subagent.external', slot: 'summary' }),
  entry('back', 7, {
    turn_start: true, turn_number: 2, kind: 'user.input', slot: 'turn.input', origin: 'subagent', dispatched_by_subagent_scope: RUN,
  }),
  entry('backEnd', 8, {
    turn_number: 2, kind: 'turn.end', slot: 'turn.output', origin: 'subagent', dispatched_by_subagent_scope: RUN,
  }),
]

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
  list: async (): Promise<TrajectoryListResult> => ({
    epoch: 'e1', snapshot_revision: 100, entries: ROWS, next_cursor: null, index_state: READY, complete: true,
  }),
  changes: () => Promise.reject(new Error('not scripted')),
  detail: () => new Promise(() => {}),
  block: () => new Promise(() => {}),
}

beforeEach(async () => {
  list._resetForTests()
  details._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  setSources({ trajectory: source })
  absorb(['trajectory-v1'])
  list.install()
  details.install()
  unpitch()
  list.sessionChanged('gui:a')
  await list.refreshState()
  list.setView('trajectory')
  await list.load()
})

afterEach(() => {
  resetSources()
})

const row = (id: string): TrajectoryEntry => list.entry(id)!

describe('where a row leads', () => {
  it('leads from a door to its scope and from a report turn\'s first row to the scope that sent it, nowhere else', () => {
    expect(scopes.openTarget(row('doorRun'))).toBe(RUN)
    expect(scopes.openTarget(row('back'))).toBe(RUN)
    expect(scopes.openTarget(row('backEnd'))).toBeNull()
    expect(scopes.openTarget(row('m0'))).toBeNull()
    expect(scopes.openTarget(row('r0'))).toBeNull()
  })

  it('goes where a row leads, and does nothing for a row that leads nowhere', () => {
    expect(scopes.openFromEntry(row('m0'))).toBe(false)
    expect([list.get().scope, list.get().selectedId]).toEqual([null, null])
    expect(scopes.openFromEntry(row('back'))).toBe(true)
    /* Every agent is on screen at once: the jump selects, it does not filter. */
    expect([list.get().scope, list.get().selectedId]).toEqual([null, 'r0'])
  })
})

describe('entering a scope', () => {
  it('lands on its external agent\'s entry, with the pane open on what the agent recorded', () => {
    expect(scopes.enter(EXT)).toBe(true)
    const s = list.get()
    expect([s.scope, s.selectedId, s.selectedBy]).toEqual([null, 'ext', 'link'])
    expect(details.get().open).toBe(true)
    expect(details.get().arrival).toBe('ext')
    expect(details.get().preferred).toEqual({ ext: scopes.EXTERNAL_BLOCKS })
  })

  it('lands on the same entry every time, whatever the reader picked since', () => {
    scopes.enter(EXT)
    list.selectAcross('e0', { source: 'click' })
    scopes.enter(RUN)
    scopes.enter(EXT)
    expect([list.get().selectedId, list.get().selectedBy]).toEqual(['ext', 'link'])
  })

  it('lands a run with no external agent on its first entry, and opens the pane on it', () => {
    expect(scopes.enter(RUN)).toBe(true)
    expect([list.get().scope, list.get().selectedId, details.get().open]).toEqual([null, 'r0', true])
    expect(details.get().preferred).toEqual({})
  })

  it('has nowhere to land in a scope with no entries yet, and leaves the selection alone', () => {
    list.selectAcross('m0', { source: 'click' })
    expect(scopes.landingOf('t:nothing-yet')).toBeNull()
    expect(scopes.enter('t:nothing-yet')).toBe(false)
    expect(list.get().selectedId).toBe('m0')
  })
})
