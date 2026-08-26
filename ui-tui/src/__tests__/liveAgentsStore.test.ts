// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { beforeEach, describe, expect, it } from 'vitest'

import type { DagRunStartedEvent, SubagentCall } from '../rpc/index.js'

import {
  $dagRuns,
  $liveAgents,
  applyDagEvent,
  applySubagentStatus,
  dagRunCounts,
  liveAgentCounts,
  resetLiveAgents,
  toSubagentProgress
} from '../app/liveAgentsStore.js'

const status = (over: Partial<Parameters<typeof applySubagentStatus>[0]> = {}) =>
  applySubagentStatus({ agent: 'raven', label: 'find the bug', status: 'pending', task_id: 't1', ...over })

const dagStarted = (): DagRunStartedEvent => ({
  payload: {
    nodes: [
      { depends_on: [], id: 'research', subagent: 'research-raven' },
      { depends_on: ['research'], id: 'write', subagent: 'content-raven' }
    ],
    run_id: 'run-1'
  },
  type: 'dag.run_started'
})

beforeEach(() => resetLiveAgents())

describe('applySubagentStatus', () => {
  it('folds a spawn lifecycle onto one row', () => {
    status()
    expect($liveAgents.get()).toHaveLength(1)
    expect($liveAgents.get()[0]).toMatchObject({ id: 't1', kind: 'spawn', status: 'pending' })

    status({ call_id: 'rec-1', started_at: 1000, status: 'running' })
    expect($liveAgents.get()).toHaveLength(1)
    expect($liveAgents.get()[0]).toMatchObject({ callId: 'rec-1', status: 'running' })

    status({ call_id: 'rec-1', ended_at: 2000, status: 'completed' })
    expect($liveAgents.get()[0]).toMatchObject({ endedAtMs: 2000, status: 'completed' })
  })

  it('never downgrades a terminal row on a late frame', () => {
    status({ call_id: 'rec-1', status: 'completed' })
    status({ call_id: 'rec-1', status: 'running' })
    expect($liveAgents.get()[0]?.status).toBe('completed')
  })

  it('supersedes a disk-seeded row once the event names the same record', () => {
    applySubagentStatus({ agent: 'raven', label: 'seeded', status: 'running', task_id: 'rec-9' })
    status({ call_id: 'rec-9', status: 'running', task_id: 't9' })

    const rows = $liveAgents.get()
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ callId: 'rec-9', id: 't9' })
  })
})

describe('applyDagEvent', () => {
  it('seeds every node pending on run_started, then tracks node_updated', () => {
    applyDagEvent(dagStarted())
    expect($liveAgents.get().map(r => [r.id, r.status])).toEqual([
      ['run-1/research', 'pending'],
      ['run-1/write', 'pending']
    ])

    applyDagEvent({
      payload: { node: 'research', run_id: 'run-1', started_at: 1000, status: 'running' },
      type: 'dag.node_updated'
    })
    expect($liveAgents.get()[0]).toMatchObject({ startedAtMs: 1000, status: 'running' })
  })

  it('settles every node from the run manifest', () => {
    applyDagEvent(dagStarted())
    applyDagEvent({
      payload: {
        dir: '/tmp/run-1',
        files: [
          { node: 'research', status: 'completed' },
          { node: 'write', status: 'skipped' }
        ],
        run_id: 'run-1',
        summary: {}
      },
      type: 'dag.run_completed'
    })

    expect($liveAgents.get().map(r => r.status)).toEqual(['completed', 'skipped'])
  })
})

