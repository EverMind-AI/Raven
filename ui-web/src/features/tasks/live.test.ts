import { describe, expect, it, vi } from 'vitest'

import {
  applyNodeUpdated, applyRunCompleted, applyRunReplanned, applyRunStarted, applySubagentStatus,
  carryOutcome, countsOf, deriveStatus, startOfRunId,
} from './live'

import type { RunCompletedPayload } from './live'
import type { TaskNode, TaskRow } from './types'

const node = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'status'>): TaskNode => ({
  agent: 'raven', instance: null, depends_on: [], started_at: null, ended_at: null, error: null,
  tokens_in: null, tokens_out: null, tool_call_count: null, has_output: null,
  prompt_template: null, node_summary: null, files: [],
  ...over,
})

describe('startOfRunId', () => {
  it('reads the UTC stamp a run id was minted with', () => {
    const ms = startOfRunId('20260717T031500123456Z-1a2b3c4d')
    expect(ms).toBe(Date.UTC(2026, 6, 17, 3, 15, 0, 123))
  })

  it('answers null for anything that is not that shape', () => {
    expect(startOfRunId('not-a-run-id')).toBeNull()
  })
})

describe('countsOf / deriveStatus', () => {
  it('counts every status, including one this run never saw', () => {
    const nodes = [node({ node_id: 'a', status: 'completed' }), node({ node_id: 'b', status: 'running' })]
    expect(countsOf(nodes)).toMatchObject({ total: 2, completed: 1, running: 1, pending: 0 })
  })

  it('any cancelled or skipped reads as cancelled, not as completed', () => {
    const nodes = [node({ node_id: 'a', status: 'completed' }), node({ node_id: 'b', status: 'skipped' })]
    expect(deriveStatus(nodes)).toBe('cancelled')
  })

  it('a suspended exception node reads the task as running', () => {
    expect(deriveStatus([node({ node_id: 'a', status: 'exception' })])).toBe('running')
  })

  it('failed outranks interrupted and cancelled', () => {
    const nodes = [
      node({ node_id: 'a', status: 'failed' }), node({ node_id: 'b', status: 'interrupted' }),
      node({ node_id: 'c', status: 'skipped' }),
    ]
    expect(deriveStatus(nodes)).toBe('failed')
  })
})

/* A node a branch left out never ran, so it is counted -- the total is the
   graph's, and it adds up -- and it moves nothing: an unselected branch is the
   run working as written, not a run that was stopped. */
describe('a node a branch left out', () => {
  const nodes = [node({ node_id: 'a', status: 'completed' }), node({ node_id: 'b', status: 'not_executed' })]

  it('is counted on its own and in the total', () => {
    expect(countsOf(nodes)).toStrictEqual({
      total: 2, pending: 0, running: 0, completed: 1, failed: 0, skipped: 0, cancelled: 0, interrupted: 0,
      exception: 0, not_executed: 1,
    })
  })

  it('keeps the task completed', () => {
    expect(deriveStatus(nodes)).toBe('completed')
    expect(deriveStatus([node({ node_id: 'b', status: 'not_executed' })])).toBe('completed')
  })

  it('hides neither a failure, a stop nor work still to do beside it', () => {
    const left = node({ node_id: 'x', status: 'not_executed' })
    expect(deriveStatus([left, node({ node_id: 'a', status: 'failed' })])).toBe('failed')
    expect(deriveStatus([left, node({ node_id: 'a', status: 'interrupted' })])).toBe('interrupted')
    expect(deriveStatus([left, node({ node_id: 'a', status: 'running' })])).toBe('running')
    expect(deriveStatus([left, node({ node_id: 'a', status: 'pending' })])).toBe('running')
    expect(deriveStatus([left, node({ node_id: 'a', status: 'skipped' })])).toBe('cancelled')
  })

  it('leaves the counts of a run that never branches exactly as they were', () => {
    const plain = [node({ node_id: 'a', status: 'completed' }), node({ node_id: 'b', status: 'running' })]
    expect(JSON.stringify(countsOf(plain))).toBe(
      '{"total":2,"pending":0,"running":1,"completed":1,"failed":0,"skipped":0,"cancelled":0,"interrupted":0,'
      + '"exception":0,"not_executed":0}',
    )
  })
})

