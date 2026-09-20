import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { setCurrent } from '../../lib/session'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { NodeRecord, TaskFile, TaskNode, TaskRow, TasksSource } from './types'

const emptyRecord: NodeRecord = { dispatch: null, steps: [], answer: null, outputTruncated: false }

const row = (over: Partial<TaskRow> & Pick<TaskRow, 'id' | 'kind' | 'status'>): TaskRow => ({
  task_summary: null, started_at: null, ended_at: null, agent: null, handle: null,
  counts: { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0 },
  nodes: [],
  ...over,
})

let rows: TaskRow[] = []
let stopped: TaskRow[] = []

const source: TasksSource = {
  list: async (key) => (key ? rows : []),
  one: async (kind, id) => rows.find((r) => r.kind === kind && r.id === id) || null,
  stop: async (r) => { stopped.push(r); return true },
  node: async () => emptyRecord,
  roster: async () => [],
}

beforeEach(() => {
  rows = []
  stopped = []
  store._resetForTests()
  /* Reset every test to the default answers: a case that pends and later
     resolves `list`/`one` itself (the session-switch guards below) must not
     leak its stand-in into the next one. */
  source.list = async (key) => (key ? rows : [])
  source.one = async (kind, id) => rows.find((r) => r.kind === kind && r.id === id) || null
  setSources({ tasks: source })
  setCurrent('s1')
})

afterEach(() => {
  resetSources()
  setCurrent(null)
})

describe('refresh', () => {
  it('reads the session on screen', async () => {
    rows = [row({ id: 'a', kind: 'dag', status: 'running' })]
    await store.refresh()
    expect(store.rows()).toHaveLength(1)
  })

  it('answers empty rather than asking, with no open conversation', async () => {
    setCurrent(null)
    rows = [row({ id: 'a', kind: 'dag', status: 'running' })]
    await store.refresh()
    expect(store.rows()).toEqual([])
  })

  /* Asked for one conversation, answered into whichever is open now: the
     reader can click another session while this is in flight. The same guard
     the agents lists, the deliveries shelf and the desk replay use. */
  it('drops an answer for a conversation the reader has already left', async () => {
    let release: (r: TaskRow[]) => void = () => {}
    source.list = () => new Promise((res) => { release = res })

    const pending = store.refresh()
    setCurrent('s2')
    release([row({ id: 'a', kind: 'dag', status: 'running' })])
    await pending

    expect(store.rows()).toEqual([])
    /* Not just empty rows: `loaded` must stay false too, or nothing ever asks
       again for s2 -- the panel would read as "read, and empty" forever. */
    expect(store.get().loaded).toBe(false)
  })
})

describe('running / settled', () => {
  it('splits by status, not by a three-state guess', () => {
    store.set((prev) => ({
      ...prev,
      rows: [
        row({ id: 'a', kind: 'dag', status: 'running' }),
        row({ id: 'b', kind: 'dag', status: 'interrupted' }),
        row({ id: 'c', kind: 'dag', status: 'cancelled' }),
      ],
      loaded: true,
    }))
    expect(store.running().map((r) => r.id)).toEqual(['a'])
    expect(store.settled().map((r) => r.id)).toEqual(['b', 'c'])
  })
})

describe('byKey', () => {
  it('addresses a row by (kind, id), not by id alone', () => {
    store.set((prev) => ({
      ...prev,
      rows: [row({ id: 'x', kind: 'spawn', status: 'running' }), row({ id: 'x', kind: 'dag', status: 'failed' })],
      loaded: true,
    }))
    expect(store.byKey('spawn', 'x')?.status).toBe('running')
    expect(store.byKey('dag', 'x')?.status).toBe('failed')
  })
})

describe('node selection', () => {
  it('is per pane: picking in one leaves another pane on what it was showing', () => {
    store.pickNode('task:dag:a', 'n1')
    store.pickNode('task:dag:b', 'n2')
    expect(store.nodeOf('task:dag:a')).toBe('n1')
    expect(store.nodeOf('task:dag:b')).toBe('n2')
    store.pickNode('task:dag:a', null)
    expect(store.nodeOf('task:dag:a')).toBeNull()
    expect(store.nodeOf('task:dag:b')).toBe('n2')
  })
})

