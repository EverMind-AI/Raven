// @vitest-environment happy-dom
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../state/sources'
import { actLabel } from '../transcript/source'
import { Answer, groupSteps, StepList } from './NodeRecord'
import * as store from './store'

import type { TranscriptSource } from '../transcript/types'
import type { NodeStep, TaskNode, TaskRow, TasksSource } from './types'

const taskRow = (over: Partial<TaskRow> & Pick<TaskRow, 'id' | 'kind' | 'status'>): TaskRow => ({
  task_summary: null, started_at: null, ended_at: null, agent: null, handle: null,
  counts: { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0 },
  nodes: [],
  ...over,
})

const taskNode = (over: Partial<TaskNode> & Pick<TaskNode, 'node_id' | 'agent' | 'status'>): TaskNode => ({
  node_summary: null, instance: null, depends_on: [], started_at: null, ended_at: null, error: null,
  tokens_in: null, tokens_out: null, tool_call_count: null, tool_failure_count: null, has_output: null,
  prompt_template: null, files: [],
  ...over,
})

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const tool = (over: Partial<Extract<NodeStep, { kind: 'tool' }>> & { name: string }): NodeStep => ({
  kind: 'tool', id: 'c1', args: '{}', result: 'ok', ok: true, ...over,
})

beforeEach(() => {
  /* Every fold below is now the tasks store's own state (per-node memory,
     CONTRIBUTING 2.2), not component-local -- so a row opened in one test
     stays open for the next unless the store itself resets. */
  store._resetForTests()
  setSources({
    transcript: { clean: (s) => String(s), okOf: () => true, actLabel } as TranscriptSource,
    /* A real elision, not the identity: `shortPath` truncates anything over
       42 chars to '…' plus its last 40, the same rule the workspace domain's
       own implementation carries -- a test for the elided head vs. the full
       copy payload needs the mock to actually elide. */
    workspace: {
      shortPath: (p: string) => (p.length > 42 ? `…${p.slice(-40)}` : p),
      hostPlatform: () => 'mac', canBrowse: false, openPath: () => {},
    },
  })
})

afterEach(() => {
  cleanup()
  resetSources()
  vi.useRealTimers()
})

describe('groupSteps', () => {
  it('folds a thought together with the calls it led to into one group', () => {
    const groups = groupSteps([
      { kind: 'think', text: 'let me look' },
      tool({ name: 'read_file', args: JSON.stringify({ path: '/a.py' }) }),
      tool({ name: 'grep', args: JSON.stringify({ pattern: 'x' }), id: 'c2' }),
    ])
    expect(groups).toHaveLength(1)
    const g = groups[0]!
    expect(g.kind).toBe('group')
    if (g.kind !== 'group') throw new Error('unreachable')
    expect(g.think).toBe('let me look')
    expect(g.calls).toHaveLength(2)
  })

  it('opens a new group when a thought arrives after the current one already has calls', () => {
    const groups = groupSteps([
      { kind: 'think', text: 'first' },
      tool({ name: 'read_file' }),
      { kind: 'think', text: 'second' },
      tool({ name: 'write_file', id: 'c2' }),
    ])
    expect(groups).toHaveLength(2)
  })

  it('keeps a console entry on its own, outside any group', () => {
    const groups = groupSteps([
      { kind: 'think', text: 'first' },
      { kind: 'console', text: 'cli output' },
    ])
    expect(groups.map((g) => g.kind)).toEqual(['group', 'console'])
  })
})