describe('applyRunStarted', () => {
  const payload = {
    run_id: '20260717T031500123456Z-1a2b3c4d', task_summary: 'Ship the release',
    nodes: [
      { id: 'survey', subagent: 'Researcher', depends_on: [] },
      { id: 'merge', subagent: 'Writer', depends_on: ['survey'], instance: 'w1', node_summary: 'Merge the drafts' },
    ],
  }

  it('builds a pending-node row, timed off the run id', () => {
    const rows = applyRunStarted([], payload)
    expect(rows).toHaveLength(1)
    const [row] = rows
    expect(row!.status).toBe('running')
    expect(row!.started_at).toBe(startOfRunId(payload.run_id))
    expect(row!.nodes.map((n) => n.status)).toEqual(['pending', 'pending'])
    /* subagent -> agent, once, at the boundary. */
    expect(row!.nodes[1]!.agent).toBe('Writer')
    expect(row!.nodes[1]!.instance).toBe('w1')
    expect(row!.nodes[1]!.node_summary).toBe('Merge the drafts')
  })

  it('is idempotent: the same run_id twice inserts once', () => {
    const once = applyRunStarted([], payload)
    const twice = applyRunStarted(once, payload)
    expect(twice).toHaveLength(1)
  })

  it("carries a decision node's routes and select, and a guarded node's guard", () => {
    const [row] = applyRunStarted([], {
      run_id: 'r1',
      nodes: [
        { id: 'triage', subagent: 'Router', depends_on: [], routes: ['question', 'bug'], select: 'one' },
        { id: 'answer', subagent: 'Writer', depends_on: ['triage'], when: 'triage.question' },
      ],
    })
    expect(row!.nodes[0]).toMatchObject({ routes: ['question', 'bug'], select: 'one' })
    expect(row!.nodes[0]).not.toHaveProperty('when')
    expect(row!.nodes[1]).toMatchObject({ when: 'triage.question' })
    expect(row!.nodes[1]).not.toHaveProperty('routes')
    expect(row!.nodes[1]).not.toHaveProperty('select')
  })

  it("leaves a decision's empty selection alone when the run's own frame is replayed", () => {
    const started = {
      run_id: 'r1',
      nodes: [{ id: 'triage', subagent: 'Router', depends_on: [], routes: ['a', 'b'], select: 'many' as const }],
    }
    const decided = applyNodeUpdated(applyRunStarted([], started), {
      run_id: 'r1', node: 'triage', status: 'completed', selected_routes: [],
    }).rows
    expect(applyRunStarted(decided, started)[0]!.nodes[0]!.selected_routes).toEqual([])
  })

  /* A key an event never carried is not a key at all: a row built from a run
     that never branches is the row it always was, down to its keys. */
  it('gives a node with no branch fields exactly the keys it always had', () => {
    const [row] = applyRunStarted([], payload)
    expect(Object.keys(row!.nodes[0]!).sort()).toEqual([
      'agent', 'depends_on', 'ended_at', 'error', 'files', 'has_output', 'instance', 'node_id', 'node_summary',
      'prompt_template', 'started_at', 'status', 'tokens_in', 'tokens_out', 'tool_call_count',
    ])
  })
})