describe('reconcile and selectors', () => {
  it('counts only pending and running rows', () => {
    status({ status: 'running', task_id: 'a' })
    status({ status: 'pending', task_id: 'b' })
    status({ status: 'completed', task_id: 'c' })

    expect(liveAgentCounts($liveAgents.get())).toEqual({ pending: 1, running: 1 })
  })

  it('maps a dag row into the overlay shape with a dag liveRef', () => {
    applyDagEvent(dagStarted())
    const progress = toSubagentProgress($liveAgents.get()[0]!)

    expect(progress.status).toBe('queued')
    expect(progress.liveRef).toEqual({ kind: 'dag', nodeId: 'research', runId: 'run-1' })
    expect(progress.goal).toContain('research-raven')
  })

  it('maps a spawn row with its record id for subagent.context', () => {
    status({ call_id: 'rec-1', status: 'running' })
    const progress = toSubagentProgress($liveAgents.get()[0]!)

    expect(progress.status).toBe('running')
    expect(progress.liveRef).toEqual({ callId: 'rec-1', kind: 'spawn' })
  })

  it('names the instance a stateful spawn is a turn of, and none for a stateless one', () => {
    status({ call_id: 'rec-1', instance: 'refactor-auth', status: 'running' })
    expect(toSubagentProgress($liveAgents.get()[0]!).instance).toEqual({ agent: 'raven', handle: 'refactor-auth' })

    resetLiveAgents()
    status({ call_id: 'rec-2', status: 'running' })
    expect(toSubagentProgress($liveAgents.get()[0]!).instance).toBeUndefined()
  })
})

describe('reconcileFromList', () => {
  it('seeds unknown active runs and disowns spawn rows the disk no longer lists', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    status({ call_id: 'rec-gone', status: 'running', task_id: 'gone' })

    const items: SubagentCall[] = [
      { id: 'rec-new', kind: 'spawn', label: 'fresh run', message_count: 1, status: 'run' },
      {
        id: 'run-2/node-a',
        kind: 'dag',
        label: 'node-a',
        message_count: 1,
        node: 'node-a',
        run_id: 'run-2',
        status: 'queued'
      }
    ]
    reconcileFromList(items)

    const rows = $liveAgents.get()
    expect(rows.map(r => r.id).sort()).toEqual(['rec-new', 'run-2/node-a'])
    expect(rows.find(r => r.id === 'rec-new')).toMatchObject({ callId: 'rec-new', kind: 'spawn', status: 'running' })
    expect(rows.find(r => r.id === 'run-2/node-a')).toMatchObject({ kind: 'dag-node', status: 'pending' })
  })

  it('disowns a dag row the disk no longer lists, the way it disowns a spawn', async () => {
    // Switching session mid-graph: `ui.sid` changes, the next list is the new
    // session's, and no further dag frame for the old run will ever arrive.
    // Neither path used to reach these rows -- this filter skipped anything that
    // was not a spawn, and `prune` ages only a pending spawn -- so the nodes sat
    // at `running` for the life of the process, holding a strip row and a count
    // in the ratio the spawn HUD colours itself by.
    const { applyDagEvent, liveAgentCounts, reconcileFromList } = await import('../app/liveAgentsStore.js')

    applyDagEvent({
      type: 'dag.run_started',
      payload: { run_id: 'r1', nodes: [{ id: 'n1', subagent: 'coder', depends_on: [] }] }
    } as DagRunStartedEvent)
    applyDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'r1', node: 'n1', status: 'running', started_at: 1 }
    } as never)

    expect(liveAgentCounts($liveAgents.get()).running).toBe(1)

    reconcileFromList([])

    expect($liveAgents.get()).toEqual([])
    expect(liveAgentCounts($liveAgents.get()).running).toBe(0)
  })

  it('settles a live row whose terminal frame was missed', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    status({ call_id: 'rec-1', status: 'running' })
    reconcileFromList([{ id: 'rec-1', kind: 'spawn', label: 'find the bug', message_count: 2, status: 'ok' }])

    expect($liveAgents.get()[0]?.status).toBe('completed')
  })
})