describe('verb labels', () => {
  it.each([
    ['write_file', JSON.stringify({ path: '/a.py', content: 'x' }), 'wrote', 'writing'],
    ['edit_file', JSON.stringify({ path: '/a.py', old_text: 'a', new_text: 'b' }), 'edited', 'editing'],
    ['exec', JSON.stringify({ command: 'ls' }), 'ran command', 'running command'],
    ['read_file', JSON.stringify({ path: '/a.py' }), 'read', 'reading'],
    ['web_fetch', JSON.stringify({ url: 'https://a.com' }), 'read page', 'reading page'],
  ])('shows %s\'s catalogue verb, done and in flight', (name, args, doneWord, ingWord) => {
    const { container, unmount } = render(
      <StepList steps={[tool({ name, args, result: 'ok', ok: true })]} running={false} />,
    )
    expect(container.querySelector('.tkvb')?.textContent).toBe(doneWord)
    unmount()
    const flying = render(
      <StepList steps={[tool({ name, args, result: null, ok: null, id: 'c2' })]} running />,
    )
    expect(flying.container.querySelector('.tkvb')?.textContent).toBe(ingWord)
  })

  it('shows the MCP server prefix and falls back to the bare tool name for its verb', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'mcp_github_search_issues',
          args: JSON.stringify({ q: 'bug' }), result: '[]', ok: true,
        })]}
        running={false}
      />,
    )
    expect(container.querySelector('.tksrv')?.textContent).toBe('[github] ')
    expect(container.querySelector('.tkvb')?.textContent).toBe('[github] search issues')
  })
})

describe('the multi-call summary', () => {
  it('folds more than one call behind a summary naming the kind of work', () => {
    const { container } = render(
      <StepList
        steps={[
          tool({ name: 'read_file', args: JSON.stringify({ path: '/a.py' }), id: 'c1' }),
          tool({ name: 'read_file', args: JSON.stringify({ path: '/b.py' }), id: 'c2' }),
        ]}
        running={false}
      />,
    )
    const sum = container.querySelector('.tkwrow.tksum')
    expect(sum).not.toBeNull()
    expect(sum?.querySelector('.tkar')?.textContent).toBe('read 2 files')
    expect(container.querySelector('.tkwkin')).toHaveProperty('hidden', true)
  })

  it('names how many failed on the summary chip', () => {
    const { container } = render(
      <StepList
        steps={[
          tool({ name: 'read_file', id: 'c1', result: 'ok', ok: true }),
          tool({ name: 'read_file', id: 'c2', result: 'ENOENT', ok: false }),
        ]}
        running={false}
      />,
    )
    expect(container.querySelector('.tkbadchip')?.textContent).toBe('1 failed')
  })

  it('renders a single call as just its own row, with no summary and nothing hidden', () => {
    const { container } = render(
      <StepList steps={[tool({ name: 'read_file' })]} running={false} />,
    )
    expect(container.querySelector('.tkwrow.tksum')).toBeNull()
    expect(container.querySelector('.tkwkin')).toHaveProperty('hidden', false)
  })
})