describe('applyNodeUpdated', () => {
  const base: TaskRow[] = [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([node({ node_id: 'a', status: 'running' }), node({ node_id: 'b', status: 'pending' })]),
    nodes: [node({ node_id: 'a', status: 'running', started_at: 1000 }), node({ node_id: 'b', status: 'pending' })],
  }]

  it('moves the named node and recomputes the row status', () => {
    const { rows } = applyNodeUpdated(base, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    const row = rows[0]!
    expect(row.nodes[0]).toMatchObject({ status: 'completed', ended_at: 2000 })
    /* b is still pending, so the row as a whole is still running. */
    expect(row.status).toBe('running')
  })

  it('leaves the timestamps alone when the frame carries none, as a skip does', () => {
    const { rows } = applyNodeUpdated(base, { run_id: 'r1', node: 'b', status: 'skipped' })
    expect(rows[0]!.nodes[1]).toMatchObject({ status: 'skipped', started_at: null, ended_at: null })
  })

  it('replaying the same frame lands on the same rows', () => {
    const once = applyNodeUpdated(base, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    const twice = applyNodeUpdated(once.rows, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    expect(twice.rows).toEqual(once.rows)
  })

  it('does nothing for a run this page holds no row for', () => {
    const { rows, refetch } = applyNodeUpdated(base, { run_id: 'other', node: 'a', status: 'completed' })
    expect(rows).toEqual(base)
    expect(refetch).toBeUndefined()
  })

  /* The frame carries no usage; the runner sets the node's account aside as
     the node settles, and only a read brings it. */
  it("asks for a reconcile on a node's own terminal frame, and not before", () => {
    expect(applyNodeUpdated(base, { run_id: 'r1', node: 'a', status: 'running', tool_call_id: 'c1' }).refetch).toBeUndefined()
    /* `interrupted` is the reader's own word for a run the gateway lost, never
       a frame's, so the wire type does not carry it. */
    for (const status of ['completed', 'failed', 'skipped', 'cancelled', 'exception', 'not_executed'] as const) {
      expect(applyNodeUpdated(base, { run_id: 'r1', node: 'a', status }).refetch).toEqual({ kind: 'dag', id: 'r1' })
    }
  })
})

/* The two frames that say how a branch came out: a decision node's own finish
   frame carries what it selected, and a node a branch left out is announced
   with the reason it was. Both are copied field by field and only when the frame
   carries them -- an empty `selected_routes` is a decision that chose nothing,
   which is not the same as a node that decided nothing. */
describe('applyNodeUpdated on a branch', () => {
  const base: TaskRow[] = [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([
      node({ node_id: 'triage', status: 'running' }), node({ node_id: 'answer', status: 'pending' }),
      node({ node_id: 'fix', status: 'pending' }),
    ]),
    nodes: [
      node({ node_id: 'triage', status: 'running', started_at: 1000 }),
      node({ node_id: 'answer', status: 'pending', depends_on: ['triage'] }),
      node({ node_id: 'fix', status: 'pending', depends_on: ['triage'] }),
    ],
  }]

  it("stores a decision's selection and the evidence for it from its finish frame", () => {
    const { rows } = applyNodeUpdated(base, {
      run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000,
      selected_routes: ['question'], route_evidence: 'The user asked how to reset a password.',
    })
    expect(rows[0]!.nodes[0]).toMatchObject({
      status: 'completed', selected_routes: ['question'], route_evidence: 'The user asked how to reset a password.',
    })
  })

  it('keeps an empty selection: a decision that chose nothing has still decided', () => {
    const { rows } = applyNodeUpdated(base, {
      run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000, selected_routes: [],
    })
    expect(rows[0]!.nodes[0]!.selected_routes).toEqual([])
    expect(Array.isArray(rows[0]!.nodes[0]!.selected_routes)).toBe(true)
  })

  it('writes no branch key at all for a frame that carries none', () => {
    const { rows } = applyNodeUpdated(base, { run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000 })
    const keys = Object.keys(rows[0]!.nodes[0]!)
    expect(keys).not.toContain('selected_routes')
    expect(keys).not.toContain('route_evidence')
    expect(keys).not.toContain('reason')
  })

  it('does not lose a selection to a later frame that says nothing about it', () => {
    const decided = applyNodeUpdated(base, {
      run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000, selected_routes: [],
    }).rows
    const later = applyNodeUpdated(decided, { run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000 }).rows
    expect(later[0]!.nodes[0]!.selected_routes).toEqual([])
  })

  it('marks a node a branch left out with the reason it was, and asks for a reconcile', () => {
    const result = applyNodeUpdated(base, { run_id: 'r1', node: 'fix', status: 'not_executed', reason: 'when_false' })
    expect(result.rows[0]!.nodes[2]).toMatchObject({ status: 'not_executed', reason: 'when_false' })
    expect(result.rows[0]!.counts.not_executed).toBe(1)
    expect(result.refetch).toEqual({ kind: 'dag', id: 'r1' })
  })

  it('reads the task as completed once only an unselected branch is left', () => {
    const decided = applyNodeUpdated(base, {
      run_id: 'r1', node: 'triage', status: 'completed', ended_at: 2000, selected_routes: ['question'],
    }).rows
    const answered = applyNodeUpdated(decided, { run_id: 'r1', node: 'answer', status: 'completed', ended_at: 3000 }).rows
    expect(answered[0]!.status).toBe('running')
    const left = applyNodeUpdated(answered, { run_id: 'r1', node: 'fix', status: 'not_executed', reason: 'when_false' }).rows
    expect(left[0]!.status).toBe('completed')
    expect(left[0]!.counts).toMatchObject({ total: 3, completed: 2, not_executed: 1 })
  })

  it('replaying the same frame lands on the same rows', () => {
    const frame = {
      run_id: 'r1', node: 'triage', status: 'completed' as const, ended_at: 2000,
      selected_routes: [] as string[], route_evidence: 'nothing applied',
    }
    const once = applyNodeUpdated(base, frame)
    const twice = applyNodeUpdated(once.rows, frame)
    expect(twice.rows).toEqual(once.rows)
  })
})

describe('applyRunCompleted', () => {
  const base: TaskRow[] = [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([node({ node_id: 'a', status: 'running' })]),
    nodes: [node({ node_id: 'a', status: 'running' })],
  }]

  it('folds each named file into its node and asks for a reconcile', () => {
    const result = applyRunCompleted(base, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'a', status: 'completed' }],
    })
    expect(result.rows[0]!.status).toBe('completed')
    expect(result.refetch).toEqual({ kind: 'dag', id: 'r1' })
  })

  it('with no files at all -- the hard-cancel shape -- ends the row cancelled outright', () => {
    const result = applyRunCompleted(base, { run_id: 'r1', dir: '/d', summary: { total: 1 }, files: [] })
    expect(result.rows[0]!.status).toBe('cancelled')
  })

  it('does nothing for a run this page holds no row for', () => {
    const result = applyRunCompleted(base, { run_id: 'other', dir: '/d', summary: {}, files: [] })
    expect(result.rows).toEqual(base)
    expect(result.refetch).toBeUndefined()
  })

  it('replaying the same completion frame keeps the first ended_at rather than restamping it', () => {
    vi.useFakeTimers()
    vi.setSystemTime(5000)
    const once = applyRunCompleted(base, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'a', status: 'completed' }],
    })
    vi.setSystemTime(9000)
    const twice = applyRunCompleted(once.rows, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'a', status: 'completed' }],
    })
    expect(twice.rows[0]!.ended_at).toBe(once.rows[0]!.ended_at)
    vi.useRealTimers()
  })
})

