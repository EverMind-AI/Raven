// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { beforeEach, describe, expect, it } from 'vitest'

import type { DagNodeUpdatedEvent, DagRunStartedEvent, SubagentCall } from '../rpc/index.js'

import {
  $dagRuns,
  $liveAgents,
  agentRows,
  applyDagEvent,
  applySubagentStatus,
  dagRunCounts,
  isDagRunSettled,
  isTerminal,
  liveAgentCounts,
  reconcileFromList,
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

  it('seeds a run that was over before this session opened, for the overlay', async () => {
    // After a resume the disk is the overlay's only account of what the
    // session delegated; the strip is unaffected, never drawing a settled row.
    const { liveAgentCounts, reconcileFromList, resetLiveAgents } = await import('../app/liveAgentsStore.js')

    resetLiveAgents()
    reconcileFromList([
      { id: 'rec-old', kind: 'spawn', label: 'done before resume', message_count: 3, status: 'ok' },
      { id: 'r9/n1', kind: 'dag', label: 'n1', message_count: 1, node: 'n1', run_id: 'r9', status: 'interrupted' }
    ])

    const rows = $liveAgents.get()
    expect(rows.find(r => r.id === 'rec-old')).toMatchObject({ kind: 'spawn', status: 'completed' })
    expect(rows.find(r => r.id === 'r9/n1')).toMatchObject({ kind: 'dag-node', status: 'cancelled' })
    expect(liveAgentCounts(rows)).toEqual({ pending: 0, running: 0 })
  })

  it('keeps the newest rows when a resume seed crosses the row cap', async () => {
    // `subagent.list` answers newest first; seeded in that order the newest
    // rows drew the lowest seq and the cap prune, which keeps terminal rows by
    // descending seq, retained the oldest 300 instead.
    const { reconcileFromList, resetLiveAgents } = await import('../app/liveAgentsStore.js')

    resetLiveAgents()

    const items: SubagentCall[] = Array.from({ length: 302 }, (_, i) => ({
      id: `rec-${String(302 - i).padStart(3, '0')}`,
      kind: 'spawn' as const,
      label: 'done before resume',
      message_count: 1,
      started_at: new Date(1_000_000 + (302 - i) * 1000).toISOString(),
      status: 'ok'
    }))
    reconcileFromList(items)

    const ids = new Set($liveAgents.get().map(r => r.id))
    expect(ids.size).toBe(300)
    expect(ids.has('rec-302')).toBe(true)
    expect(ids.has('rec-301')).toBe(true)
    expect(ids.has('rec-001')).toBe(false)
    expect(ids.has('rec-002')).toBe(false)

    // The same snapshot folds in again on every poll. A pruned old row is
    // re-seeded with the freshest seq, so the cap must trim by chronology or
    // each reconciliation rotates pruned rows back in and evicts newer ones.
    reconcileFromList(items)
    reconcileFromList(items)

    const again = [...new Set($liveAgents.get().map(r => r.id))].sort()
    expect(again).toEqual([...ids].sort())
  })

  it('stays stable across polls when rows carry no start clock, or share one', async () => {
    // `started_at` is optional (a record whose meta was unreadable has none)
    // and shareable (graph nodes start together); on a clock tie the trim must
    // fall back to something stable across polls, which fresh insertion order
    // is not.
    const { reconcileFromList, resetLiveAgents } = await import('../app/liveAgentsStore.js')

    for (const started_at of [undefined, new Date(1_000_000).toISOString()]) {
      resetLiveAgents()

      const items: SubagentCall[] = Array.from({ length: 302 }, (_, i) => ({
        id: `rec-${String(302 - i).padStart(3, '0')}`,
        kind: 'spawn' as const,
        label: 'done before resume',
        message_count: 1,
        ...(started_at === undefined ? {} : { started_at }),
        status: 'ok'
      }))

      reconcileFromList(items)
      const first = [...new Set($liveAgents.get().map(r => r.id))].sort()
      expect(first).toHaveLength(300)
      expect(first).toContain('rec-302')
      expect(first).toContain('rec-003')
      expect(first).not.toContain('rec-001')
      expect(first).not.toContain('rec-002')

      reconcileFromList(items)
      reconcileFromList(items)
      const again = [...new Set($liveAgents.get().map(r => r.id))].sort()
      expect(again).toEqual(first)
    }
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

  it('keeps a node waiting on adjudication open when the list folds it to "error"', async () => {
    // raven/rpc/methods/subagent.py's _DAG_WIRE_STATUS collapses a suspended
    // node's status to "error" on this wire, the same value a genuine failure
    // reports. The list poll right after the exception announce carries
    // exactly that, and must not be read as the node resolving itself.
    const { applyDagEvent, reconcileFromList } = await import('../app/liveAgentsStore.js')

    applyDagEvent({
      type: 'dag.run_started',
      payload: { run_id: 'run-3', nodes: [{ id: 'stuck', subagent: 'coder', depends_on: [] }] }
    } as DagRunStartedEvent)
    applyDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'run-3', node: 'stuck', status: 'exception' }
    } as never)

    reconcileFromList([{ id: 'run-3/stuck', kind: 'dag', label: 'stuck', message_count: 1, status: 'error' }])

    const row = $liveAgents.get().find(r => r.id === 'run-3/stuck')
    expect(row).toMatchObject({ status: 'exception' })
    expect(row?.settledAtMs).toBeUndefined()
  })
})