describe('detail cards', () => {
  function openRow(container: HTMLElement, i = 0): void {
    const rows = container.querySelectorAll<HTMLButtonElement>('.tkwkin > .tkwrow > button')
    fireEvent.click(rows[i]!)
  }

  it('shows a write/edit card as the path and the +/- count', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'write_file', args: JSON.stringify({ path: '/a.py', content: 'one\ntwo\n' }), result: 'ok', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdtlnm')?.textContent).toBe('/a.py')
    expect(container.querySelector('.tkdstat .add')?.textContent).toBe('+2')
    expect(container.querySelector('.tkdstat .del')?.textContent).toBe('−0')
  })

  it('shows an exec card as the command, the output and an exit-code pill', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'exec', args: JSON.stringify({ command: 'ls -la' }),
          result: 'file1\nfile2\nExit code: 0', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdtlnm')?.textContent).toBe('ls -la')
    expect(container.querySelector('.tkcmd')?.textContent).toBe('ls -la')
    expect(container.querySelector('.tkdtlbd pre')?.textContent).toBe('file1\nfile2')
    expect(container.querySelector('.tkexit')?.textContent).toBe('exit code 0')
    expect(container.querySelector('.tkexit')?.className).toContain('ok')
  })

  it('shows a web_fetch card as the url', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'web_fetch', args: JSON.stringify({ url: 'https://example.com/x' }), result: 'page text', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdtlnm')?.textContent).toBe('https://example.com/x')
  })

  it('shows a plain tool as its computed label with the raw output', () => {
    const { container } = render(
      <StepList
        steps={[tool({ name: 'grep', args: JSON.stringify({ pattern: 'TODO' }), result: 'a.py:1', ok: true })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdtlnm')?.textContent).toBe('TODO')
    expect(container.querySelector('.tkdtlbd pre')?.textContent).toBe('a.py:1')
  })

  it('shows a spawn card as a task / target-agent / state grid', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'spawn', args: JSON.stringify({ task_summary: 'cross-check quotes', agent: 'coder' }),
          result: null, ok: null,
        })]}
        running
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Task', 'Agent', 'State'])
    expect(values[0]).toBe('cross-check quotes')
    expect(values[1]).toBe('coder')
    expect(values[2]).toBe('running')
  })

  it('shows a run_subagent_dag card as a scale / state grid, with no task/run-id/elapsed row when the receipt cannot be parsed', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'run_subagent_dag', args: JSON.stringify({ task_summary: 'audit the repo' }),
          result: 'DAG run r1: done', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Scale', 'State'])
    expect(values[0]).toBe('audit the repo')
  })

  it("resolves a dag call's task, scale, state, elapsed time and run id from the tasks store's own row", () => {
    vi.useFakeTimers()
    vi.setSystemTime(66000)
    store.set({
      ...store.get(),
      rows: [taskRow({
        id: 'r1', kind: 'dag', status: 'running', task_summary: 'nightly audit', started_at: 1000,
        nodes: [
          taskNode({ node_id: 'n1', agent: 'coder', status: 'running' }),
          taskNode({ node_id: 'n2', agent: 'reviewer', status: 'pending' }),
        ],
      })],
    })
    const opened: string[] = []
    setSources({ tasks: { openRun: (id: string) => { opened.push(id); return true } } as unknown as TasksSource })
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'run_subagent_dag', args: JSON.stringify({ task_summary: 'audit the repo' }),
          result: 'DAG r1: 2 nodes started', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Task', 'Scale', 'State', 'Elapsed', 'Run id'])
    expect(values[0]).toBe('nightly audit')
    expect(values[1]).toBe('2 nodes · 2 agents · coder · reviewer')
    expect(values[2]).toBe('running')
    expect(values[3]).toBe('1m05s')
    expect(values[4]).toBe('r1')
    fireEvent.click(container.querySelector('.tkdgv.tkgov') as HTMLElement)
    expect(opened).toEqual(['r1'])
  })

  it("resolves a spawn call's real state and elapsed time from the tasks store's own row, rather than the dispatch call's own", () => {
    store.set({
      ...store.get(),
      rows: [taskRow({
        id: 'n42', kind: 'spawn', status: 'completed', started_at: 1000, ended_at: 66000, agent: 'coder',
        nodes: [taskNode({ node_id: 'n42', agent: 'coder', status: 'completed' })],
      })],
    })
    const opened: string[] = []
    setSources({ tasks: { openByNode: (id: string) => { opened.push(id); return true } } as unknown as TasksSource })
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'spawn',
          args: JSON.stringify({ task_summary: 'cross-check quotes', agent: 'coder', node_id: 'n42' }),
          /* The dispatch call itself returned the instant it was accepted --
             `done` on the call alone would read "completed" from the moment
             the spawn started, which is exactly the gap this resolves. */
          result: 'Subagent [cross-check quotes] started (id: abc12345). I\'ll notify you when it completes.',
          ok: true,
        })]}
        running
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Task', 'Agent', 'State', 'Elapsed'])
    expect(values[2]).toBe('completed')
    expect(values[3]).toBe('1m05s')
    fireEvent.click(container.querySelector('.tkdgv.tkgov') as HTMLElement)
    expect(opened).toEqual(['n42'])
  })

  it("falls back to the receipt's own run id, with no control, when the run is outside this list", () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'load_playbook', args: JSON.stringify({ name: 'nightly-checks' }),
          result: 'DAG r9: started \'nightly-checks\' (3 steps); results will be delivered when the run completes.',
          ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Scale', 'Playbook', 'State', 'Run id'])
    expect(values[1]).toBe('nightly-checks')
    expect(values[3]).toBe('r9')
    expect(container.querySelector('.tkdgv.tkgov')).toBeNull()
  })

  it("names the playbook that ran alongside the scale row, for a load_playbook call", () => {
    const { container } = render(
      <StepList
        steps={[tool({ name: 'load_playbook', args: JSON.stringify({ name: 'nightly-checks' }), result: 'ok', ok: true })]}
        running={false}
      />,
    )
    openRow(container)
    const keys = [...container.querySelectorAll('.tkdgk')].map((k) => k.textContent)
    const values = [...container.querySelectorAll('.tkdgv')].map((v) => v.textContent)
    expect(keys).toEqual(['Scale', 'Playbook', 'State'])
    expect(values[1]).toBe('nightly-checks')
  })

  it("opens the nested run through its own node id when the store holds that spawn's row", () => {
    store.set({
      ...store.get(),
      rows: [taskRow({
        id: 'n42', kind: 'spawn', status: 'running', agent: 'coder',
        nodes: [taskNode({ node_id: 'n42', agent: 'coder', status: 'running' })],
      })],
    })
    const opened: string[] = []
    setSources({ tasks: { openByNode: (id: string) => { opened.push(id); return true } } as unknown as TasksSource })
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'spawn',
          args: JSON.stringify({ task_summary: 'cross-check quotes', agent: 'coder', node_id: 'n42' }),
          result: 'done', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    fireEvent.click(container.querySelector('.tkdgv.tkgov') as HTMLElement)
    expect(opened).toEqual(['n42'])
  })

  it('leaves the task row as plain text when the spawn call names no node id', () => {
    const { container } = render(
      <StepList
        steps={[tool({ name: 'spawn', args: JSON.stringify({ task_summary: 'x', agent: 'coder' }), result: 'done', ok: true })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdgv.tkgov')).toBeNull()
  })

  it('leaves the task row as plain text when the named node is outside this list', () => {
    const { container } = render(
      <StepList
        steps={[tool({
          name: 'spawn',
          args: JSON.stringify({ task_summary: 'x', agent: 'coder', node_id: 'unknown' }),
          result: 'done', ok: true,
        })]}
        running={false}
      />,
    )
    openRow(container)
    expect(container.querySelector('.tkdgv.tkgov')).toBeNull()
  })

  it('copies the full path rather than the shortened label for a write/edit card', () => {
    const fullPath = '/very/deep/nested/workspace/path/that/is/quite/long/indeed/out.md'
    const written: string[] = []
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (s: string) => { written.push(s); return Promise.resolve() } },
    })
    const { container } = render(
      <StepList
        steps={[tool({ name: 'write_file', args: JSON.stringify({ path: fullPath, content: 'x' }), result: 'ok', ok: true })]}
        running={false}
      />,
    )
    openRow(container)
    const head = container.querySelector('.tkdtlnm')?.textContent || ''
    expect(head.startsWith('…')).toBe(true)
    expect(head).not.toBe(fullPath)
    const copyBtn = container.querySelector('.tkdtlcp') as HTMLButtonElement
    copyBtn.click()
    expect(written).toEqual([fullPath])
  })

  it('shows the write/edit +N/-M only once the call has returned', () => {
    const args = JSON.stringify({ path: '/a.py', content: 'one\ntwo\n' })
    const flying = render(<StepList steps={[tool({ name: 'write_file', args, result: null, ok: null })]} running />)
    expect(flying.container.querySelector('.tkwrow .tkdstat')).toBeNull()
    flying.unmount()
    const { container } = render(
      <StepList steps={[tool({ name: 'write_file', args, result: 'ok', ok: true, id: 'c2' })]} running={false} />,
    )
    expect(container.querySelector('.tkwrow .tkdstat .add')?.textContent).toBe('+2')
  })

  it('gives deliver_files the same document glyph a read gets, not the generic dot', () => {
    const read = render(<StepList steps={[tool({ name: 'read_file', args: JSON.stringify({ path: '/a.md' }) })]} running={false} />)
    const readIcon = read.container.querySelector('.tkic path')?.getAttribute('d')
    read.unmount()
    const { container } = render(
      <StepList steps={[tool({ name: 'deliver_files', args: JSON.stringify({ paths: ['/a.md'] }) })]} running={false} />,
    )
    expect(container.querySelector('.tkic path')?.getAttribute('d')).toBe(readIcon)
  })
})

