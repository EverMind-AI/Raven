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
    // The runner only ever reports the five terminal states, so a node left
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

describe('foldDagEvent on a branching run', () => {
  const BRANCHING: DagRunStartedEvent['payload']['nodes'] = [
    { id: 'triage', subagent: 'echo', depends_on: [], routes: ['docs', 'bug'], select: 'one' },
    { id: 'fix', subagent: 'echo', depends_on: ['triage'], when: 'triage.bug' },
    { id: 'plain', subagent: 'echo', depends_on: [] }
  ]

  const open = () => foldDagEvent(null, started(BRANCHING))

  const completion = (files: DagRunCompletedEvent['payload']['files'], summary = {}): DagRunCompletedEvent => ({
    type: 'dag.run_completed',
    payload: { run_id: 'dag-1', dir: '/w/mas_dag/dag-1', summary: { total: 3, ...summary }, files }
  })

  const nodeOf = (run: ReturnType<typeof open>, id: string) => run?.nodes.find(n => n.id === id)

  it('keeps the reason, the selection and the guard', () => {
    const run = foldDagEvent(open(), {
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'fix', status: 'not_executed', reason: 'when_false' }
    })

    expect(nodeOf(run, 'fix')).toMatchObject({ reason: 'when_false', status: 'not_executed', when: 'triage.bug' })
  })

  it('learns from the start frame which node decides and which is guarded', () => {
    const run = open()

    expect(nodeOf(run, 'triage')).toMatchObject({ routes: ['docs', 'bug'], select: 'one' })
    expect(nodeOf(run, 'fix')?.when).toBe('triage.bug')
  })

  it('leaves a plain node with no branch key at all', () => {
    // A key present and undefined would still be a shape no ordinary run had.
    expect(nodeOf(open(), 'plain')).toStrictEqual({
      id: 'plain',
      subagent: 'echo',
      dependsOn: [],
      status: 'pending'
    })
  })

  it('keeps what a decision node selected, and the evidence it gave', () => {
    const run = foldDagEvent(open(), {
      type: 'dag.node_updated',
      payload: {
        run_id: 'dag-1',
        node: 'triage',
        status: 'completed',
        ended_at: 9,
        selected_routes: ['docs'],
        route_evidence: 'it asks how to configure the proxy'
      }
    })

    expect(nodeOf(run, 'triage')).toMatchObject({
      routeEvidence: 'it asks how to configure the proxy',
      selectedRoutes: ['docs'],
      status: 'completed'
    })
  })

  it('keeps an empty selection, which is a decision that chose nothing', () => {
    // Copied by presence, not truthiness: `[]` is a selection of nothing, and
    // dropping it would read as a decision never made.
    const run = foldDagEvent(open(), {
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'triage', status: 'completed', selected_routes: [] }
    })

    expect(nodeOf(run, 'triage')?.selectedRoutes).toStrictEqual([])
  })

  it('gives no selection to a node whose frame carried none', () => {
    const run = foldDagEvent(open(), updated('triage', 'completed'))

    expect(nodeOf(run, 'triage')).not.toHaveProperty('selectedRoutes')
    expect(nodeOf(run, 'triage')).not.toHaveProperty('routeEvidence')
    expect(nodeOf(run, 'triage')).not.toHaveProperty('reason')
  })

  it('does not lose the selection to a later frame that does not repeat it', () => {
    const decided = foldDagEvent(open(), {
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'triage', status: 'completed', selected_routes: [], route_evidence: 'none' }
    })
    const run = foldDagEvent(decided, updated('triage', 'completed'))

    expect(nodeOf(run, 'triage')).toMatchObject({ routeEvidence: 'none' })
    expect(nodeOf(run, 'triage')?.selectedRoutes).toStrictEqual([])
  })

  it('takes the reason, the selection and the evidence from the manifest, an empty selection included', () => {
    const run = foldDagEvent(
      open(),
      completion(
        [
          { node: 'triage', status: 'completed', selected_routes: [], route_evidence: 'no route applies' },
          { node: 'fix', status: 'not_executed', reason: 'when_unknown' },
          { node: 'plain', status: 'completed' }
        ],
        { completed: 2, not_executed: 1 }
      )
    )

    expect(nodeOf(run, 'triage')).toMatchObject({ routeEvidence: 'no route applies' })
    expect(nodeOf(run, 'triage')?.selectedRoutes).toStrictEqual([])
    expect(nodeOf(run, 'fix')).toMatchObject({ reason: 'when_unknown', status: 'not_executed' })
    expect(nodeOf(run, 'plain')).toStrictEqual({
      id: 'plain',
      subagent: 'echo',
      dependsOn: [],
      status: 'completed'
    })
  })

  it('counts the nodes a branch left out in the summary', () => {
    const run = foldDagEvent(
      open(),
      completion([{ node: 'fix', status: 'not_executed' }], { completed: 2, not_executed: 1 })
    )

    expect(run?.summary).toEqual({ total: 3, completed: 2, not_executed: 1 })
  })

  it('does not drop what the updated frames said when the manifest repeats none of it', () => {
    const live = foldDagEvent(
      foldDagEvent(open(), {
        type: 'dag.node_updated',
        payload: { run_id: 'dag-1', node: 'triage', status: 'completed', selected_routes: ['bug'], route_evidence: 'x' }
      }),
      {
        type: 'dag.node_updated',
        payload: { run_id: 'dag-1', node: 'fix', status: 'not_executed', reason: 'when_false' }
      }
    )
    const run = foldDagEvent(
      live,
      completion([
        { node: 'triage', status: 'completed' },
        { node: 'fix', status: 'not_executed' }
      ])
    )

    expect(nodeOf(run, 'triage')).toMatchObject({ routeEvidence: 'x', selectedRoutes: ['bug'] })
    expect(nodeOf(run, 'fix')?.reason).toBe('when_false')
  })

  it('does not share the selection array with the event it came from', () => {
    const selected = ['docs']
    const run = foldDagEvent(open(), {
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'triage', status: 'completed', selected_routes: selected }
    })

    selected.push('bug')

    expect(nodeOf(run, 'triage')?.selectedRoutes).toEqual(['docs'])
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

  describe('on a branching run', () => {
    const BRANCHED: DagRunSnapshot['files'] = [
      {
        node: 'triage',
        status: 'completed',
        subagent: 'echo',
        depends_on: [],
        routes: ['docs', 'bug'],
        select: 'many',
        selected_routes: [],
        route_evidence: 'no route applies'
      },
      {
        node: 'fix',
        status: 'not_executed',
        subagent: 'echo',
        depends_on: ['triage'],
        when: 'triage.bug',
        reason: 'when_false'
      },
      { node: 'plain', status: 'completed', subagent: 'echo', depends_on: [] }
    ]

    it('carries what each node asked and what was decided, an empty selection included', () => {
      const run = foldDagSnapshot(null, snapshot(BRANCHED))

      expect(run.nodes[0]).toMatchObject({
        routeEvidence: 'no route applies',
        routes: ['docs', 'bug'],
        select: 'many'
      })
      expect(run.nodes[0]!.selectedRoutes).toStrictEqual([])
      expect(run.nodes[1]).toMatchObject({ reason: 'when_false', status: 'not_executed', when: 'triage.bug' })
    })

    it('leaves a plain node with no branch key at all', () => {
      expect(foldDagSnapshot(null, snapshot(BRANCHED)).nodes[2]).toStrictEqual({
        id: 'plain',
        subagent: 'echo',
        dependsOn: [],
        status: 'completed'
      })
    })

    it('counts the nodes a branch left out in the summary', () => {
      const run = foldDagSnapshot(null, snapshot(BRANCHED, { summary: { total: 3, completed: 2, not_executed: 1 } }))

      expect(run.summary).toEqual({ total: 3, completed: 2, not_executed: 1 })
    })

    it('keeps the outcome the live frames reported when an unfinished run has none on disk yet', () => {
      // A run still going has no manifest, so `dag.get` answers from the
      // registry, where a write is best-effort and can be dropped. Letting it win
      // outright would blank a selection and a reason that were reading fine a
      // moment ago.
      const live = foldDagEvent(
        foldDagEvent(
          foldDagEvent(
            null,
            started([
              { id: 'triage', subagent: 'echo', depends_on: [], routes: ['docs', 'bug'] },
              { id: 'fix', subagent: 'echo', depends_on: ['triage'], when: 'triage.bug' }
            ])
          ),
          {
            type: 'dag.node_updated',
            payload: {
              run_id: 'dag-1',
              node: 'triage',
              status: 'completed',
              selected_routes: ['docs'],
              route_evidence: 'it asks how to configure the proxy'
            }
          }
        ),
        {
          type: 'dag.node_updated',
          payload: { run_id: 'dag-1', node: 'fix', status: 'not_executed', reason: 'when_false' }
        }
      )

      const run = foldDagSnapshot(
        live,
        snapshot(
          [
            { node: 'triage', status: 'completed', routes: ['docs', 'bug'], select: 'one' },
            { node: 'fix', status: 'not_executed', when: 'triage.bug' }
          ],
          { finalized: false }
        )
      )

      expect(run.nodes[0]).toMatchObject({
        routeEvidence: 'it asks how to configure the proxy',
        selectedRoutes: ['docs']
      })
      expect(run.nodes[1]!.reason).toBe('when_false')
    })

    it('renders a not-executed node with no reason as it came, without inventing one', () => {
      const run = foldDagSnapshot(null, snapshot([{ node: 'fix', status: 'not_executed' }], { finalized: false }))

      expect(run.nodes[0]).toStrictEqual({ id: 'fix', subagent: '', dependsOn: [], status: 'not_executed' })
    })

    it('takes the disk over what the live frames said when both have an answer', () => {
      const live = foldDagEvent(
        foldDagEvent(null, started([{ id: 'triage', subagent: 'echo', depends_on: [], routes: ['docs', 'bug'] }])),
        {
          type: 'dag.node_updated',
          payload: { run_id: 'dag-1', node: 'triage', status: 'completed', selected_routes: ['docs'] }
        }
      )
      const run = foldDagSnapshot(
        live,
        snapshot([{ node: 'triage', status: 'completed', selected_routes: ['bug'], route_evidence: 'later' }])
      )

      expect(run.nodes[0]).toMatchObject({ routeEvidence: 'later', selectedRoutes: ['bug'] })
    })
  })
})
