// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A spawn run's path through a turn: `subagent.status` frames -> turnController
// -> the live turn store, and the run pinned onto the tool row so it survives
// into the transcript -- the single-run counterpart of dagTurnFlow.

import { afterEach, describe, expect, it } from 'vitest'

import type { SubagentStatusEvent } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState } from '../app/uiStore.js'
import { foldSpawnStatus, spawnRunFromListRow, spawnRunsFromHistory } from '../domain/spawnRun.js'

afterEach(() => {
  patchUiState({ transcript: 'legacy' })
  turnController.reset()
  // reset() goes through idle(), which deliberately keeps a still-working run
  // for the frames yet to come -- across tests that reads as leakage.
  resetTurnState()
})

const status = (
  over: Partial<SubagentStatusEvent['payload']> = {}
): SubagentStatusEvent['payload'] => ({
  task_id: '1a021575',
  agent: 'raven-research',
  label: 'research carbon monoxide',
  status: 'pending',
  tool_call_id: 'call-1',
  ...over
})

describe('turnController spawn runs', () => {
  it('opens a run on the live turn store', () => {
    turnController.reset()
    turnController.recordSpawnStatus(status())

    expect(getTurnState().spawnRuns.map(run => run.taskId)).toEqual(['1a021575'])
    expect(getTurnState().spawnRuns[0]!.status).toBe('pending')
  })

  it('drops a frame that names no tool call', () => {
    // One without belongs to no row of this turn (an older gateway, or a spawn
    // another surface dispatched); $liveAgents already tracks it for the strip.
    turnController.reset()
    turnController.recordSpawnStatus(status({ tool_call_id: undefined }))

    expect(getTurnState().spawnRuns).toEqual([])
  })

  it('keeps the record id and the clocks across later frames', () => {
    turnController.reset()
    turnController.recordSpawnStatus(status())
    turnController.recordSpawnStatus(
      status({ call_id: '20260827T02Z-1a021575', started_at: 1000, status: 'running' })
    )
    turnController.recordSpawnStatus(status({ ended_at: 5000, status: 'completed' }))

    const [run] = getTurnState().spawnRuns

    expect(run!.status).toBe('completed')
    expect(run!.callId).toBe('20260827T02Z-1a021575')
    expect(run!.startedAt).toBe(1000)
    expect(run!.endedAt).toBe(5000)
  })

  it('pins the run onto the tool row that dispatched it', () => {
    // Without this the run vanishes when the turn closes: the live store keeps
    // only working runs, and the tool result is one line of prose.
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-1', 'spawn', 'research carbon monoxide')
    turnController.recordSpawnStatus(status())
    turnController.recordToolComplete('call-1', 'spawn', undefined, 'Subagent [...] started (id: 1a021575).', 1)
    turnController.recordSpawnStatus(status({ call_id: '20260827T02Z-1a021575', status: 'running' }))
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'done' })
    const tool = finalMessages.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.spawn?.taskId).toBe('1a021575')
    expect(tool.spawn?.status).toBe('running')
  })

  it('keeps a still-working run across the end of its turn, and releases a settled one', () => {
    // `spawn` returns as soon as the run is scheduled, so the reply commits
    // while the sub-agent still works and every later frame arrives after
    // idle(). A settled run is already pinned and goes with the turn.
    turnController.reset()
    turnController.recordSpawnStatus(status({ status: 'running' }))
    turnController.recordSpawnStatus(status({ status: 'completed', task_id: 'aa021575' }))
    turnController.idle()

    expect(getTurnState().spawnRuns.map(run => run.taskId)).toEqual(['1a021575'])

    turnController.recordSpawnStatus(status({ ended_at: 5000, status: 'completed' }))

    expect(getTurnState().spawnRuns[0]!.status).toBe('completed')
  })
})

describe('foldSpawnStatus', () => {
  it('never moves a settled run back to a live status', () => {
    // The terminal frame and a late `running` can arrive out of order, and a
    // run that finished must not start pulsing again.
    const settled = foldSpawnStatus(null, status({ ended_at: 5000, status: 'completed' }))

    expect(foldSpawnStatus(settled, status({ status: 'running' }))!.status).toBe('completed')
  })
})

describe('spawnRunFromListRow', () => {
  const rows = [
    { id: '20260827T02Z-9/n1', kind: 'dag', label: 'a node', status: 'ok' },
    {
      id: '20260827T021530000000Z-1a021575',
      kind: 'spawn',
      label: 'research carbon monoxide',
      status: 'ok',
      agent: 'raven-research',
      started_at: '2026-08-27T02:15:30.000Z',
      ended_at: '2026-08-27T02:18:00.000Z'
    }
  ]

  it('finds the record by its task suffix and maps the wire status', () => {
    const run = spawnRunFromListRow('1a021575', 'call-1', rows)

    expect(run?.callId).toBe('20260827T021530000000Z-1a021575')
    expect(run?.status).toBe('completed')
    expect(run?.toolCallId).toBe('call-1')
    expect(run?.startedAt).toBe(Date.parse('2026-08-27T02:15:30.000Z'))
  })

  it('answers null for a task the list does not name', () => {
    expect(spawnRunFromListRow('deadbeef', 'call-1', rows)).toBeNull()
  })
})

describe('spawnRunsFromHistory', () => {
  it('collects the runs pinned onto tool rows, one per task id', () => {
    const pinned = foldSpawnStatus(null, status({ status: 'running' }))
    const history: Msg[] = [
      {
        kind: 'episodes',
        role: 'assistant',
        text: '',
        episodes: [
          { index: 0, tools: [{ id: 'call-1', name: 'spawn', summary: 't', ok: true, spawn: pinned }] },
          { index: 1, tools: [{ id: 'call-2', name: 'read_file', summary: 'a.ts', ok: true }] }
        ]
      }
    ]

    expect(spawnRunsFromHistory(history).map(run => run.taskId)).toEqual(['1a021575'])
  })
})