/* The closing frame repeats the outcome of every node it names, and a client
   folding it must neither drop what the node frames already said nor swallow an
   empty selection. */
describe('applyRunCompleted on a branch', () => {
  const row = (nodes: TaskNode[]): TaskRow[] => [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null, counts: countsOf(nodes), nodes,
  }]
  const bare = row([
    node({ node_id: 'triage', status: 'running' }), node({ node_id: 'fix', status: 'pending', depends_on: ['triage'] }),
  ])
  const told = row([
    node({ node_id: 'triage', status: 'completed', selected_routes: ['question'], route_evidence: 'asked a question' }),
    node({ node_id: 'fix', status: 'not_executed', reason: 'when_false', depends_on: ['triage'] }),
  ])

  it("folds a decision's selection and a left-out node's reason out of the files", () => {
    const result = applyRunCompleted(bare, {
      run_id: 'r1', dir: '/d', summary: { total: 2, completed: 1, not_executed: 1 },
      files: [
        { node: 'triage', status: 'completed', selected_routes: ['question'], route_evidence: 'asked a question' },
        { node: 'fix', status: 'not_executed', reason: 'when_unknown' },
      ],
    })
    expect(result.rows[0]!.nodes[0]).toMatchObject({ selected_routes: ['question'], route_evidence: 'asked a question' })
    expect(result.rows[0]!.nodes[1]).toMatchObject({ status: 'not_executed', reason: 'when_unknown' })
    expect(result.rows[0]!.counts).toMatchObject({ total: 2, completed: 1, not_executed: 1 })
    expect(result.rows[0]!.status).toBe('completed')
  })

  it('keeps an empty selection out of the files', () => {
    const result = applyRunCompleted(bare, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'triage', status: 'completed', selected_routes: [] }],
    })
    expect(result.rows[0]!.nodes[0]!.selected_routes).toEqual([])
  })

  it('does not drop what the node frames said when the files say nothing of it', () => {
    const result = applyRunCompleted(told, {
      run_id: 'r1', dir: '/d', summary: {},
      files: [{ node: 'triage', status: 'completed' }, { node: 'fix', status: 'not_executed' }],
    })
    expect(result.rows[0]!.nodes[0]).toMatchObject({ selected_routes: ['question'], route_evidence: 'asked a question' })
    expect(result.rows[0]!.nodes[1]).toMatchObject({ reason: 'when_false' })
  })

  it('lets a file correct what a node frame said', () => {
    const result = applyRunCompleted(told, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'triage', status: 'completed', selected_routes: ['bug'] }],
    })
    expect(result.rows[0]!.nodes[0]!.selected_routes).toEqual(['bug'])
  })

  it('gives a node of a run that never branches the keys it had', () => {
    const before = Object.keys(bare[0]!.nodes[0]!).sort()
    const result = applyRunCompleted(bare, {
      run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'triage', status: 'completed' }],
    })
    expect(Object.keys(result.rows[0]!.nodes[0]!).sort()).toEqual(before)
  })

  it('replaying the same completion frame lands on the same rows', () => {
    const frame = {
      run_id: 'r1', dir: '/d', summary: {},
      files: [{ node: 'triage', status: 'completed' as const, selected_routes: [] as string[] }],
    }
    const once = applyRunCompleted(bare, frame)
    expect(applyRunCompleted(once.rows, frame).rows[0]!.nodes).toEqual(once.rows[0]!.nodes)
  })
})

