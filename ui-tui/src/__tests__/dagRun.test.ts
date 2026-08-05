// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagNodeUpdatedEvent, DagRunCompletedEvent, DagRunSnapshot, DagRunStartedEvent } from '../rpc/index.js'

import { foldDagEvent, foldDagSnapshot } from '../domain/dagRun.js'

const started = (
  nodes: DagRunStartedEvent['payload']['nodes'],
  runId = 'dag-1',
  toolCallId?: string
): DagRunStartedEvent => ({
  type: 'dag.run_started',
  payload: { run_id: runId, nodes, ...(toolCallId ? { tool_call_id: toolCallId } : {}) }
})

const updated = (
  node: string,
  status: DagNodeUpdatedEvent['payload']['status'],
  runId = 'dag-1'
): DagNodeUpdatedEvent => ({
  type: 'dag.node_updated',
  payload: { run_id: runId, node, status }
})

const CHAIN: DagRunStartedEvent['payload']['nodes'] = [
  { id: 'a', subagent: 'echo', depends_on: [] },
  { id: 'b', subagent: 'echo', depends_on: ['a'] }
]

describe('foldDagEvent', () => {
  it('starts every node of a new run as pending', () => {
    const run = foldDagEvent(null, started(CHAIN))

    expect(run).not.toBeNull()
    expect(run?.runId).toBe('dag-1')
    expect(run?.nodes.map(n => [n.id, n.status])).toEqual([
      ['a', 'pending'],
      ['b', 'pending']
    ])
  })

  it('keeps the tool call id so the graph can be drawn under its own tool row', () => {
    expect(foldDagEvent(null, started(CHAIN, 'dag-1', 'call-a'))?.toolCallId).toBe('call-a')
  })

  it('applies a node status change', () => {
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), updated('a', 'running'))

    expect(run?.nodes.find(n => n.id === 'a')?.status).toBe('running')
    expect(run?.nodes.find(n => n.id === 'b')?.status).toBe('pending')
  })

  it('drops an update that arrives before the run started', () => {
    // The subscription can attach mid-run; without the graph there is nothing
    // to draw, and inventing a node from an update would draw a partial graph
    // that never gains its edges.
    expect(foldDagEvent(null, updated('a', 'running'))).toBeNull()
  })

  it('ignores an update for a node the graph does not contain', () => {
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), updated('ghost', 'running'))

    expect(run?.nodes.map(n => n.id)).toEqual(['a', 'b'])
  })

  it('ignores an event from a different run', () => {
    const first = foldDagEvent(null, started(CHAIN))
    const run = foldDagEvent(first, updated('a', 'running', 'dag-2'))

    expect(run?.nodes.find(n => n.id === 'a')?.status).toBe('pending')
  })

  it('marks the run finished and records where the outputs went', () => {
    const completed: DagRunCompletedEvent = {
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/.ravenx_dag/dag-1',
        summary: { total: 2, completed: 1, failed: 1, skipped: 0 },
        files: [
          { node: 'a', status: 'completed', output_file: '/w/.ravenx_dag/dag-1/a.out.md' },
          { node: 'b', status: 'failed', error: 'boom' }
        ]
      }
    }
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), completed)

    expect(run?.done).toBe(true)
    expect(run?.dir).toBe('/w/.ravenx_dag/dag-1')
    expect(run?.summary).toEqual({ total: 2, completed: 1, failed: 1, skipped: 0 })
    expect(run?.nodes.find(n => n.id === 'a')?.outputFile).toBe('/w/.ravenx_dag/dag-1/a.out.md')
    expect(run?.nodes.find(n => n.id === 'b')?.error).toBe('boom')
  })

  it('takes terminal node statuses from the completion manifest', () => {
    // A node whose own update was lost (a dropped frame, a subscription that
    // attached late) would otherwise sit at pending forever on a finished run.
    const completed: DagRunCompletedEvent = {
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/.ravenx_dag/dag-1',
        summary: { total: 2, completed: 2, failed: 0, skipped: 0 },
        files: [
          { node: 'a', status: 'completed' },
          { node: 'b', status: 'completed' }
        ]
      }
    }
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), completed)

    expect(run?.nodes.map(n => n.status)).toEqual(['completed', 'completed'])
  })

  it('reports a node still called running on a finished run as interrupted', () => {
    // The runner only ever reports the four terminal states, so a node left
    // running when the run closed means its terminal write never arrived.
    const completed: DagRunCompletedEvent = {
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/.ravenx_dag/dag-1',
        summary: { total: 2, completed: 1, failed: 0, skipped: 0 },
        files: [{ node: 'a', status: 'completed' }]
      }
    }
    const mid = foldDagEvent(foldDagEvent(null, started(CHAIN)), updated('b', 'running'))
    const run = foldDagEvent(mid, completed)

    expect(run?.nodes.find(n => n.id === 'b')?.status).toBe('interrupted')
  })

  it('does not mutate the run it folds onto', () => {
    const before = foldDagEvent(null, started(CHAIN))
    const snapshot = JSON.stringify(before)

    foldDagEvent(before, updated('a', 'completed'))

    expect(JSON.stringify(before)).toBe(snapshot)
  })
})