describe('the folded summary row', () => {
  it('breathes while any of its calls is still out', () => {
    const { container } = render(
      <StepList
        steps={[
          tool({ name: 'read_file', id: 'c1', args: JSON.stringify({ path: '/a.py' }) }),
          tool({ name: 'read_file', id: 'c2', args: JSON.stringify({ path: '/b.py' }), result: null, ok: null }),
        ]}
        running
      />,
    )
    const cls = container.querySelector('.tkwrow.tksum')?.className.split(' ') ?? []
    expect(cls).toContain('tkbusy')
    expect(cls, "the in-flight token is not BoardCard's root class").not.toContain('tkrun')
  })

  it('marks a single call still out the same way, and never as a board card', () => {
    const { container } = render(
      <StepList
        steps={[tool({ name: 'read_file', id: 'c1', args: JSON.stringify({ path: '/a.py' }), result: null, ok: null })]}
        running
      />,
    )
    const cls = container.querySelector('.tkwrow')?.className.split(' ') ?? []
    expect(cls).toContain('tkbusy')
    expect(cls).not.toContain('tkrun')
  })

  it('does not breathe once every call in it has returned', () => {
    const { container } = render(
      <StepList
        steps={[
          tool({ name: 'read_file', id: 'c1', args: JSON.stringify({ path: '/a.py' }) }),
          tool({ name: 'read_file', id: 'c2', args: JSON.stringify({ path: '/b.py' }) }),
        ]}
        running={false}
      />,
    )
    expect(container.querySelector('.tkwrow.tksum')?.className).not.toContain('tkbusy')
  })
})

