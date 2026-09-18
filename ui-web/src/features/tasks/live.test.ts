import { describe, expect, it, vi } from 'vitest'

import {
  applyNodeUpdated, applyRunCompleted, applyRunReplanned, applyRunStarted, applySubagentStatus,
  countsOf, deriveStatus, startOfRunId,
} from './live'

import type { TaskNode, TaskRow } from './types'

const node = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'status'>): TaskNode => ({
  agent: 'raven', instance: null, depends_on: [], started_at: null, ended_at: null, error: null,
  tokens_in: null, tokens_out: null, tool_call_count: null, tool_failure_count: null, has_output: null,
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
})

describe('applyNodeUpdated', () => {
  const base: TaskRow[] = [{
    id: 'r1', kind: 'dag', task_summary: null, status: 'running', agent: null, handle: null,
    started_at: 1000, ended_at: null,
    counts: countsOf([node({ node_id: 'a', status: 'running' }), node({ node_id: 'b', status: 'pending' })]),
    nodes: [node({ node_id: 'a', status: 'running', started_at: 1000 }), node({ node_id: 'b', status: 'pending' })],
  }]

  it('moves the named node and recomputes the row status', () => {
    const rows = applyNodeUpdated(base, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    const row = rows[0]!
    expect(row.nodes[0]).toMatchObject({ status: 'completed', ended_at: 2000 })
    /* b is still pending, so the row as a whole is still running. */
    expect(row.status).toBe('running')
  })

  it('leaves the timestamps alone when the frame carries none, as a skip does', () => {
    const rows = applyNodeUpdated(base, { run_id: 'r1', node: 'b', status: 'skipped' })
    expect(rows[0]!.nodes[1]).toMatchObject({ status: 'skipped', started_at: null, ended_at: null })
  })

  it('replaying the same frame lands on the same rows', () => {
    const once = applyNodeUpdated(base, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    const twice = applyNodeUpdated(once, { run_id: 'r1', node: 'a', status: 'completed', ended_at: 2000 })
    expect(twice).toEqual(once)
  })

  it('does nothing for a run this page holds no row for', () => {
    expect(applyNodeUpdated(base, { run_id: 'other', node: 'a', status: 'completed' })).toEqual(base)
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