/* While a run goes, `tasks.list` takes a decision's selection and a left-out
   node's reason from registry rows, a best-effort write that can be dropped,
   so a read taken then can answer those nodes without them. The frames already
   said both, and the read must not take them back. */
describe('carryOutcome', () => {
  const rowOf = (nodes: TaskNode[]): TaskRow => ({
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null, counts: countsOf(nodes), nodes,
  })
  const held = rowOf([
    node({ node_id: 'triage', status: 'completed', selected_routes: [], route_evidence: 'nothing applied' }),
    node({ node_id: 'fix', status: 'not_executed', reason: 'when_false' }),
  ])

  it('puts back what a read taken mid-run leaves out', () => {
    const got = rowOf([node({ node_id: 'triage', status: 'completed' }), node({ node_id: 'fix', status: 'not_executed' })])
    const carried = carryOutcome(held, got)
    expect(carried.nodes[0]).toMatchObject({ selected_routes: [], route_evidence: 'nothing applied' })
    expect(carried.nodes[1]).toMatchObject({ reason: 'when_false' })
  })

  it('carries an empty selection, which is a decision, as it carries any other', () => {
    const got = rowOf([node({ node_id: 'triage', status: 'completed' })])
    expect(carryOutcome(held, got).nodes[0]!.selected_routes).toEqual([])
  })

  it('takes what the read says wherever it says anything, an empty selection included', () => {
    const decided = rowOf([node({ node_id: 'triage', status: 'completed', selected_routes: ['bug'] })])
    const none = rowOf([node({ node_id: 'triage', status: 'completed', selected_routes: [] })])
    expect(carryOutcome(decided, none).nodes[0]!.selected_routes).toEqual([])
    expect(carryOutcome(held, decided).nodes[0]!.selected_routes).toEqual(['bug'])
  })

  it('leaves a node alone that is no longer in the state the outcome was about', () => {
    const got = rowOf([node({ node_id: 'triage', status: 'running' })])
    expect(carryOutcome(held, got).nodes[0]).not.toHaveProperty('selected_routes')
  })

  it('hands back the row it was given when there is nothing to carry', () => {
    const plainHeld = rowOf([node({ node_id: 'a', status: 'completed' })])
    const plainGot = rowOf([node({ node_id: 'a', status: 'completed' })])
    expect(carryOutcome(plainHeld, plainGot)).toBe(plainGot)
    expect(carryOutcome(undefined, plainGot)).toBe(plainGot)
  })
})