describe('$dagRuns', () => {
  const completion = (statuses: Record<string, string>) => ({
    payload: {
      dir: '/tmp/run-1',
      files: Object.entries(statuses).map(([node, status]) => ({ node, status: status as never })),
      run_id: 'run-1',
      summary: {}
    },
    type: 'dag.run_completed' as const
  })

  it('tallies the whole graph, and keeps tallying it after its rows are gone', () => {
    applyDagEvent({ ...dagStarted(), payload: { ...dagStarted().payload, task_summary: 'ship the site' } })
    applyDagEvent({
      payload: { node: 'research', run_id: 'run-1', started_at: 1000, status: 'running' },
      type: 'dag.node_updated'
    })

    const running = $dagRuns.get()[0]!
    expect(running).toMatchObject({ runId: 'run-1', summary: 'ship the site' })
    expect(running.startedAtMs).toBeDefined()
    expect(dagRunCounts(running)).toEqual({ done: 0, failed: 0, pending: 1, running: 1, total: 2 })

    applyDagEvent(completion({ research: 'completed', write: 'failed' }))

    // The rows may be pruned from under it; the tally is the run's own.
    $liveAgents.set([])

    const done = $dagRuns.get()[0]!
    expect(dagRunCounts(done)).toEqual({ done: 2, failed: 1, pending: 0, running: 0, total: 2 })
    expect(done.endedAtMs).toBeDefined()
  })

  it('keeps a node its own end when the run completes later', () => {
    applyDagEvent(dagStarted())
    applyDagEvent({
      payload: { ended_at: 61_000, node: 'research', run_id: 'run-1', started_at: 1_000, status: 'completed' },
      type: 'dag.node_updated'
    })
    applyDagEvent({
      payload: { node: 'write', run_id: 'run-1', started_at: 61_000, status: 'running' },
      type: 'dag.node_updated'
    })

    applyDagEvent(completion({ research: 'completed', write: 'completed' }))

    // research ran for a minute; the run ended much later, and that is not
    // research's elapsed time.
    const research = $liveAgents.get().find(r => r.id === 'run-1/research')!
    expect(research.endedAtMs).toBe(61_000)
    expect(toSubagentProgress(research).durationSeconds).toBe(60)
  })

  it('ends a run whose completion frame never came, when its last node settles', () => {
    applyDagEvent(dagStarted())
    applyDagEvent({
      payload: { node: 'research', run_id: 'run-1', status: 'completed' },
      type: 'dag.node_updated'
    })
    expect($dagRuns.get()[0]?.endedAtMs).toBeUndefined()

    applyDagEvent({ payload: { node: 'write', run_id: 'run-1', status: 'skipped' }, type: 'dag.node_updated' })
    expect($dagRuns.get()[0]?.endedAtMs).toBeDefined()
  })

  it('never opens a run on a node frame alone, whose node set it cannot know', () => {
    applyDagEvent({
      payload: { node: 'orphan', run_id: 'run-9', status: 'running' },
      type: 'dag.node_updated'
    })

    expect($dagRuns.get()).toEqual([])
  })

  it('keeps a terminal node terminal on a late frame, as the rows do', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(completion({ research: 'completed', write: 'completed' }))
    applyDagEvent({ payload: { node: 'research', run_id: 'run-1', status: 'running' }, type: 'dag.node_updated' })

    expect(dagRunCounts($dagRuns.get()[0]!)).toMatchObject({ done: 2, running: 0 })
  })
})

describe('$dagRuns reconcile', () => {
  const dagItem = (node: string, status: string): SubagentCall => ({
    id: `run-2/${node}`,
    kind: 'dag',
    label: node,
    message_count: 1,
    node,
    run_id: 'run-2',
    status
  })

  it('seeds a graph the disk still shows working, with every node it lists', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    reconcileFromList([dagItem('a', 'ok'), dagItem('b', 'run'), dagItem('c', 'queued')])

    const run = $dagRuns.get()[0]!
    expect(run.runId).toBe('run-2')
    expect(dagRunCounts(run)).toEqual({ done: 1, failed: 0, pending: 1, running: 1, total: 3 })
  })

  it('does not resurrect a graph that was over before this session opened', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    reconcileFromList([dagItem('a', 'ok'), dagItem('b', 'ok')])

    expect($dagRuns.get()).toEqual([])
  })

  it('cancels a node the disk stopped listing, so an abandoned graph stops reading as live', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    reconcileFromList([dagItem('a', 'run'), dagItem('b', 'queued')])
    expect(dagRunCounts($dagRuns.get()[0]!)).toMatchObject({ pending: 1, running: 1 })

    reconcileFromList([dagItem('a', 'ok')])

    expect(dagRunCounts($dagRuns.get()[0]!)).toEqual({ done: 2, failed: 1, pending: 0, running: 0, total: 2 })
  })

  it('leaves a graph alone when the snapshot names no node of it', async () => {
    const { reconcileFromList } = await import('../app/liveAgentsStore.js')

    reconcileFromList([dagItem('a', 'run')])
    reconcileFromList([])

    expect(dagRunCounts($dagRuns.get()[0]!)).toMatchObject({ running: 1 })
  })
})
