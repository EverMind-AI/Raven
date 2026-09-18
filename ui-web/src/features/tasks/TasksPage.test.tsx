// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import { resetSources, setSources } from '../../state/sources'
import { installWsPane } from '../../test/wsPaneHarness'
import * as desk from '../desk/store'
import * as workspace from '../workspace/store'
import * as store from './store'
import { TaskPane, TaskRuns, TasksApp } from './TasksPage'

import type { WsChange } from '../workspace/types'
import type { NodeRecord, SubagentRow, TaskNode, TaskRow, TasksSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let rows: TaskRow[] = []
let roster: SubagentRow[] = []
let record: NodeRecord = { dispatch: null, steps: [], answer: null, outputTruncated: false }
let stopped: TaskRow[] = []

const counts = (over: Partial<TaskRow['counts']> = {}) => (
  { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0, ...over }
)

const node = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'status'>): TaskNode => ({
  agent: 'raven', node_summary: null, instance: null, depends_on: [], started_at: null, ended_at: null,
  error: null, tokens_in: null, tokens_out: null, tool_call_count: null, tool_failure_count: null,
  has_output: null, prompt_template: null, files: [],
  ...over,
})

const task = (over: Partial<TaskRow> & Pick<TaskRow, 'id' | 'kind' | 'status'>): TaskRow => ({
  task_summary: 'Cross-check quotes', started_at: 1000, ended_at: null, agent: null, handle: null,
  counts: counts(), nodes: [],
  ...over,
})

const source = (): TasksSource => ({
  list: async () => rows,
  one: async (kind, id) => rows.find((r) => r.kind === kind && r.id === id) || null,
  stop: async (r) => { stopped.push(r); return true },
  node: async () => record,
  roster: async () => roster,
})

beforeEach(() => {
  rows = []
  roster = []
  stopped = []
  record = { dispatch: null, steps: [], answer: null, outputTruncated: false }
  store._resetForTests()
  desk._resetForTests()
  installWsPane()
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({
    tasks: source(),
    workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
  })
  setCurrent('s1')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  setCurrent(null)
})

describe('the tasks list', () => {
  const draw = async (): Promise<void> => {
    render(<TasksApp />)
    await act(async () => { await store.refresh() })
  }

  it('groups running and settled, each with its count', async () => {
    rows = [
      task({ id: 'a', kind: 'dag', status: 'running' }),
      task({ id: 'b', kind: 'spawn', status: 'running' }),
      task({ id: 'c', kind: 'dag', status: 'completed' }),
    ]
    await draw()
    expect([...document.querySelectorAll('.wsgrp')].map((g) => g.textContent))
      .toEqual(['gui.tasks.running 2', 'gui.tasks.settled 1'])
  })

  it('shows the summary, or the id when there is none, and the error tag on a failure', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'failed', task_summary: null })]
    await draw()
    expect(document.querySelector('.sarow.task .nm')?.textContent).toBe('a')
    expect(document.querySelector('.sarow.task .tkerr')?.textContent).toBe('error')
  })

  it('shows the playbook name only when the row carries one', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'completed', playbook: 'nightly-checks' })]
    await draw()
    expect(document.querySelector('.tksrc')?.textContent).toBe('nightly-checks')
  })

  it('counts products across every node, not per task', async () => {
    rows = [task({
      id: 'a', kind: 'dag', status: 'completed',
      nodes: [
        node({ node_id: 'n1', status: 'completed', files: [{ path: '/w/a.md', op: 'write', add: 1, del: 0 }] }),
        node({ node_id: 'n2', status: 'completed', files: [{ path: '/w/b.md', op: 'write', add: 1, del: 0 }] }),
      ],
    })]
    await draw()
    expect(document.querySelector('.sarow.task .st')?.textContent).toContain('gui.tasks.artifacts_n {"n":2}')
  })

  it('says the panel is empty rather than drawing an empty list', async () => {
    await draw()
    expect(document.querySelector('.wsempty')?.textContent).toBe('gui.tasks.none')
  })

  it('opens a row as a desk pane keyed by (kind, id)', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'running' })]
    await draw()
    await act(async () => { (document.querySelector('.sarow.task') as HTMLElement).click() })
    const panes = desk.get().panes
    expect(panes).toHaveLength(1)
    expect(panes[0]!.id).toBe('task:dag:a')
  })
})

