// @vitest-environment happy-dom
/* The tasks tab as a Task Board: groups newest first, threads that open in
   place, chips into the task pane, the Completed fold, Unassigned, and the
   flat list a conversation with no groups still gets. */

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import * as lang from '../../state/lang'
import { resetSources, setSources } from '../../state/sources'
import { installWsPane } from '../../test/wsPaneHarness'
import * as desk from '../desk/store'
import * as store from './store'
import { TasksApp } from './TasksPage'

import type { TaskBoard, TaskGroup, TaskRow, TasksSource } from './types'

vi.mock('../transcript/source', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../transcript/source')>()),
  revealTurnAt: vi.fn(() => true),
}))

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const counts = (over: Partial<TaskRow['counts']> = {}) => (
  { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0, not_executed: 0, ...over }
)

const task = (over: Partial<TaskRow> & Pick<TaskRow, 'id' | 'kind' | 'status'>): TaskRow => ({
  task_summary: `summary of ${over.id}`, started_at: 1000, ended_at: null, agent: 'raven-code', handle: null,
  counts: counts(), nodes: [],
  ...over,
})

const NOW = Date.parse('2026-10-29T12:00:00Z')

const group = (id: string, created: number, status: TaskGroup['status'], links: [TaskRow['kind'], string][] = [], texts = [`moved ${id}`]): TaskGroup => ({
  id, title: `Matter ${id}`, status, created_at: created, updated_at: created,
  steps: texts.map((text, i) => ({
    id: `s_${id}_${i}`, turn_at: new Date(created + i * 60_000).toISOString(), turn_id: null, text,
    tasks: i === texts.length - 1 ? links.map(([kind, tid]) => ({ kind, id: tid, summary: `summary of ${tid}` })) : [],
  })),
})

let rows: TaskRow[] = []
let board: TaskBoard = { version: 1, groups: [], unassigned: [] }
let statusCalls: [string, string, string][] = []

const source = (): TasksSource => ({
  list: async () => rows,
  one: async () => null,
  stop: async () => true,
  node: async () => ({ dispatch: null, steps: [], answer: null, outputTruncated: false }),
  roster: async () => [],
  board: async () => ({ board, lineage: {} }),
  setGroupStatus: async (key, gid, status) => {
    statusCalls.push([key, gid, status])
    board = { ...board, groups: board.groups.map((g) => (g.id === gid ? { ...g, status } : g)) }
    return { board, lineage: {} }
  },
})

const draw = async (): Promise<void> => {
  render(<TasksApp />)
  await act(async () => { await store.refresh(); await store.refreshBoard() })
}

const $$ = (sel: string): HTMLElement[] => [...document.querySelectorAll<HTMLElement>(sel)]
const groupButtons = (): HTMLButtonElement[] => $$('.tkgroup') as HTMLButtonElement[]

beforeEach(() => {
  rows = []
  board = { version: 1, groups: [], unassigned: [] }
  statusCalls = []
  store._resetForTests()
  desk._resetForTests()
  installWsPane()
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({
    tasks: source(),
    workspace: { shortPath: (p: string) => p, hostPlatform: () => 'mac', canBrowse: false, openPath: () => {} },
  })
  setCurrent('s1')
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  lang._resetForTests()
  setCurrent(null)
  vi.useRealTimers()
})