describe('foldDagSnapshot', () => {
  const snapshot = (files: DagRunSnapshot['files'], over: Partial<DagRunSnapshot> = {}): DagRunSnapshot => ({
    run_id: 'dag-1',
    dir: '/w/.ravenx_dag/dag-1',
    finalized: true,
    files,
    summary: { total: files.length, completed: 0, failed: 0, skipped: 0 },
    ...over
  })

  it('repairs a graph whose live frames were lost', () => {
    // The whole point: a run whose gateway died mid-flight leaves nodes pinned
    // to `running` forever. The snapshot is the durable truth.
    const live = foldDagEvent(foldDagEvent(null, started(CHAIN)), updated('a', 'running'))
    const run = foldDagSnapshot(
      live,
      snapshot([
        { node: 'a', status: 'interrupted' },
        { node: 'b', status: 'skipped' }
      ])
    )

    expect(run.nodes.map(n => n.status)).toEqual(['interrupted', 'skipped'])
    expect(run.done).toBe(true)
  })

  it('keeps an unfinalized run open', () => {
    const live = foldDagEvent(null, started(CHAIN))
    const run = foldDagSnapshot(live, snapshot([{ node: 'a', status: 'running' }], { finalized: false }))

    expect(run.done).toBe(false)
  })

  it('rebuilds the topology when there is no local run at all', () => {
    // Nothing was ever folded locally (the client attached after the run began),
    // so the snapshot has to carry the graph shape too, not just the statuses.
    const run = foldDagSnapshot(
      null,
      snapshot([
        { node: 'a', status: 'completed', subagent: 'echo', depends_on: [] },
        { node: 'b', status: 'running', subagent: 'claude', depends_on: ['a'], instance: 'author' }
      ])
    )

    expect(run.runId).toBe('dag-1')
    expect(run.nodes.map(n => [n.id, n.subagent, n.dependsOn])).toEqual([
      ['a', 'echo', []],
      ['b', 'claude', ['a']]
    ])
    expect(run.nodes[1]!.instance).toBe('author')
  })

  it('carries the per-node output paths and errors', () => {
    const run = foldDagSnapshot(
      null,
      snapshot([
        { node: 'a', status: 'completed', output_file: '/w/a.out.md' },
        { node: 'b', status: 'failed', error: 'exited 1' }
      ])
    )

    expect(run.nodes[0]!.outputFile).toBe('/w/a.out.md')
    expect(run.nodes[1]!.error).toBe('exited 1')
  })

  it('preserves the tool call id the local run was pinned by', () => {
    // The snapshot has no idea which tool row drew it; losing this would orphan
    // the graph from its transcript row.
    const live = foldDagEvent(null, started(CHAIN, 'dag-1', 'call-a'))
    const run = foldDagSnapshot(live, snapshot([{ node: 'a', status: 'completed' }]))

    expect(run.toolCallId).toBe('call-a')
  })
})