describe('a node a branch left out', () => {
  const nodeUpdated = (
    node: string,
    status: DagNodeUpdatedEvent['payload']['status'],
    extra: Partial<DagNodeUpdatedEvent['payload']> = {}
  ): DagNodeUpdatedEvent => ({ payload: { node, run_id: 'run-1', status, ...extra }, type: 'dag.node_updated' })

  const rowFor = (id: string) => $liveAgents.get().find(r => r.id === id)

  // Read off the generated union rather than listed here: the next status the
  // schema grows is the one nobody remembered to map.
  const wireStatuses = () => {
    const source = readFileSync(fileURLToPath(new URL('../rpc/generated.ts', import.meta.url)), 'utf8')
    const start = source.indexOf('export type DagNodeStatus =')

    return [...source.slice(start, source.indexOf(';', start)).matchAll(/'([a-z_]+)'/g)].map(
      m => m[1]!
    ) as DagNodeUpdatedEvent['payload']['status'][]
  }

  const completion = (statuses: Record<string, DagNodeUpdatedEvent['payload']['status']>) => ({
    payload: {
      dir: '/tmp/run-1',
      files: Object.entries(statuses).map(([node, status]) => ({ node, status })),
      run_id: 'run-1',
      summary: {}
    },
    type: 'dag.run_completed' as const
  })

  it('settles rather than staying running', () => {
    // An unmapped status falls back to `running`, which is a node that never
    // finishes: the strip keeps its row and the graph keeps its clock.
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('write', 'not_executed', { reason: 'when_false' }))

    const row = rowFor('run-1/write')

    expect(row?.status).toBe('not_executed')
    expect(row?.settledAtMs).toBeDefined()
    expect(isTerminal('not_executed')).toBe(true)
  })

  it('keeps the end a frame stamped it with', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('write', 'not_executed', { ended_at: 5_000 }))

    expect(rowFor('run-1/write')).toMatchObject({ endedAtMs: 5_000, status: 'not_executed' })
  })

  it('stays settled when a late frame says it is running', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('write', 'not_executed'))
    applyDagEvent(nodeUpdated('write', 'running'))

    expect(rowFor('run-1/write')?.status).toBe('not_executed')
    expect(dagRunCounts($dagRuns.get()[0]!)).toMatchObject({ running: 0 })
  })

  it('is settled by the run manifest, which gives it an end', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(completion({ research: 'completed', write: 'not_executed' }))

    expect(rowFor('run-1/write')).toMatchObject({ status: 'not_executed' })
    expect(rowFor('run-1/write')?.endedAtMs).toBeDefined()
  })

  it('counts as done on its run, neither failed nor still moving', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('research', 'completed'))
    applyDagEvent(nodeUpdated('write', 'not_executed'))

    expect(dagRunCounts($dagRuns.get()[0]!)).toEqual({ done: 2, failed: 0, pending: 0, running: 0, total: 2 })
  })

  it('ends a run whose last node it is, though the completion frame never came', () => {
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('research', 'completed'))
    expect($dagRuns.get()[0]?.endedAtMs).toBeUndefined()

    applyDagEvent(nodeUpdated('write', 'not_executed'))

    expect(isDagRunSettled($dagRuns.get()[0]!)).toBe(true)
    expect($dagRuns.get()[0]?.endedAtMs).toBeDefined()
  })

  it('is seeded settled from a list row that names it', () => {
    reconcileFromList([
      { id: 'r9/n2', kind: 'dag', label: 'n2', message_count: 0, node: 'n2', run_id: 'r9', status: 'not_executed' }
    ])

    expect(rowFor('r9/n2')).toMatchObject({ kind: 'dag-node', status: 'not_executed' })
    expect(rowFor('r9/n2')?.settledAtMs).toBeDefined()
  })

  it('is no agent, so the overlay has no row for it', () => {
    // `completed` would say an agent ran and finished, and `interrupted` would
    // light the failed filter for a run that went as written.
    applyDagEvent(dagStarted())
    applyDagEvent(nodeUpdated('research', 'completed'))
    applyDagEvent(nodeUpdated('write', 'not_executed', { reason: 'when_false' }))

    expect(agentRows($liveAgents.get()).map(r => r.id)).toEqual(['run-1/research'])
    // The store keeps the row: it is how the run learns the node is settled.
    expect(rowFor('run-1/write')).toMatchObject({ status: 'not_executed' })
    expect(isDagRunSettled($dagRuns.get()[0]!)).toBe(true)
  })

  it('leaves every row of a run no branch touched in the overlay, as it was', () => {
    const legacy = wireStatuses().filter(status => status !== 'not_executed')

    applyDagEvent({
      payload: { nodes: legacy.map(id => ({ depends_on: [], id, subagent: 'echo' })), run_id: 'run-1' },
      type: 'dag.run_started'
    })
    legacy.forEach(status => applyDagEvent(nodeUpdated(status, status)))

    const rows = $liveAgents.get()
    const kept = agentRows(rows)

    expect(rows.map(r => r.status)).toEqual(legacy)
    expect(kept).toHaveLength(rows.length)
    kept.forEach((row, i) => expect(row).toBe(rows[i]))
  })

  it('maps every status the wire can send, so that none falls back to running', () => {
    const wire = wireStatuses()

    expect(wire).toContain('not_executed')
    expect(wire.length).toBeGreaterThan(6)

    for (const status of wire) {
      resetLiveAgents()
      applyDagEvent(dagStarted())
      applyDagEvent(nodeUpdated('write', status))

      expect(rowFor('run-1/write')?.status, status).toBe(status)
      expect($dagRuns.get()[0]?.nodes.write, status).toBe(status)
    }
  })
})