describe('applyRunReplanned', () => {
  const base: TaskRow[] = [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'failed', agent: null, handle: null,
    started_at: null, ended_at: null, counts: countsOf([]), nodes: [],
  }]

  it('marks the superseded run cancelled and links the successor', () => {
    const result = applyRunReplanned(base, { run_id: 'r1', replan_run_id: 'r2', from_node: 'a', reason: 'stuck' })
    expect(result.rows[0]!.status).toBe('cancelled')
    expect(result.rows[0]!.replan).toEqual({ run_id: 'r2', from_node: 'a', reason: 'stuck', started: true })
    expect(result.refetch).toEqual({ kind: 'dag', id: 'r1' })
  })
})

describe('applySubagentStatus', () => {
  it('files a pending row under the task id, with no call_id yet', () => {
    const result = applySubagentStatus([], { task_id: 't1', agent: 'raven', label: 'clean cache', status: 'pending' })
    expect(result.rows).toHaveLength(1)
    expect(result.rows[0]).toMatchObject({ id: 't1', kind: 'spawn', handle: 't1', status: 'running' })
    expect(result.refetch).toBeUndefined()
  })

  it('renames the row onto call_id once the run goes live, using instance over task_id for the handle', () => {
    const pending = applySubagentStatus([], { task_id: 't1', agent: 'raven', label: 'x', status: 'pending' }).rows
    const running = applySubagentStatus(pending, {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'running', instance: 'named',
    })
    expect(running.rows).toHaveLength(1)
    expect(running.rows[0]).toMatchObject({ id: 'n1', handle: 'named' })
  })

  it('folds the pending row into the record row a list read brought in under call_id', () => {
    const pending = applySubagentStatus([], { task_id: 't1', agent: 'raven', label: 'x', status: 'pending' }).rows
    /* The list answered while the pending row stood, so the same spawn is on
       the list under its record id as well. */
    const listed: TaskRow = {
      ...pending[0]!, id: 'n1', handle: 'named', nodes: [node({ node_id: 'n1', status: 'running', instance: 'named' })],
    }
    const running = applySubagentStatus([listed, ...pending], {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'running', instance: 'named',
    })
    expect(running.rows.map((r) => r.id)).toEqual(['n1'])
    expect(running.rows[0]).toMatchObject({ handle: 'named', status: 'running' })
  })

  it('drops a row that went from pending straight to cancelled -- it never got a disk record', () => {
    const pending = applySubagentStatus([], { task_id: 't1', agent: 'raven', label: 'x', status: 'pending' }).rows
    const cancelled = applySubagentStatus(pending, { task_id: 't1', agent: 'raven', label: 'x', status: 'cancelled' })
    expect(cancelled.rows).toEqual([])
  })

  it('keeps a row that was running when it is cancelled -- that one has a disk record', () => {
    const running = applySubagentStatus([], {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'running',
    }).rows
    const cancelled = applySubagentStatus(running, {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'cancelled',
    })
    expect(cancelled.rows).toHaveLength(1)
    expect(cancelled.rows[0]!.status).toBe('cancelled')
    expect(cancelled.refetch).toEqual({ kind: 'spawn', id: 'n1' })
  })

  it('asks for a reconcile on every terminal status, not on running', () => {
    const running = applySubagentStatus([], {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'running',
    })
    expect(running.refetch).toBeUndefined()
    const failed = applySubagentStatus(running.rows, {
      task_id: 't1', call_id: 'n1', agent: 'raven', label: 'x', status: 'failed',
    })
    expect(failed.refetch).toEqual({ kind: 'spawn', id: 'n1' })
  })

  it('replaying the same pending frame twice still files exactly one row', () => {
    const first = applySubagentStatus([], { task_id: 't1', agent: 'raven', label: 'x', status: 'pending' }).rows
    const second = applySubagentStatus(first, { task_id: 't1', agent: 'raven', label: 'x', status: 'pending' }).rows
    expect(second).toHaveLength(1)
  })
})