describe('stop', () => {
  it('calls the source and reconciles the row from its answer', async () => {
    const running = row({ id: 'r1', kind: 'dag', status: 'running' })
    store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
    rows = [{ ...running, status: 'cancelled' }]
    await store.stop(running)
    expect(stopped).toEqual([running])
    expect(store.byKey('dag', 'r1')?.status).toBe('cancelled')
  })

  /* stop() reconciles through the same function refresh does, so it inherits
     the same guard: a stop button pressed on a row the reader has since left
     must not write that conversation's answer into the one they switched to. */
  it('does not apply a reconcile that lands after the reader switched conversations', async () => {
    const running = row({ id: 'r1', kind: 'dag', status: 'running' })
    store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
    let releaseStop: (ok: boolean) => void = () => {}
    let releaseOne: (r: TaskRow | null) => void = () => {}
    source.stop = () => new Promise((res) => { releaseStop = res })
    source.one = () => new Promise((res) => { releaseOne = res })

    const pending = store.stop(running)
    releaseStop(true)
    /* One microtask tick resumes stop() past `await src.stop`, into
       reconcile() far enough to capture the session key and call src.one --
       synchronously, before reconcile suspends on that call. */
    await Promise.resolve()
    setCurrent('s2')
    releaseOne({ ...running, status: 'cancelled' })
    await pending

    expect(store.byKey('dag', 'r1')?.status).toBe('running')
  })
})

describe('fileDiffChange', () => {
  const node = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id'>): TaskNode => ({
    agent: 'raven', status: 'completed', depends_on: [], files: [], ...over,
  })

  /* The pane's own `openNodeDiff` (TasksPage.tsx) builds this same shape from
     the same inputs; this is the verb the desk's diff tab shares with it
     rather than re-fetching the record on its own. */
  it('reads the patch body from the node record, keyed to the task/node/path', async () => {
    source.node = async () => ({
      dispatch: null,
      steps: [{
        kind: 'tool', id: 't1', name: 'edit_file',
        args: JSON.stringify({ path: '/w/a.py', old_text: 'a', new_text: 'ab' }),
        result: null, ok: true,
      }],
      answer: null, outputTruncated: false,
    })
    const taskRow = row({ id: 'r1', kind: 'dag', status: 'completed' })
    const file: TaskFile = { path: '/w/a.py', op: 'edit', add: 1, del: 0 }
    const change = await store.fileDiffChange(taskRow, node({ node_id: 'n1' }), file)

    expect(change.key).toBe('task:dag:r1:n1:/w/a.py')
    expect(change.name).toBe('a.py')
    expect(change.kind).toBe('edit')
    expect(change.add).toBe(1)
    expect(change.del).toBe(0)
    expect(change.hunks).toHaveLength(1)
  })

  it('answers with no hunks rather than throwing when there is no source', async () => {
    resetSources()
    const taskRow = row({ id: 'r1', kind: 'dag', status: 'completed' })
    const file: TaskFile = { path: '/w/a.py', op: 'write', add: 3, del: 0 }
    const change = await store.fileDiffChange(taskRow, node({ node_id: 'n1' }), file)
    expect(change.hunks).toEqual([])
  })
})

describe('node fold memory', () => {
  it('is undefined until the reader touches a fold, then remembers per node', () => {
    expect(store.foldOf('dag:r1:n1', 'proc')).toBeUndefined()
    store.setFold('dag:r1:n1', 'proc', false)
    expect(store.foldOf('dag:r1:n1', 'proc')).toBe(false)
    /* A different node's own fold of the same name is untouched. */
    expect(store.foldOf('dag:r1:n2', 'proc')).toBeUndefined()
  })

  it('keeps two folds on the same node independent', () => {
    store.setFold('dag:r1:n1', 'wide', true)
    store.setFold('dag:r1:n1', 'think:0', false)
    expect(store.foldOf('dag:r1:n1', 'wide')).toBe(true)
    expect(store.foldOf('dag:r1:n1', 'think:0')).toBe(false)
  })
})

describe('live event consumers', () => {
  it('onRunStarted inserts a new row', () => {
    store.onRunStarted({ run_id: '20260717T031500123456Z-1a2b3c4d', nodes: [{ id: 'a', subagent: 'raven', depends_on: [] }] })
    expect(store.byKey('dag', '20260717T031500123456Z-1a2b3c4d')).not.toBeNull()
  })

  it('onNodeUpdated bumps that node\'s own version, once per event', () => {
    expect(store.nodeVersion('dag', 'r1', 'n1')).toBe(0)
    store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running' })
    expect(store.nodeVersion('dag', 'r1', 'n1')).toBe(1)
    store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c2' })
    expect(store.nodeVersion('dag', 'r1', 'n1')).toBe(2)
    /* A different node's version is untouched by another node's event. */
    expect(store.nodeVersion('dag', 'r1', 'n2')).toBe(0)
  })

  it('a terminal frame triggers a reconcile through one(kind, id)', async () => {
    store.set((prev) => ({ ...prev, rows: [row({ id: 'r1', kind: 'dag', status: 'running' })], loaded: true }))
    rows = [row({ id: 'r1', kind: 'dag', status: 'completed', task_summary: 'reconciled' })]
    store.onRunCompleted({ run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'a', status: 'completed' }] })
    /* The reconcile is async; give its promise a turn to land. */
    await Promise.resolve()
    await Promise.resolve()
    expect(store.byKey('dag', 'r1')?.task_summary).toBe('reconciled')
  })

  /* Independent of any React or effect timing: a terminal event schedules
     reconcile, the reader switches conversations before it answers, and the
     answer must not prepend or overwrite a row into the session now open. */
  it("a terminal frame's reconcile does not land in the next conversation", async () => {
    store.set((prev) => ({ ...prev, rows: [row({ id: 'r1', kind: 'dag', status: 'running' })], loaded: true }))
    let release: (r: TaskRow | null) => void = () => {}
    source.one = () => new Promise((res) => { release = res })

    store.onRunCompleted({ run_id: 'r1', dir: '/d', summary: {}, files: [{ node: 'a', status: 'completed' }] })
    setCurrent('s2')
    release(row({ id: 'r1', kind: 'dag', status: 'completed', task_summary: 'reconciled' }))
    await Promise.resolve()
    await Promise.resolve()

    expect(store.byKey('dag', 'r1')?.task_summary).not.toBe('reconciled')
  })
})

