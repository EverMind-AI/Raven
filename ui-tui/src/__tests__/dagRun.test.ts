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

  it("keeps a node's timings, and does not lose them to a later frame that has none", () => {
    // `started_at` rides the frame that starts a node and `ended_at` the one
    // that ends it; a frame carrying neither must not blank what a row prints
    // as the node's elapsed time.
    const running = foldDagEvent(foldDagEvent(null, started(CHAIN)), {
      type: 'dag.node_updated',
      payload: { node: 'a', run_id: 'dag-1', started_at: 1_000, status: 'running' }
    })

    expect(running?.nodes[0]).toMatchObject({ startedAt: 1_000 })

    const done = foldDagEvent(running, {
      type: 'dag.node_updated',
      payload: { ended_at: 4_000, node: 'a', run_id: 'dag-1', status: 'completed' }
    })

    expect(done?.nodes[0]).toMatchObject({ endedAt: 4_000, startedAt: 1_000, status: 'completed' })
  })

  it('keeps a suspended node non-terminal and its dependent pending', () => {
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), updated('a', 'exception'))

    expect(run?.nodes.map(n => n.status)).toEqual(['exception', 'pending'])
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
        dir: '/w/mas_dag/dag-1',
        summary: { total: 2, completed: 1, failed: 1, skipped: 0 },
        files: [
          { node: 'a', status: 'completed', output_file: '/w/mas_dag/dag-1/a.out.md' },
          { node: 'b', status: 'failed', error: 'boom' }
        ]
      }
    }
    const run = foldDagEvent(foldDagEvent(null, started(CHAIN)), completed)

    expect(run?.done).toBe(true)
    expect(run?.dir).toBe('/w/mas_dag/dag-1')
    expect(run?.summary).toEqual({ total: 2, completed: 1, failed: 1, skipped: 0 })
    expect(run?.nodes.find(n => n.id === 'a')?.outputFile).toBe('/w/mas_dag/dag-1/a.out.md')
    expect(run?.nodes.find(n => n.id === 'b')?.error).toBe('boom')
  })

  it('takes terminal node statuses from the completion manifest', () => {
    // A node whose own update was lost (a dropped frame, a subscription that
    // attached late) would otherwise sit at pending forever on a finished run.
    const completed: DagRunCompletedEvent = {
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/mas_dag/dag-1',
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
        dir: '/w/mas_dag/dag-1',
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

  it("attaches each node's prompt template, which no event carries", () => {
    // The template comes off the tool call's own arguments -- the only source
    // available before a node has run, which is when the graph is first drawn.
    const run = foldDagEvent(null, started(CHAIN, 'dag-1', 'call-a'), { a: 'read the file', b: 'summarise it' })

    expect(run?.nodes.map(n => n.promptTemplate)).toEqual(['read the file', 'summarise it'])
  })

  it('leaves a node the call args did not name without a template', () => {
    const run = foldDagEvent(null, started(CHAIN, 'dag-1', 'call-a'), { a: 'read the file' })

    expect(run?.nodes[0]!.promptTemplate).toBe('read the file')
    expect(run?.nodes[1]!.promptTemplate).toBeUndefined()
  })

  it('records the successor run on the run it replaced', () => {
    const run = foldDagEvent(null, started(CHAIN))

    const folded = foldDagEvent(run, {
      type: 'dag.run_replanned',
      payload: { run_id: 'dag-1', replan_run_id: 'dag-2', from_node: 'a', reason: 'the plan was wrong' }
    })

    expect(folded?.replannedInto).toBe('dag-2')
  })

  it('records the successor even after the run has settled', () => {
    // This normally arrives before the run's own `dag.run_completed` (the event
    // fires the moment the desk accepts the decision, not once the successor has
    // started), but the TUI pins run state permanently and must absorb the link
    // whenever it arrives, including after the run has already settled.
    const completed: DagRunCompletedEvent = {
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/mas_dag/dag-1',
        summary: { total: 2, completed: 2, failed: 0, skipped: 0 },
        files: [
          { node: 'a', status: 'completed' },
          { node: 'b', status: 'completed' }
        ]
      }
    }
    const done = foldDagEvent(foldDagEvent(null, started(CHAIN)), completed)

    const folded = foldDagEvent(done, {
      type: 'dag.run_replanned',
      payload: { run_id: 'dag-1', replan_run_id: 'dag-2', from_node: 'a', reason: 'wrong' }
    })

    expect(folded?.replannedInto).toBe('dag-2')
    expect(folded?.done).toBe(true)
    expect(folded?.nodes.map(n => n.status)).toEqual(['completed', 'completed'])
  })

  it('ignores a replanned event for another run', () => {
    const run = foldDagEvent(null, started(CHAIN))

    const folded = foldDagEvent(run, {
      type: 'dag.run_replanned',
      payload: { run_id: 'other', replan_run_id: 'dag-2', from_node: 'a', reason: 'wrong' }
    })

    expect(folded).toBe(run)
  })
})

describe('foldDagSnapshot', () => {
  const snapshot = (files: DagRunSnapshot['files'], over: Partial<DagRunSnapshot> = {}): DagRunSnapshot => ({
    run_id: 'dag-1',
    dir: '/w/mas_dag/dag-1',
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

  it("reads each node's prompt template off the run dir", () => {
    const run = foldDagSnapshot(null, snapshot([{ node: 'a', status: 'completed', prompt_template: 'read the file' }]))

    expect(run.nodes[0]!.promptTemplate).toBe('read the file')
  })

  it('keeps the template the call args supplied when the run dir has none', () => {
    // An older run dir wrote no template. Letting the snapshot win outright
    // there would blank a row that was reading fine a moment earlier.
    const live = foldDagEvent(null, started(CHAIN, 'dag-1', 'call-a'), { a: 'read the file' })
    const run = foldDagSnapshot(live, snapshot([{ node: 'a', status: 'completed' }]))

    expect(run.nodes[0]!.promptTemplate).toBe('read the file')
  })
})