/* A node handed to another agent mid-run. The frames of the attempt that gave
   up can still be in flight when the next one starts, and the board names the
   agent running the node now -- so a frame has to be placed in its attempt
   before it may move anything. */
describe('a reassigned node', () => {
  const base = (): TaskRow[] => [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([node({ node_id: 'fix', status: 'running' })]),
    nodes: [node({ node_id: 'fix', status: 'running', agent: 'beta', instance: 'fix-minted', started_at: 20 })],
  }]
  const moved = (): TaskRow[] => applyNodeUpdated(base(), {
    run_id: 'r1', node: 'fix', status: 'running', started_at: 20, subagent: 'beta',
    instance: 'fix-minted', attempt_agents: ['alpha', 'beta'],
  }).rows

  it('moves identity, status and timing of one attempt together', () => {
    const { rows } = applyNodeUpdated(base(), {
      run_id: 'r1', node: 'fix', status: 'exception', started_at: 30,
      subagent: 'gamma', instance: 'fix-gamma', instance_auto: true,
      attempt_agents: ['alpha', 'beta', 'gamma'],
    })

    expect(rows[0]!.nodes[0]).toMatchObject({
      agent: 'gamma', ended_at: null, instance: 'fix-gamma', instance_auto: true, started_at: 30,
      status: 'exception', attempt_agents: ['alpha', 'beta', 'gamma'],
    })
  })

  it('keeps every attempt of an A -> B -> A reassignment', () => {
    const first = applyNodeUpdated(base(), {
      run_id: 'r1', node: 'fix', status: 'running', started_at: 20, subagent: 'beta',
      instance: 'fix-minted', attempt_agents: ['alpha', 'beta'],
    })
    const second = applyNodeUpdated(first.rows, {
      run_id: 'r1', node: 'fix', status: 'running', started_at: 40, subagent: 'alpha',
      instance: null, attempt_agents: ['alpha', 'beta', 'alpha'],
    })

    expect(second.rows[0]!.nodes[0]).toMatchObject({
      agent: 'alpha', instance: null, started_at: 40, attempt_agents: ['alpha', 'beta', 'alpha'],
    })
  })

  it('does not let a delayed frame from an earlier attempt overwrite the newer one', () => {
    const rows = moved()
    const { rows: next } = applyNodeUpdated(rows, {
      run_id: 'r1', node: 'fix', status: 'exception', started_at: 10, ended_at: 15,
      subagent: 'alpha', instance: 'fix-alpha', attempt_agents: ['alpha'],
    })

    expect(next[0]!.nodes[0]).toMatchObject({
      agent: 'beta', instance: 'fix-minted', started_at: 20, status: 'running',
      attempt_agents: ['alpha', 'beta'],
    })
  })

  it('treats a frame that names no attempt as older than the history the node carries', () => {
    const rows = moved()
    const { rows: next } = applyNodeUpdated(rows, { run_id: 'r1', node: 'fix', status: 'completed' })

    expect(next[0]!.nodes[0]).toMatchObject({ status: 'running', attempt_agents: ['alpha', 'beta'] })
  })

  it('clears the handle on a stateless retry', () => {
    const rows = moved()
    const { rows: next } = applyNodeUpdated(rows, {
      run_id: 'r1', node: 'fix', status: 'running', started_at: 40, subagent: 'gamma',
      instance: null, attempt_agents: ['alpha', 'beta', 'gamma'],
    })

    expect(next[0]!.nodes[0]).toMatchObject({ agent: 'gamma', instance: null })
  })

  it('takes the same attempt by the established status and timing policy', () => {
    const rows = moved()
    const { rows: next } = applyNodeUpdated(rows, {
      run_id: 'r1', node: 'fix', status: 'completed', ended_at: 60,
      subagent: 'beta', attempt_agents: ['alpha', 'beta'],
    })

    expect(next[0]!.nodes[0]).toMatchObject({ ended_at: 60, started_at: 20, status: 'completed' })
  })

  it('ignores a malformed history rather than half-adopting it', () => {
    const rows = moved()
    const { rows: next } = applyNodeUpdated(rows, {
      run_id: 'r1', node: 'fix', status: 'completed', subagent: 'gamma',
      attempt_agents: ['alpha', 7, ''] as never,
    })

    expect(next[0]!.nodes[0]).toMatchObject({ agent: 'beta', status: 'running' })
  })

  it('leaves a node that never moved with exactly the keys it had', () => {
    const { rows } = applyNodeUpdated(base(), { run_id: 'r1', node: 'fix', status: 'completed' })

    expect(rows[0]!.nodes[0]).toStrictEqual({
      node_id: 'fix', agent: 'beta', instance: 'fix-minted', status: 'completed', depends_on: [],
      started_at: 20, ended_at: null, error: null, tokens_in: null, tokens_out: null,
      tool_call_count: null, tool_failure_count: null, has_output: null, prompt_template: null,
      node_summary: null, files: [],
    })
  })
})

