// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import { resetSources, setSources } from '../../state/sources'
import { get as toastGet } from '../../state/toast'
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

  it('groups running and settled under a bare heading, with no count on it', async () => {
    rows = [
      task({ id: 'a', kind: 'dag', status: 'running' }),
      task({ id: 'b', kind: 'spawn', status: 'running' }),
      task({ id: 'c', kind: 'dag', status: 'completed' }),
    ]
    await draw()
    expect([...document.querySelectorAll('.wsgrp')].map((g) => g.textContent))
      .toEqual(['gui.tasks.running', 'gui.tasks.settled'])
  })

  it('shows the summary, or the id when there is none, and the error tag on a failure', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'failed', task_summary: null })]
    await draw()
    expect(document.querySelector('.sarow.task .nm')?.textContent).toBe('a')
    expect(document.querySelector('.sarow.task .tkerr')?.textContent).toBe('error')
  })

  /* The row's dot and the pane's own status word already read an interrupted
     run as the same "error" state a failure is (`tdotState`, `stepLine`'s
     `interrupted` branch); the tag beside the name is the one place that used
     to draw it only for `failed`, so a stopped run showed a clay dot with no
     word next to it. */
  it('shows the error tag on an interrupted run too, not only a failed one', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'interrupted' })]
    await draw()
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

  it('renders no rows and does not crash when the store holds none', async () => {
    await draw()
    expect(document.querySelectorAll('.sarow.task')).toHaveLength(0)
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

  it('says the request landed before the reconciled row does', async () => {
    /* A slow reconcile must not be the reader's only sign the click worked --
       the toast fires on the click itself, not once `store.stop` resolves.
       (No <Toasts/> is mounted in this harness, so the notice is read off the
       store `show` writes to rather than off a rendered element.) */
    let release = (): void => {}
    const running = task({ id: 'r1', kind: 'dag', status: 'running' })
    rows = [running]
    setSources({
      tasks: {
        ...source(),
        stop: async (r) => { await new Promise<void>((res) => { release = res }); stopped.push(r); return true },
      },
      workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
    })
    document.body.insertAdjacentHTML('afterbegin', '<div id="toasts"></div>')
    render(<TaskPane task={running} />)
    act(() => { (document.querySelector('.tkbaract') as HTMLElement).click() })
    expect(toastGet().some((n) => n.text === 'gui.tasks.stop_requested')).toBe(true)
    await act(async () => { release(); await Promise.resolve() })
  })

  it('names the failed node in the why banner, and links to it', async () => {
    const bad = node({ node_id: 'fetch_comex', status: 'failed', node_summary: 'Fetch the futures quote', error: 'HTTP 429' })
    const failed = task({ id: 'a', kind: 'dag', status: 'failed', nodes: [bad] })
    render(<TaskPane task={failed} full />)
    expect(document.querySelector('.tkwhy p')?.textContent).toBe('HTTP 429')
    /* The name sits in its own `<b>`, set apart from the "stuck at: " prefix
       rather than folded into one interpolated sentence. */
    expect(document.querySelector('.tkwhyat b')?.textContent).toBe('Fetch the futures quote')
    expect(document.querySelector('.tkwhyat')?.textContent).toBe('gui.tasks.why_atFetch the futures quote')
    /* The whole banner is the control now, not just the name inside it. */
    expect(document.querySelector('.tkwhy')?.getAttribute('role')).toBe('button')

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

describe('the board', () => {
  it('draws the prototype\'s own runtime card, not the shared SVG box', () => {
    const withNode = task({
      id: 'a', kind: 'dag', status: 'running',
      nodes: [node({
        node_id: 'n1', status: 'running', node_summary: 'scan the feed',
        started_at: Date.now() - 4000, tool_call_count: 3,
      })],
    })
    render(<TaskPane task={withNode} full />)
    expect(document.querySelector('.daggraph .nd .tkrunl1')?.textContent).toBe('scan the feed')
    expect(document.querySelector('.daggraph .nd .tkrunag')?.textContent).toBe('raven')
    expect(document.querySelector('.daggraph .nd .tkrunchip')?.textContent).toBe('gui.tasks.tools_n {"n":3}')
    /* Only this board's own card is drawn -- never the shared SVG box it
       replaces (DagGraph.tsx's `renderNode` branch). */
    expect(document.querySelector('.daggraph .nd rect')).toBeNull()
    expect(document.querySelector('.daggraph .nd title')).toBeNull()
  })

  /* happy-dom reports every element as zero-width, so the viewport size has to
     be supplied by hand -- same as PlaybooksPage.test.tsx's own board tests,
     which read from the same Board.tsx. */
  function withPort(w: number, h: number): () => void {
    const own = {
      w: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth'),
      h: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
    }
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h })
    return () => {
      if (own.w) Object.defineProperty(HTMLElement.prototype, 'clientWidth', own.w)
      if (own.h) Object.defineProperty(HTMLElement.prototype, 'clientHeight', own.h)
    }
  }

  const chain = (n: number): TaskNode[] =>
    Array.from({ length: n }, (_, i) =>
      node({ node_id: `n${i}`, status: 'pending', depends_on: i ? [`n${i - 1}`] : [] }))

  it('leaves EDGE of ground on the axis that binds the fit, so a lane label just above the top node is not clipped by the stage', async () => {
    /* A straight chain running top to bottom makes height, not width, the
       binding axis -- the one a lane's own label pokes 20px above (11px lane
       inset + 9px label overhang), and the one the prototype's own EDGE*2
       margin is for. */
    const restore = withPort(300, 300)
    try {
      const tall = task({ id: 'a', kind: 'dag', status: 'running', nodes: chain(5) })
      render(<TaskPane task={tall} full />)
      await act(async () => {})
      const m = /translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)/
        .exec((document.querySelector('.gview') as HTMLElement).style.transform)
      expect(m).toBeTruthy()
      /* height*z lands 28px short of the port on the binding axis, so the
         centred offset is exactly EDGE (14) on each side -- not 0, which is
         what clipped the label before the margin came off the ratio. */
      expect(Number(m![2])).toBeCloseTo(14, 5)
    } finally {
      restore()
    }
  })
})

describe('a docked pane', () => {
  const twoNodes = (): TaskRow => task({
    id: 'a', kind: 'dag', status: 'running',
    nodes: [node({ node_id: 'n1', status: 'completed' }), node({ node_id: 'n2', status: 'running', depends_on: ['n1'] })],
  })

  it('keeps the board mounted under a picked node, rather than tearing it down and losing the reader\'s pan', () => {
    const row = twoNodes()
    render(<TaskPane task={row} />)
    /* The board is drawn (no node picked yet). */
    expect(document.querySelector('.daggraph')).not.toBeNull()
    fireEvent.click(document.querySelectorAll('.daggraph .nd')[0] as Element)
    /* CSS -- not this component tree -- decides which of the two shows in a
       docked pane; both stay mounted so the graph the reader panned is still
       there, at the same pan, once they back out. */
    expect(document.querySelector('.daggraph')).not.toBeNull()
    expect(document.querySelector('.tkcard')).not.toBeNull()
  })

  it('collapses the fullscreen grid to one column, with no placeholder, while nothing is picked', () => {
    render(<TaskPane task={twoNodes()} full />)
    expect(document.querySelector('.tkview')?.getAttribute('data-detail')).toBe('false')
    expect(document.querySelector('.tkpick')).toBeNull()
    expect(document.querySelector('.daggraph')).not.toBeNull()
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
    expect(document.querySelector('.tkcaret')).toBeNull()
  })

  it('trails the answer with the streaming caret while the node is still running', async () => {
    record = { dispatch: 'the rendered prompt', steps: [], answer: 'still writ', outputTruncated: false }
    const running = task({
      id: 'a', kind: 'dag', status: 'running',
      nodes: [node({ node_id: 'n1', status: 'running', started_at: 1000 })],
    })
    pick(running)
    await act(async () => {})
    expect(document.querySelector('.tkans .tkcaret')).not.toBeNull()
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

  it('marks only the fence lines, leaving the injected body as ordinary prose', async () => {
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
    expect(fenced.map((f) => f.textContent)).toEqual([
      '[BEGIN UNTRUSTED subagent #spot]', '[END UNTRUSTED subagent #spot]',
    ])
    /* The injected body between the two markers is not one of them. */
    expect(document.querySelector('.tkdispb')?.textContent).toContain('4312.5')
    expect([...document.querySelectorAll('.tkuntrusted')].some((f) => f.textContent?.includes('4312.5'))).toBe(false)
  })

  it('still marks a lone BEGIN marker whose matching END never arrived', async () => {
    record = {
      dispatch: 'Spot quotes:\n[BEGIN UNTRUSTED subagent #spot]\n4312.5 (truncated',
      steps: [], answer: null, outputTruncated: false,
    }
    const running = task({
      id: 'a', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running', started_at: 1000 })],
    })
    pick(running)
    await act(async () => {})
    expect([...document.querySelectorAll('.tkuntrusted')].map((f) => f.textContent))
      .toEqual(['[BEGIN UNTRUSTED subagent #spot]'])
  })

  it('footer names the run id for a dag and the call id for a spawn, each with a copy button', () => {
    const dagRow = task({ id: 'run-123', kind: 'dag', status: 'completed', nodes: [node({ node_id: 'n1', status: 'completed' })] })
    pick(dagRow)
    act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect(document.querySelector('.tkspecid span')?.textContent).toBe('gui.tasks.run_label')
    expect(document.querySelector('.tkspecid b')?.textContent).toBe('run-123')
  })

  it('subtitle is the agent, the status word and the duration when the lane reported no usage', () => {
    const done = task({
      id: 'a', kind: 'dag', status: 'completed',
      nodes: [node({
        node_id: 'n1', status: 'completed', started_at: 1000, ended_at: 2000,
        tokens_in: null, tokens_out: null, tool_call_count: 14, tool_failure_count: 1,
      })],
    })
    pick(done)
    expect(document.querySelector('.tksub')?.textContent).toBe('raven · gui.tasks.node_st_completed · 1s')
  })

  /* A settled node: every lane writes its usage into the record when the run
     finishes, so that is the shape the server sends the fragment on. */
  it('subtitle adds the token total, grouped by thousands, when the lane reported usage', () => {
    const done = task({
      id: 'a', kind: 'spawn', status: 'completed',
      nodes: [node({ node_id: 'n1', status: 'completed', started_at: 1000, ended_at: 111_000, tokens_in: 4000, tokens_out: 910 })],
    })
    pick(done)
    expect(document.querySelector('.tksub')?.textContent).toBe('raven · gui.tasks.node_st_completed · 1m50s · gui.tasks.tokens_n {"n":"4,910"}')
  })

  it('subtitle counts usage a lane reported on one side only', () => {
    const done = task({
      id: 'a', kind: 'spawn', status: 'completed',
      nodes: [node({ node_id: 'n1', status: 'completed', started_at: 1000, ended_at: 2000, tokens_in: 500, tokens_out: null })],
    })
    pick(done)
    expect(document.querySelector('.tksub')?.textContent).toBe('raven · gui.tasks.node_st_completed · 1s · gui.tasks.tokens_n {"n":"500"}')
  })

  it('sets the agent apart in its own <b>, without the handle', () => {
    const done = task({
      id: 'a', kind: 'dag', status: 'completed',
      nodes: [node({ node_id: 'n1', status: 'completed', agent: 'coder', instance: 'x1' })],
    })
    pick(done)
    expect(document.querySelector('.tksub b')?.textContent).toBe('coder')
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

  describe('a running node\'s record', () => {
    it('is re-read on a live node_updated event, so its steps and answer keep arriving', async () => {
      const calls: string[] = []
      record = { dispatch: 'go', steps: [], answer: null, outputTruncated: false }
      setSources({
        tasks: {
          ...source(),
          node: async () => { calls.push('fetch'); return record },
        },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      pick(running)
      await act(async () => {})
      expect(calls).toEqual(['fetch'])

      record = { dispatch: 'go', steps: [], answer: 'the answer', outputTruncated: false }
      await act(async () => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c1' }) })
      expect(calls).toEqual(['fetch', 'fetch'])
      expect(document.querySelector('.tkans')?.textContent).toBe('the answer')
    })

    it('keeps the last record on screen while refetching, rather than flashing blank', async () => {
      let calls = 0
      let resolveSecond: ((r: NodeRecord) => void) | null = null
      setSources({
        tasks: {
          ...source(),
          node: async () => {
            calls += 1
            if (calls === 1) return { dispatch: 'go', steps: [], answer: 'first answer', outputTruncated: false }
            return new Promise((res) => { resolveSecond = res })
          },
        },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      pick(running)
      await act(async () => {})
      expect(document.querySelector('.tkans')?.textContent).toBe('first answer')

      act(() => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c2' }) })
      /* Still showing the old answer while the second fetch is in flight. */
      expect(calls).toBe(2)
      expect(document.querySelector('.tkans')?.textContent).toBe('first answer')
      await act(async () => { resolveSecond?.({ dispatch: 'go', steps: [], answer: 'second answer', outputTruncated: false }) })
      expect(document.querySelector('.tkans')?.textContent).toBe('second answer')
    })

    /* A spawn's lane sends no per-step event -- `subagent.status` moves on
       pending, running and the terminal word only -- so a running spawn's
       record is re-read on a beat, the transcript's spawn card's own
       cadence, rather than left at whatever the first read saw. */
    it("a running spawn's record is re-read on a beat, so its steps keep arriving", async () => {
      vi.useFakeTimers()
      try {
        const calls: string[] = []
        record = { dispatch: 'go', steps: [], answer: null, outputTruncated: false }
        setSources({
          tasks: { ...source(), node: async () => { calls.push('fetch'); return record } },
          workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
        })
        const running = task({
          id: 's1', kind: 'spawn', status: 'running', nodes: [node({ node_id: 's1', status: 'running' })],
        })
        pick(running)
        await act(async () => {})
        expect(calls).toEqual(['fetch'])

        record = { dispatch: 'go', steps: [{ kind: 'say', text: 'first step' }], answer: null, outputTruncated: false }
        await act(async () => { vi.advanceTimersByTime(1000) })
        expect(calls).toEqual(['fetch', 'fetch'])
        expect(document.querySelector('.tkprocb .tkans')?.textContent).toBe('first step')

        await act(async () => { vi.advanceTimersByTime(1000) })
        expect(calls).toEqual(['fetch', 'fetch', 'fetch'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('skips a beat while a read is still out, rather than stacking reads', async () => {
      vi.useFakeTimers()
      try {
        let calls = 0
        setSources({
          tasks: { ...source(), node: () => { calls += 1; return new Promise<NodeRecord>(() => {}) } },
          workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
        })
        const running = task({
          id: 's1', kind: 'spawn', status: 'running', nodes: [node({ node_id: 's1', status: 'running' })],
        })
        pick(running)
        await act(async () => {})
        expect(calls).toBe(1)
        await act(async () => { vi.advanceTimersByTime(3000) })
        expect(calls).toBe(1)
      } finally {
        vi.useRealTimers()
      }
    })

    it('a running dag node is not re-read on a beat: its node_updated events already do that', async () => {
      vi.useFakeTimers()
      try {
        const calls: string[] = []
        setSources({
          tasks: { ...source(), node: async () => { calls.push('fetch'); return record } },
          workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
        })
        const running = task({
          id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
        })
        pick(running)
        await act(async () => {})
        await act(async () => { vi.advanceTimersByTime(3000) })
        expect(calls).toEqual(['fetch'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('is re-read once when the spawn settles, so the answer lands without reopening the node, and the beat stops', async () => {
      vi.useFakeTimers()
      try {
        const calls: string[] = []
        record = { dispatch: 'go', steps: [], answer: null, outputTruncated: false }
        setSources({
          tasks: { ...source(), node: async () => { calls.push('fetch'); return record } },
          workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
        })
        const running = task({
          id: 's1', kind: 'spawn', status: 'running', agent: 'raven', handle: 'h1',
          nodes: [node({ node_id: 's1', status: 'running', instance: 'h1', started_at: 1000 })],
        })
        store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
        pick(running)
        await act(async () => {})
        expect(calls).toEqual(['fetch'])

        record = { dispatch: 'go', steps: [], answer: 'the answer', outputTruncated: false }
        await act(async () => {
          store.onSubagentStatus({ task_id: 't1', call_id: 's1', agent: 'raven', label: 'x', status: 'completed', ended_at: 2000 })
        })
        expect(calls).toEqual(['fetch', 'fetch'])
        expect(document.querySelector('.tkanswer .tkans')?.textContent).toBe('the answer')
        expect(document.querySelector('.tksub')?.textContent).toBe('raven · gui.tasks.node_st_completed · 1s')

        /* Settled: the beat has nothing left to follow. */
        await act(async () => { vi.advanceTimersByTime(3000) })
        expect(calls).toEqual(['fetch', 'fetch'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('stops the beat when the node panel closes', async () => {
      vi.useFakeTimers()
      try {
        const calls: string[] = []
        setSources({
          tasks: { ...source(), node: async () => { calls.push('fetch'); return record } },
          workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
        })
        const running = task({
          id: 's1', kind: 'spawn', status: 'running', nodes: [node({ node_id: 's1', status: 'running' })],
        })
        const mounted = pick(running)
        await act(async () => {})
        await act(async () => { vi.advanceTimersByTime(1000) })
        expect(calls).toEqual(['fetch', 'fetch'])
        mounted.unmount()
        await act(async () => { vi.advanceTimersByTime(3000) })
        expect(calls).toEqual(['fetch', 'fetch'])
      } finally {
        vi.useRealTimers()
      }
    })

    /* The row rides the same beat: a running node's usage grows on the
       server with no frame to carry it, so the subtitle's token total is
       read again with the record. */
    it("re-reads a running spawn's row on the beat, so its usage reaches the subtitle", async () => {
      vi.useFakeTimers()
      try {
        const running = task({
          id: 's1', kind: 'spawn', status: 'running', nodes: [node({ node_id: 's1', status: 'running', started_at: 1000 })],
        })
        rows = [running]
        store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
        pick(running)
        await act(async () => {})
        expect(document.querySelector('.tksub')?.textContent).not.toContain('gui.tasks.tokens_n')

        rows = [{ ...running, nodes: [node({ node_id: 's1', status: 'running', started_at: 1000, tokens_in: 1200, tokens_out: 34 })] }]
        await act(async () => { vi.advanceTimersByTime(1000) })
        expect(document.querySelector('.tksub')?.textContent).toContain('gui.tasks.tokens_n {"n":"1,234"}')
      } finally {
        vi.useRealTimers()
      }
    })

    it("re-reads a running dag node's row on node_updated, so its usage reaches the subtitle", async () => {
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running', started_at: 1000 })],
      })
      rows = [running]
      store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
      pick(running)
      await act(async () => {})
      expect(document.querySelector('.tksub')?.textContent).not.toContain('gui.tasks.tokens_n')

      rows = [{ ...running, nodes: [node({ node_id: 'n1', status: 'running', started_at: 1000, tokens_in: 4000, tokens_out: 910 })] }]
      await act(async () => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c1' }) })
      expect(document.querySelector('.tksub')?.textContent).toContain('gui.tasks.tokens_n {"n":"4,910"}')
    })

    it('keeps one row read out at a time across node_updated frames', async () => {
      let reads = 0
      let release: ((r: TaskRow | null) => void) | null = null
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      setSources({
        tasks: { ...source(), one: () => { reads += 1; return new Promise((res) => { release = res }) } },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      store.set((prev) => ({ ...prev, rows: [running], loaded: true }))
      pick(running)
      await act(async () => {})
      expect(reads).toBe(1)
      await act(async () => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c1' }) })
      await act(async () => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c2' }) })
      /* Two frames while the first row read is still out: no second read. */
      expect(reads).toBe(1)
      await act(async () => { release?.(null) })
      await act(async () => { store.onNodeUpdated({ run_id: 'r1', node: 'n1', status: 'running', tool_call_id: 'c3' }) })
      expect(reads).toBe(2)
    })
  })

  describe('the context tab while the record is loading or failed', () => {
    it('says it is reading rather than showing nothing, for the length of the fetch', async () => {
      let release: ((r: NodeRecord) => void) | null = null
      setSources({
        tasks: { ...source(), node: () => new Promise((res) => { release = res }) },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      pick(running)
      expect(document.querySelector('.tkempty')?.textContent).toBe('gui.tasks.ctx_loading')
      await act(async () => { release?.({ dispatch: 'go', steps: [], answer: null, outputTruncated: false }) })
      expect(document.querySelector('.tkempty')).toBeNull()
    })

    it('offers a retry rather than staying blank when the fetch fails', async () => {
      let attempts = 0
      setSources({
        tasks: {
          ...source(),
          node: async () => {
            attempts += 1
            if (attempts === 1) throw new Error('gateway refused')
            return { dispatch: 'go', steps: [], answer: 'the answer', outputTruncated: false }
          },
        },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      const running = task({
        id: 'r1', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      pick(running)
      await act(async () => {})
      expect(document.querySelector('.tkretry')).not.toBeNull()
      await act(async () => { (document.querySelector('.tkretry') as HTMLElement).click() })
      expect(document.querySelector('.tkans')?.textContent).toBe('the answer')
    })
  })

  describe('a failed node with no error of its own', () => {
    it('falls back to a sibling node\'s error rather than the bare fallback sentence', async () => {
      record = { dispatch: 'go', steps: [], answer: null, outputTruncated: false }
      const failed = task({
        id: 'a', kind: 'dag', status: 'failed',
        nodes: [
          node({ node_id: 'n1', status: 'failed', error: null }),
          node({ node_id: 'n2', status: 'failed', error: 'HTTP 500 from upstream' }),
        ],
      })
      pick(failed)
      await act(async () => {})
      expect(document.querySelector('.tkerrb')?.textContent).toBe('HTTP 500 from upstream')
    })
  })

  describe('the work order tab during the fetch', () => {
    it('never claims a dispatched node was not dispatched while its record is still loading', async () => {
      let release: ((r: NodeRecord) => void) | null = null
      setSources({
        tasks: { ...source(), node: () => new Promise((res) => { release = res }) },
        workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
      })
      const running = task({
        id: 'r1', kind: 'dag', status: 'running',
        nodes: [node({ node_id: 'n1', status: 'running', prompt_template: 'Use {{ inputs.week }}.' })],
      })
      pick(running)
      act(() => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
      const instructionField = [...document.querySelectorAll('.tkfield')]
        .find((f) => f.querySelector('.tkfk')?.textContent === 'gui.tasks.instruction')
      expect(instructionField?.querySelector('.tkfv')?.textContent).toBe('gui.tasks.instruction_loading')
      expect(document.querySelector('.tknote')).toBeNull()
      await act(async () => { release?.({ dispatch: 'the rendered prompt', steps: [], answer: null, outputTruncated: false }) })
      expect(document.querySelector('.tkpv')?.textContent).toBe('the rendered prompt')
    })
  })

  describe('fold memory across a node switch', () => {
    it('does not carry one node\'s open thought onto a different node picked next', async () => {
      record = { dispatch: 'go', steps: [{ kind: 'think', text: 'first, check the dates' }], answer: null, outputTruncated: false }
      const withTwo = task({
        id: 'a', kind: 'dag', status: 'completed',
        nodes: [
          node({ node_id: 'n1', status: 'completed' }),
          node({ node_id: 'n2', status: 'completed' }),
        ],
      })
      pick(withTwo, 0)
      await act(async () => {})
      /* The process fold starts closed for a settled node (item 15); open it
         to reach the thought inside. */
      fireEvent.click(document.querySelector('.tkprock') as Element)
      fireEvent.click(document.querySelector('.tkthh') as Element)
      expect(document.querySelector('.tkthink blockquote')).toBeNull()
      fireEvent.click(document.querySelectorAll('.daggraph .nd')[1] as Element)
      await act(async () => {})
      fireEvent.click(document.querySelector('.tkprock') as Element)
      expect(document.querySelector('.tkthink blockquote')).not.toBeNull()
    })
  })

  describe('the process fold\'s own default', () => {
    it('recomputes from the node\'s status rather than freezing at mount', async () => {
      record = { dispatch: 'go', steps: [{ kind: 'say', text: 'partial' }], answer: null, outputTruncated: false }
      const running = task({
        id: 'a', kind: 'dag', status: 'running', nodes: [node({ node_id: 'n1', status: 'running' })],
      })
      pick(running)
      await act(async () => {})
      expect(document.querySelector('.tkprock')?.getAttribute('aria-expanded')).toBe('true')

      await act(async () => {
        store.set((prev) => ({
          ...prev,
          rows: [{
            ...running, status: 'completed',
            nodes: [{ ...running.nodes[0]!, status: 'completed', ended_at: 2000, started_at: 1000 }],
          }],
        }))
      })
      expect(document.querySelector('.tkprock')?.getAttribute('aria-expanded')).toBe('false')
    })
  })

  describe('the reply dock\'s send control', () => {
    it('is an icon button, not a text pill', async () => {
      roster = [{
        name: 'raven', kind: 'builtin', description: '', enabled: true, configured: true, group: 'builtin',
        probe_status: 'ready', probe_detail: '', has_api_key: false, mcps: [], allow_mcp_secrets: false,
        test_running: false, stateful: true,
      }]
      const withInstance = task({
        id: 'a', kind: 'dag', status: 'completed',
        nodes: [node({ node_id: 'n1', status: 'completed', agent: 'raven', instance: 'x1' })],
      })
      pick(withInstance)
      await act(async () => {})
      const send = document.querySelector('.tkdock button') as HTMLButtonElement
      expect(send.className).toContain('go')
      expect(send.textContent).toBe('')
      expect(send.querySelector('svg')).not.toBeNull()
    })
  })
})