/* The read that fills the panel and the frames that move it race: the server
   answers from the state it held when asked, so a run dispatched during the
   round trip is not in that answer. Before this was guarded, the answer
   replaced the whole list and took the run's own row with it -- and since
   `dag.run_started` fires once and no other dag reducer can insert, the run
   stayed invisible for the whole of its life. The desk pane and the strip
   above the composer read the same rows, so both went blank together. */
describe('a frame that lands while the list read is in flight', () => {
  const node = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id'>): TaskNode => ({
    agent: 'raven', status: 'completed', depends_on: [], files: [], ...over,
  })
  const started = (runId: string): Parameters<typeof store.onRunStarted>[0] => ({
    run_id: runId,
    task_summary: 'a playbook',
    nodes: [{ id: 'n1', subagent: 'Raven-Research', depends_on: [] }],
  })

  it('keeps the row a dag run inserted while the read was out', async () => {
    let release: (r: TaskRow[]) => void = () => {}
    source.list = () => new Promise((res) => { release = res })

    const inFlight = store.refresh()
    await Promise.resolve()
    store.onRunStarted(started('run-1'))
    release([])
    await inFlight

    expect(store.rows().map((r) => r.id)).toEqual(['run-1'])
    expect(store.get().loaded).toBe(true)
  })

  /* The answer is the older copy of any row a frame moved after the read was
     asked, so it does not get to speak for that row: a node the reader has
     already watched finish must not go back to running, and `node_updated`
     schedules no reconcile that would put it right again. */
  it('does not let the answer undo a frame that moved the same row', async () => {
    store.set((prev) => ({
      ...prev,
      rows: [row({ id: 'run-3', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })] })],
      loaded: true,
    }))
    let release: (r: TaskRow[]) => void = () => {}
    source.list = () => new Promise((res) => { release = res })

    const inFlight = store.refresh()
    await Promise.resolve()
    store.onNodeUpdated({ run_id: 'run-3', node: 'n1', status: 'completed' })
    /* What the server held when it was asked: the node still running. */
    release([row({ id: 'run-3', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })] })])
    await inFlight

    expect(store.byKey('dag', 'run-3')?.nodes[0]?.status).toBe('completed')
    expect(store.byKey('dag', 'run-3')?.status).toBe('completed')
  })

  /* And it still speaks for every row no frame touched in that window, which
     is where the tokens, the files and the final error text come from. */
  it("takes the server's copy of a row the frames left alone", async () => {
    store.set((prev) => ({
      ...prev,
      rows: [row({ id: 'quiet', kind: 'dag', status: 'running' })],
      loaded: true,
    }))
    let release: (r: TaskRow[]) => void = () => {}
    source.list = () => new Promise((res) => { release = res })

    const inFlight = store.refresh()
    await Promise.resolve()
    store.onRunStarted(started('noisy'))
    release([
      row({ id: 'quiet', kind: 'dag', status: 'completed', task_summary: 'from the server' }),
    ])
    await inFlight

    expect(store.byKey('dag', 'quiet')?.status).toBe('completed')
    expect(store.byKey('dag', 'quiet')?.task_summary).toBe('from the server')
    expect(store.byKey('dag', 'noisy')).not.toBeNull()
  })

  /* The plain case still replaces rather than merges: with no frame in the
     gap, a row the answer dropped is a row that is gone. */
  it('still drops a row the answer no longer carries when nothing raced it', async () => {
    store.set((prev) => ({ ...prev, rows: [row({ id: 'old', kind: 'dag', status: 'running' })], loaded: true }))
    rows = []

    await store.refresh()

    expect(store.rows()).toEqual([])
  })
})