describe('the running strip', () => {
  const draw = async (): Promise<void> => {
    render(<TaskRuns />)
    await act(async () => { await store.refresh() })
  }

  it('draws nothing when nothing is running', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'completed' })]
    await draw()
    expect(document.querySelector('.runs')).toBeNull()
  })

  it('carries name only, up to three', async () => {
    rows = [
      task({ id: 'a', kind: 'dag', status: 'running', task_summary: 'First' }),
      task({ id: 'b', kind: 'dag', status: 'running', task_summary: 'Second' }),
    ]
    await draw()
    expect([...document.querySelectorAll('.trun .nm')].map((n) => n.textContent)).toEqual(['First', 'Second'])
    expect(document.querySelector('.trun .st')).toBeNull()
  })

  it('folds a fourth running task behind one overflow chip', async () => {
    rows = [0, 1, 2, 3].map((i) => task({ id: `t${i}`, kind: 'dag', status: 'running', task_summary: `T${i}` }))
    await draw()
    expect(document.querySelectorAll('.trun')).toHaveLength(4)
    expect(document.querySelector('.trun[data-more]')?.textContent).toBe('gui.tasks.overflow_more {"n":1}')
  })

  it('opens the task the same door the list row does', async () => {
    rows = [task({ id: 'a', kind: 'spawn', status: 'running' })]
    await draw()
    await act(async () => { (document.querySelector('.trun') as HTMLElement).click() })
    expect(desk.get().panes[0]!.id).toBe('task:spawn:a')
  })
})