describe('the task board', () => {
  it('lists open groups newest first, then a collapsed Completed fold with its count', async () => {
    board.groups = [group('a', NOW - 3e6, 'active'), group('b', NOW - 1e6, 'waiting'), group('c', NOW - 2e6, 'completed'), group('d', NOW - 5e5, 'completed')]
    await draw()

    expect(groupButtons().map((b) => b.dataset.group)).toEqual(['b', 'a'])
    const fold = document.querySelector('.tkdonefold') as HTMLButtonElement
    expect(fold.textContent).toContain('gui.tasks.board.completed {"n":2}')
    expect(fold.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(fold)
    expect(fold.getAttribute('aria-expanded')).toBe('true')
    expect(groupButtons().map((b) => b.dataset.group)).toEqual(['b', 'a', 'd', 'c'])
    expect($$('.tkgroup.tkdone').map((b) => b.dataset.group)).toEqual(['d', 'c'])
  })

  it('expands a group in place, aria-expanded true, its steps with no numbers', async () => {
    board.groups = [group('a', NOW - 3e6, 'active', [], ['found it', 'fixed it']), group('b', NOW - 1e6, 'active')]
    await draw()
    const a = groupButtons().find((b) => b.dataset.group === 'a')!

    expect(a.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(a)
    expect(a.getAttribute('aria-expanded')).toBe('true')
    const thread = document.getElementById(a.getAttribute('aria-controls')!)!
    expect([...thread.querySelectorAll('.tksteptext')].map((n) => n.textContent)).toEqual(['found it', 'fixed it'])
    expect(thread.textContent).not.toMatch(/\b1\.|\b2\./)
    expect(a.nextElementSibling).toBe(thread)
  })

  it('keeps several groups open at once', async () => {
    board.groups = [group('a', NOW - 3e6, 'active'), group('b', NOW - 1e6, 'active')]
    await draw()

    groupButtons().forEach((b) => fireEvent.click(b))
    expect(groupButtons().map((b) => b.getAttribute('aria-expanded'))).toEqual(['true', 'true'])
    expect($$('.tkthread')).toHaveLength(2)
  })

  it('opens the task pane from a chip, and leaves a chip with no row inert', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'active', [['dag', 'RUN_A'], ['spawn', 'gone_node']])]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    const chips = $$('.tkchip')
    expect(chips).toHaveLength(2)
    expect(chips[1]!.tagName).toBe('SPAN')
    expect(chips[1]!.dataset.gone).toBe('1')
    fireEvent.click(chips[0]!)
    expect(desk.get().panes.some((p) => p.id === 'task:dag:RUN_A')).toBe(true)
  })

  it('shows a running badge from the group s running rows, else the age of its last step', async () => {
    rows = [
      task({ id: 'RUN_A', kind: 'dag', status: 'running', counts: counts({ total: 4, completed: 2 }) }),
      task({ id: 'n1', kind: 'spawn', status: 'running' }),
      task({ id: 'n2', kind: 'spawn', status: 'running' }),
    ]
    board.groups = [
      group('a', NOW - 3e6, 'active', [['dag', 'RUN_A']]),
      group('b', NOW - 2e6, 'active', [['spawn', 'n1'], ['spawn', 'n2']]),
      group('c', NOW - 1e6, 'waiting'),
    ]
    await draw()
    const meta = (id: string): string => groupButtons().find((b) => b.dataset.group === id)!.querySelector('.tkgmeta')!.textContent || ''

    expect(meta('a')).toBe('gui.tasks.board.dag {"done":2,"total":4}')
    expect(meta('b')).toBe('gui.tasks.board.running {"n":2}')
    expect(meta('c')).not.toBe('')
  })

  it('marks a group completed from its thread, and reopens a completed one', async () => {
    board.groups = [group('a', NOW - 3e6, 'active'), group('z', NOW - 4e6, 'completed')]
    await draw()

    fireEvent.click(groupButtons().find((b) => b.dataset.group === 'a')!)
    await act(async () => { fireEvent.click(document.querySelector('.tkgroupact') as HTMLElement) })
    expect(statusCalls).toEqual([['s1', 'a', 'completed']])
    expect(store.get().board?.groups.find((g) => g.id === 'a')?.status).toBe('completed')

    fireEvent.click(document.querySelector('.tkdonefold') as HTMLElement)
    fireEvent.click(groupButtons().find((b) => b.dataset.group === 'z')!)
    const reopen = document.getElementById('tkthread-z')!.querySelector('.tkgroupact') as HTMLElement
    expect(reopen.textContent).toBe('gui.tasks.board.reopen')
    await act(async () => { fireEvent.click(reopen) })
    expect(statusCalls.at(-1)).toEqual(['s1', 'z', 'active'])
  })

  it('shows Unassigned only when a row is placed nowhere', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' }), task({ id: 'loose', kind: 'spawn', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'active', [['dag', 'RUN_A']])]
    await draw()

    const fold = document.querySelector('.tkunassigned') as HTMLButtonElement
    expect(fold.textContent).toContain('gui.tasks.board.unassigned')
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(fold)
    expect($$('.tklistrow .nm').map((n) => n.textContent)).toEqual(['summary of loose'])
  })

  it('has no Unassigned row when every row is placed', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'active', [['dag', 'RUN_A']])]
    await draw()

    expect(document.querySelector('.tkunassigned')).toBeNull()
  })

  it('scrolls the transcript to a step s turn when its text is clicked', async () => {
    const { revealTurnAt } = await import('../transcript/source')
    board.groups = [group('a', NOW - 3e6, 'active')]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    fireEvent.click(document.querySelector('.tksteptext') as HTMLElement)
    expect(revealTurnAt).toHaveBeenCalledWith(board.groups[0]!.steps[0]!.turn_at)
  })

  it('carries each group s status on its row', async () => {
    board.groups = (['active', 'waiting', 'blocked', 'parked'] as const).map((st, i) => group(st, NOW - (i + 1) * 1e6, st))
    await draw()

    expect(groupButtons().map((b) => [b.dataset.group, b.dataset.st])).toEqual([
      ['active', 'active'], ['waiting', 'waiting'], ['blocked', 'blocked'], ['parked', 'parked'],
    ])
  })

  it('draws the flat list a conversation with no groups has always had', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'running' })]
    await draw()

    expect(groupButtons()).toHaveLength(0)
    expect(document.querySelector('.tkunassigned')).toBeNull()
    expect($$('.tklistrow .nm').map((n) => n.textContent)).toEqual(['summary of a'])
  })
})