// The strip and the overlay name the agent a node is on, which moves when the
// node is handed to another agent. A frame carries it only once it has.
describe('a dag row whose node was reassigned', () => {
  const rowFor = (id: string) => $liveAgents.get().find(r => r.id === id)
  const update = (over: Record<string, unknown> = {}): DagNodeUpdatedEvent => ({
    type: 'dag.node_updated',
    payload: { run_id: 'run-1', node: 'write', status: 'running', ...over }
  })

  beforeEach(() => {
    applyDagEvent(dagStarted())
  })

  it('moves the agent and the handle with the frame that carries them', () => {
    applyDagEvent(
      update({ subagent: 'claude', instance: 'write-minted', attempt_agents: ['research-raven', 'claude'] })
    )

    expect(rowFor('run-1/write')).toMatchObject({ agent: 'claude', instance: 'write-minted' })
  })

  it('keeps what an earlier frame said when this one names neither', () => {
    applyDagEvent(update({ subagent: 'claude', instance: 'write-minted' }))
    applyDagEvent(update({ status: 'completed' }))

    expect(rowFor('run-1/write')).toMatchObject({ agent: 'claude', instance: 'write-minted', status: 'completed' })
  })

  it('clears the handle on a stateless retry', () => {
    applyDagEvent(update({ subagent: 'claude', instance: 'write-minted' }))
    applyDagEvent(update({ instance: null, subagent: 'codex' }))

    expect(rowFor('run-1/write')?.instance).toBeUndefined()
    expect(rowFor('run-1/write')?.agent).toBe('codex')
  })

  it('leaves a run that never reassigned its rows exactly as they were', () => {
    applyDagEvent(update({ status: 'completed' }))

    expect(rowFor('run-1/write')).toMatchObject({
      agent: 'content-raven',
      id: 'run-1/write',
      kind: 'dag-node',
      status: 'completed'
    })
    expect(rowFor('run-1/write')?.instance).toBeUndefined()
  })
})
