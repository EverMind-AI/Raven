// @vitest-environment happy-dom
/* The reader's moves between Scope Tabs: which tab a row leads to, a freshly
   opened external agent's tab landing on its entry and recorded block, closing
   a tab, and going back to the entry that opens a scope. */
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

  it('opens what a row leads to, and does nothing for a row that leads nowhere', () => {
    expect(scopes.openFromEntry(row('m0'))).toBe(false)
    expect([list.get().scope, list.get().scopes]).toEqual([null, []])
    expect(scopes.openFromEntry(row('back'))).toBe(true)
    expect(list.get().scope).toBe(RUN)
  })
})

describe('entering a tab', () => {
  it('lands a tab opened just now on its external agent\'s entry, with the pane open on what the agent recorded', () => {
    scopes.enter(EXT)
    const s = list.get()
    expect([s.scope, s.selectedId, s.selectedBy]).toEqual([EXT, 'ext', 'link'])
    expect(details.get().open).toBe(true)
    expect(details.get().arrival).toBe('ext')
    expect(details.get().preferred).toEqual({ ext: scopes.EXTERNAL_BLOCKS })
  })

  it('goes back to where the reader was on the way back, not to the external agent\'s entry again', () => {
    scopes.enter(EXT)
    list.select('e0', { source: 'click' })
    scopes.enter(RUN)
    scopes.enter(EXT)
    expect([list.get().selectedId, list.get().selectedBy]).toEqual(['e0', 'migrate'])
  })

  it('opens a tab with no external agent on nothing selected and the pane closed', () => {
    scopes.enter(RUN)
    expect([list.get().scope, list.get().selectedId, details.get().open]).toEqual([RUN, null, false])
    expect(details.get().preferred).toEqual({})
  })
})