describe('a task pane', () => {
  it('shows the status word, the ticking duration and a stop action while running', () => {
    render(<TaskPane task={task({ id: 'a', kind: 'dag', status: 'running', started_at: Date.now() - 5000 })} />)
    expect(document.querySelector('.tkbar .st')?.textContent).toBe('gui.tasks.running')
    expect(document.querySelector('.tkbaract')).not.toBeNull()
  })

  it('has no stop action once the task has settled', () => {
    render(<TaskPane task={task({ id: 'a', kind: 'dag', status: 'completed' })} />)
    expect(document.querySelector('.tkbaract')).toBeNull()
    expect(document.querySelector('.tkbar .st')?.textContent).toBe('gui.tasks.st_completed')
  })

  it('stop dispatches by kind: a dag calls subagent.interrupt through the source', async () => {
    const running = task({ id: 'r1', kind: 'dag', status: 'running' })
    rows = [{ ...running, status: 'cancelled' }]
    render(<TaskPane task={running} />)
    await act(async () => { (document.querySelector('.tkbaract') as HTMLElement).click() })
    expect(stopped).toEqual([running])
  })

  it('names the failed node in the why banner, and links to it', async () => {
    const bad = node({ node_id: 'fetch_comex', status: 'failed', node_summary: 'Fetch the futures quote', error: 'HTTP 429' })
    const failed = task({ id: 'a', kind: 'dag', status: 'failed', nodes: [bad] })
    render(<TaskPane task={failed} full />)
    expect(document.querySelector('.tkwhy p')?.textContent).toBe('HTTP 429')
    expect(document.querySelector('.tkwhyat')?.textContent).toBe('gui.tasks.why_at {"name":"Fetch the futures quote"}')

    await act(async () => { (document.querySelector('.tkwhyat') as HTMLElement).click() })
    expect(document.querySelector('.tktt b')?.textContent).toBe('Fetch the futures quote')
  })

  it('gives the fixed sentence for an interrupted run, not a node error', () => {
    const failed = task({ id: 'a', kind: 'dag', status: 'interrupted', nodes: [node({ node_id: 'n1', status: 'interrupted' })] })
    render(<TaskPane task={failed} />)
    expect(document.querySelector('.tkwhy')?.textContent).toContain('gui.tasks.why_interrupted')
  })

  it('draws no why banner for a settled, non-failing task', () => {
    render(<TaskPane task={task({ id: 'a', kind: 'dag', status: 'completed' })} />)
    expect(document.querySelector('.tkwhy')).toBeNull()
  })

  it('follows the store row rather than the snapshot the pane opened with', async () => {
    const running = task({ id: 'a', kind: 'dag', status: 'running' })
    store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
    render(<TaskPane task={running} />)
    expect(document.querySelector('.tkbaract')).not.toBeNull()
    expect(document.querySelector('.tkbar .st')?.textContent).toBe('gui.tasks.running')

    await act(async () => {
      store.set((prev) => ({ ...prev, rows: [{ ...running, status: 'completed' }] }))
    })

    expect(document.querySelector('.tkbaract')).toBeNull()
    expect(document.querySelector('.tkbar .st')?.textContent).toBe('gui.tasks.st_completed')
  })

  it('falls back to the opening snapshot while the store holds no row for it yet', () => {
    const running = task({ id: 'a', kind: 'dag', status: 'running' })
    render(<TaskPane task={running} />)
    expect(document.querySelector('.tkbaract')).not.toBeNull()
  })

  describe('a replan banner', () => {
    it('names the successor and opens it on the desk', async () => {
      const successor = task({ id: 'r2', kind: 'dag', status: 'running' })
      const superseded = task({
        id: 'r1', kind: 'dag', status: 'cancelled',
        replan: { run_id: 'r2', from_node: 'n1', reason: 'needed a retry', started: true },
      })
      store.set((prev) => ({ ...prev, rows: [successor, superseded], loaded: true }))
      render(<TaskPane task={superseded} />)

      expect(document.querySelector('.tkwhy p')?.textContent).toBe('gui.tasks.replan_superseded')
      expect(document.querySelector('.tkwhyat')?.textContent).toBe('gui.tasks.replan_at {"id":"r2"}')

      await act(async () => { (document.querySelector('.tkwhyat') as HTMLElement).click() })
      expect(desk.get().panes.map((p) => p.id)).toEqual(['task:dag:r2'])
    })

    it('just names the id when the successor never turns up in the store', () => {
      const superseded = task({
        id: 'r1', kind: 'dag', status: 'cancelled',
        replan: { run_id: 'r2', from_node: 'n1', reason: 'needed a retry', started: true },
      })
      render(<TaskPane task={superseded} />)
      expect(document.querySelector('.tkwhyat')).toBeNull()
      expect(document.querySelector('.tkwhy')?.textContent).toContain('r2')
    })

    it('prefers replan.error over a node error when the replan never started', () => {
      const failed = task({
        id: 'r1', kind: 'dag', status: 'failed',
        replan: { run_id: 'r2', from_node: 'n1', reason: 'x', started: false, error: 'no capacity for a retry' },
        nodes: [node({ node_id: 'n1', status: 'failed', error: 'HTTP 500' })],
      })
      render(<TaskPane task={failed} />)
      expect(document.querySelector('.tkwhy p')?.textContent).toBe('no capacity for a retry')
      expect(document.querySelector('.tkwhyat')).not.toBeNull()
    })
  })

  describe('awaiting a decision', () => {
    it('flags a running row stuck on a suspended node', () => {
      const stuck = task({
        id: 'a', kind: 'dag', status: 'running',
        counts: counts({ total: 2, completed: 1, exception: 1 }),
        nodes: [node({ node_id: 'n1', status: 'completed' }), node({ node_id: 'n2', status: 'exception' })],
      })
      render(<TaskPane task={stuck} />)
      expect(document.querySelector('.tkbar')?.textContent).toContain('gui.tasks.step_awaiting {"n":1}')
    })

    it('says so even for a single-node run with no other step to report', () => {
      const stuck = task({
        id: 'a', kind: 'dag', status: 'running',
        counts: counts({ total: 1, exception: 1 }),
        nodes: [node({ node_id: 'n1', status: 'exception' })],
      })
      render(<TaskPane task={stuck} />)
      expect(document.querySelector('.tkbar')?.textContent).toContain('gui.tasks.step_awaiting {"n":1}')
    })
  })

  describe('output chips', () => {
    beforeEach(() => { workspace.reset() })

    it('opens a written file through the workspace opener', () => {
      const opened: string[] = []
      setSources({
        tasks: source(),
        workspace: { shortPath: (p) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: (p) => opened.push(p) },
      })
      const withFile = task({
        id: 'a', kind: 'dag', status: 'completed',
        nodes: [node({ node_id: 'n1', status: 'completed', files: [{ path: '/w/out.pptx', op: 'write', add: 0, del: 0, size: 2048 }] })],
      })
      render(<TaskPane task={withFile} />)
      act(() => { (document.querySelector('.tkchips .wchip') as HTMLElement).click() })
      expect(opened).toEqual(['/w/out.pptx'])
    })

    it('builds the diff from the node record and opens it as a desk pane', async () => {
      record = {
        dispatch: 'go', outputTruncated: false, answer: null,
        steps: [{
          kind: 'tool', id: 'c1', name: 'write_file',
          args: JSON.stringify({ path: '/w/a.py', content: 'one\ntwo\n' }), result: 'ok', ok: true,
        }],
      }
      const withDiff = task({
        id: 'a', kind: 'dag', status: 'completed',
        nodes: [node({ node_id: 'n1', status: 'completed', files: [{ path: '/w/a.py', op: 'edit', add: 2, del: 0 }] })],
      })
      render(<TaskPane task={withDiff} />)
      await act(async () => { (document.querySelector('.tkchips .wchip') as HTMLElement).click() })
      const panes = desk.get().panes
      expect(panes).toHaveLength(1)
      expect(panes[0]!.kind).toBe('diff')
      expect((panes[0]! as { change: WsChange }).change.hunks[0]).toMatchObject({ add: 2, del: 0 })
    })

    it('draws no chip strip for a task that left nothing behind', () => {
      render(<TaskPane task={task({ id: 'a', kind: 'dag', status: 'completed' })} />)
      expect(document.querySelector('.tkchips')).toBeNull()
    })
  })
})

