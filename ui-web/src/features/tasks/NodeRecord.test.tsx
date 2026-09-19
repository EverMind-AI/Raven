// @vitest-environment happy-dom
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetSources, setSources } from '../../state/sources'
import { actLabel } from '../transcript/source'
import { Answer, groupSteps, StepList } from './NodeRecord'

import type { TranscriptSource } from '../transcript/types'
import type { NodeStep } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const tool = (over: Partial<Extract<NodeStep, { kind: 'tool' }>> & { name: string }): NodeStep => ({
  kind: 'tool', id: 'c1', args: '{}', result: 'ok', ok: true, ...over,
})

beforeEach(() => {
  setSources({
    transcript: { clean: (s) => String(s), okOf: () => true, actLabel } as TranscriptSource,
    workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
  })
})

afterEach(() => {
  cleanup()
  resetSources()
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

  it('shows a run_subagent_dag card as a scale / state grid, with no task/run-id/elapsed row (data gap)', () => {
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
})