describe('applyRunCompleted on a reassigned node', () => {
  const base = (): TaskRow[] => [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([node({ node_id: 'fix', status: 'running' })]),
    nodes: [node({ node_id: 'fix', status: 'running', agent: 'beta', instance: 'fix-minted', started_at: 20 })],
  }]
  const live = (): TaskRow[] => {
    const { rows } = applyNodeUpdated(base(), {
      run_id: 'r1', node: 'fix', status: 'running', started_at: 20, subagent: 'beta',
      instance: 'fix-minted', attempt_agents: ['alpha', 'beta'],
    })
    return rows
  }
  const completed = (files: RunCompletedPayload['files']) => applyRunCompleted(live(), {
    run_id: 'r1', dir: '/w/r1', summary: { total: 1, completed: 1 }, files,
  })

  it('folds the attempt history and the finishing agent from the manifest', () => {
    const { rows } = completed([{
      node: 'fix', status: 'completed', subagent: 'gamma', instance: null,
      attempt_agents: ['alpha', 'beta', 'gamma'],
    }])

    /* The frame carries no node timings, so the attempt it introduces keeps none
       rather than being dated by the one the node moved past. */
    expect(rows[0]!.nodes[0]).toMatchObject({
      agent: 'gamma', attempt_agents: ['alpha', 'beta', 'gamma'], instance: null,
      started_at: null, status: 'completed',
    })
  })

  it('does not let a completion naming fewer attempts rewind the node', () => {
    const { rows } = completed([{
      node: 'fix', status: 'exception', subagent: 'alpha', attempt_agents: ['alpha'],
    }])

    expect(rows[0]!.nodes[0]).toMatchObject({ agent: 'beta', status: 'running', attempt_agents: ['alpha', 'beta'] })
  })

  it('leaves a node that never moved with exactly the keys it had', () => {
    const plain: TaskRow[] = [{
      ...base()[0]!,
      nodes: [node({ node_id: 'fix', status: 'running', agent: 'alpha', started_at: 20 })],
    }]
    const { rows } = applyRunCompleted(plain, {
      run_id: 'r1', dir: '/w/r1', summary: { total: 1, completed: 1 },
      files: [{ node: 'fix', status: 'completed' }],
    })

    expect(rows[0]!.nodes[0]).toStrictEqual({
      node_id: 'fix', agent: 'alpha', instance: null, status: 'completed', depends_on: [],
      started_at: 20, ended_at: null, error: null, tokens_in: null, tokens_out: null,
      tool_call_count: null, tool_failure_count: null, has_output: null, prompt_template: null,
      node_summary: null, files: [],
    })
  })
})