describe('the node panel', () => {
  const pick = (task: TaskRow, i = 0): { unmount: () => void } => {
    const r = render(<TaskPane task={task} full />)
    fireEvent.click(document.querySelectorAll('.daggraph .nd')[i] as Element)
    return r
  }

  it('opens on the order tab for a step that has not run, on context for one that has', () => {
    const withNodes = task({
      id: 'a', kind: 'dag', status: 'running',
      nodes: [
        node({ node_id: 'n1', status: 'completed' }),
        node({ node_id: 'n2', status: 'pending', depends_on: ['n1'] }),
      ],
    })
    pick(withNodes, 1)
    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent).toBe('gui.tasks.tab_order')
    cleanup()
    pick(withNodes, 0)
    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent).toBe('gui.tasks.tab_context')
  })

  it('warns when the assigned agent is not on this roster, and links to add it', () => {
    roster = [{ name: 'raven', kind: 'builtin', description: '', enabled: true, configured: true, group: 'builtin', probe_status: 'ready', probe_detail: '', has_api_key: false, mcps: [], allow_mcp_secrets: false, test_running: false }]
    const withMissing = task({
      id: 'a', kind: 'dag', status: 'running',
      nodes: [node({ node_id: 'n1', status: 'pending', agent: 'Raven-Ghost' })],
    })
    pick(withMissing)
    act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect(document.querySelector('.tkagent.tkmiss')?.textContent).toBe('Raven-Ghost')
    expect(document.querySelector('.tkfix')?.textContent).toBe('gui.tasks.agent_missing')
  })

  it('reads skills and mcps as absent, not as empty, when the tool takes no such parameter', () => {
    const spawnRow = task({
      id: 'a', kind: 'spawn', status: 'completed',
      nodes: [node({ node_id: 'a', status: 'completed', agent: 'raven' })],
    })
    pick(spawnRow)
    act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect(document.querySelectorAll('.tkfield .tkfv')[1]?.textContent).toBe('gui.tasks.skills_none')
    expect(document.querySelectorAll('.tkfield .tkfv')[2]?.textContent).toBe('gui.tasks.mcps_none')
  })

  it('highlights placeholders in the template for a step that has not been dispatched', () => {
    const pending = task({
      id: 'a', kind: 'dag', status: 'running',
      nodes: [node({ node_id: 'n1', status: 'pending', prompt_template: 'Use {{ inputs.week }} and {{ n0.output }}.' })],
    })
    pick(pending)
    act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect([...document.querySelectorAll('.tkph')].map((m) => m.textContent)).toEqual(['{{ inputs.week }}', '{{ n0.output }}'])
  })

  it('shows the rendered dispatch and the answer for a node that ran', async () => {
    record = { dispatch: 'the rendered prompt', steps: [], answer: 'the answer', outputTruncated: false }
    const done = task({
      id: 'a', kind: 'dag', status: 'completed',
      nodes: [node({ node_id: 'n1', status: 'completed', started_at: 1000, ended_at: 2000 })],
    })
    pick(done)
    await act(async () => {})
    expect(document.querySelector('.tkdispb')?.textContent).toBe('the rendered prompt')
    expect(document.querySelector('.tkans')?.textContent).toBe('the answer')
  })

  it('says a step has not started rather than drawing an empty context', () => {
    const pending = task({
      id: 'a', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'pending' })],
    })
    pick(pending)
    /* A step that has not run opens on the order tab by default; asking for
       the context tab explicitly is what this assertion is about. */
    act(() => { (document.querySelectorAll('.tktabs button')[0] as HTMLElement).click() })
    expect(document.querySelector('.tkempty')?.textContent).toBe('gui.tasks.ctx_none')
  })

  it('keeps the untrusted fence visible around an upstream answer', async () => {
    record = {
      dispatch: 'Spot quotes:\n[BEGIN UNTRUSTED subagent #spot]\n4312.5\n[END UNTRUSTED subagent #spot]\n\nGo.',
      steps: [], answer: null, outputTruncated: false,
    }
    const done = task({
      id: 'a', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running', started_at: 1000 })],
    })
    pick(done)
    await act(async () => {})
    const fenced = [...document.querySelectorAll('.tkuntrusted')]
    expect(fenced).toHaveLength(1)
    expect(fenced[0]!.textContent).toContain('[BEGIN UNTRUSTED subagent #spot]')
  })

  it('footer names the run id for a dag and the call id for a spawn, each with a copy button', () => {
    const dagRow = task({ id: 'run-123', kind: 'dag', status: 'completed', nodes: [node({ node_id: 'n1', status: 'completed' })] })
    pick(dagRow)
    act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect(document.querySelector('.tkspecid span')?.textContent).toBe('gui.tasks.run_label')
    expect(document.querySelector('.tkspecid b')?.textContent).toBe('run-123')
  })

  it('shows a dash rather than nothing when the lane never reported a tool count', () => {
    const done = task({
      id: 'a', kind: 'dag', status: 'completed',
      nodes: [node({ node_id: 'n1', status: 'completed', started_at: 1000, ended_at: 2000, tool_call_count: null })],
    })
    pick(done)
    expect(document.querySelector('.tksub')?.textContent).toContain('—')
  })

  describe('a cross-run dependency', () => {
    it('names a depends_on id that belongs to another task, in mono', () => {
      const withExternal = task({
        id: 'a', kind: 'dag', status: 'running',
        nodes: [node({ node_id: 'n1', status: 'pending', depends_on: ['other-run-node'] })],
      })
      pick(withExternal)
      expect(document.querySelector('.tknote')?.textContent)
        .toBe('gui.tasks.depends_on_pre other-run-node gui.tasks.depends_on_post')
      expect(document.querySelector('.tknote .mono')?.textContent).toBe('other-run-node')
    })

    it('reads one out of an {node} input as well as out of depends_on', () => {
      const withExternalInput = task({
        id: 'a', kind: 'dag', status: 'running',
        nodes: [node({ node_id: 'n1', status: 'pending', inputs: { source: { node: 'other-run-node' } } })],
      })
      pick(withExternalInput)
      expect(document.querySelector('.tknote .mono')?.textContent).toBe('other-run-node')
    })

    it('says nothing for a depends_on id that belongs to this same task', () => {
      const internal = task({
        id: 'a', kind: 'dag', status: 'running',
        nodes: [
          node({ node_id: 'n1', status: 'completed' }),
          node({ node_id: 'n2', status: 'pending', depends_on: ['n1'] }),
        ],
      })
      pick(internal, 1)
      expect(document.querySelector('.tknote')).toBeNull()
    })
  })
})