describe('fold memory', () => {
  it('keeps a fold open across a remount when the node key is the same', () => {
    const steps: NodeStep[] = [{ kind: 'think', text: 'first, check the dates' }]
    const first = render(<StepList steps={steps} running={false} nodeKey="dag:r1:n1" />)
    fireEvent.click(first.container.querySelector('.tkthh') as HTMLElement)
    expect(first.container.querySelector('blockquote')).toBeNull()
    first.unmount()

    const second = render(<StepList steps={steps} running={false} nodeKey="dag:r1:n1" />)
    expect(second.container.querySelector('blockquote')).toBeNull()
  })

  it('does not carry one node\'s fold onto a different node', () => {
    const steps: NodeStep[] = [{ kind: 'think', text: 'first, check the dates' }]
    const a = render(<StepList steps={steps} running={false} nodeKey="dag:r1:n1" />)
    fireEvent.click(a.container.querySelector('.tkthh') as HTMLElement)
    a.unmount()

    const b = render(<StepList steps={steps} running={false} nodeKey="dag:r1:n2" />)
    expect(b.container.querySelector('blockquote')).not.toBeNull()
  })
})

describe('the answer', () => {
  it('renders through the shared markdown pipeline', () => {
    const { container } = render(<Answer text={'**bold** and a list:\n- one\n- two'} at={null} />)
    expect(container.querySelector('.tkans.prose')).not.toBeNull()
    expect(container.querySelector('strong')?.textContent).toBe('bold')
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('shows the answer\'s own HH:MM when it has one', () => {
    const at = new Date(2026, 0, 1, 9, 5).getTime()
    const { container } = render(<Answer text="done" at={at} />)
    expect(container.querySelector('.tkturnmeta')?.textContent).toBe('09:05')
  })

  it('shows nothing where the timestamp goes when there is none', () => {
    const { container } = render(<Answer text="done" at={null} />)
    expect(container.querySelector('.tkturnmeta')).toBeNull()
  })

  it('trails a running node\'s partial answer with the streaming caret', () => {
    const { container } = render(<Answer text="still writ" at={null} running />)
    expect(container.querySelector('.tkans .tkcaret')).not.toBeNull()
  })

  it('carries no caret once the node has settled', () => {
    const { container } = render(<Answer text="done" at={null} running={false} />)
    expect(container.querySelector('.tkcaret')).toBeNull()
  })
})
