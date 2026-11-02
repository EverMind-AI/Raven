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
import { TasksPanel } from './TasksPage'

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

const group = (id: string, created: number, status: TaskGroup['status'], links: [TaskRow['kind'], string][] = [], titles = [`moved ${id}`], summary = `where ${id} stands`): TaskGroup => ({
  id, title: `Matter ${id}`, summary, status, created_at: created, updated_at: created,
  steps: titles.map((title, i) => ({
    id: `s_${id}_${i}`, turn_at: new Date(created + i * 60_000).toISOString(), turn_id: null, title,
    summary: `what ${title} moved`, questions: [],
    tasks: i === titles.length - 1 ? links.map(([kind, tid]) => ({ kind, id: tid, summary: `summary of ${tid}` })) : [],
  })),
})

/* The questions the group's latest event asked. */
const asking = (g: TaskGroup, questions: string[]): TaskGroup => ({
  ...g, steps: g.steps.map((s, i) => (i === g.steps.length - 1 ? { ...s, questions } : s)),
})

let rows: TaskRow[] = []
let board: TaskBoard = { version: 1, groups: [], unassigned: [] }
let statusCalls: [string, string, string][] = []
let ignoreCalls: [string, string, string, string][] = []

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
  /* The server's rule, as the board model keeps it: the question leaves its
     step, and a group whose latest step then asks nothing goes idle. */
  ignoreQuestion: async (key, gid, sid, question) => {
    ignoreCalls.push([key, gid, sid, question])
    board = {
      ...board,
      groups: board.groups.map((g) => {
        if (g.id !== gid) return g
        const steps = g.steps.map((st) => (st.id === sid ? { ...st, questions: st.questions.filter((q) => q !== question) } : st))
        const waits = steps[steps.length - 1]!.questions.length > 0
        return { ...g, steps, status: g.status === 'needs_you' && !waits ? 'idle' : g.status }
      }),
    }
    return { board, lineage: {} }
  },
})

const draw = async (): Promise<void> => {
  render(<TasksPanel />)
  await act(async () => { await store.refresh(); await store.refreshBoard() })
}

const $$ = (sel: string): HTMLElement[] => [...document.querySelectorAll<HTMLElement>(sel)]
const groupButtons = (): HTMLButtonElement[] => $$('.tkgroup') as HTMLButtonElement[]

beforeEach(() => {
  rows = []
  board = { version: 1, groups: [], unassigned: [] }
  statusCalls = []
  ignoreCalls = []
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
    board.groups = [group('a', NOW - 3e6, 'idle'), group('b', NOW - 1e6, 'needs_you'), group('c', NOW - 2e6, 'completed'), group('d', NOW - 5e5, 'completed')]
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
    board.groups = [group('a', NOW - 3e6, 'idle', [], ['found it', 'fixed it']), group('b', NOW - 1e6, 'idle')]
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
    board.groups = [group('a', NOW - 3e6, 'idle'), group('b', NOW - 1e6, 'idle')]
    await draw()

    groupButtons().forEach((b) => fireEvent.click(b))
    expect(groupButtons().map((b) => b.getAttribute('aria-expanded'))).toEqual(['true', 'true'])
    expect($$('.tkthread')).toHaveLength(2)
  })

  it('opens the task pane from a chip, and leaves a chip with no row inert', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'idle', [['dag', 'RUN_A'], ['spawn', 'gone_node']])]
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
      group('a', NOW - 3e6, 'idle', [['dag', 'RUN_A']]),
      group('b', NOW - 2e6, 'idle', [['spawn', 'n1'], ['spawn', 'n2']]),
      group('c', NOW - 1e6, 'needs_you'),
    ]
    await draw()
    const meta = (id: string): string => groupButtons().find((b) => b.dataset.group === id)!.querySelector('.tkgmeta')!.textContent || ''

    expect(meta('a')).toBe('gui.tasks.board.dag {"done":2,"total":4}')
    expect(meta('b')).toBe('gui.tasks.board.running {"n":2}')
    expect(meta('c')).not.toBe('')
  })

  it('marks a group completed from its thread, and reopens a completed one', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle'), group('z', NOW - 4e6, 'completed')]
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
    expect(statusCalls.at(-1)).toEqual(['s1', 'z', 'idle'])
  })

  it('shows Unassigned only when a row is placed nowhere', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' }), task({ id: 'loose', kind: 'spawn', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'idle', [['dag', 'RUN_A']])]
    await draw()

    const fold = document.querySelector('.tkunassigned') as HTMLButtonElement
    expect(fold.textContent).toContain('gui.tasks.board.unassigned')
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(fold)
    expect($$('.tklistrow .nm').map((n) => n.textContent)).toEqual(['summary of loose'])
  })

  it('has no Unassigned row when every row is placed', async () => {
    rows = [task({ id: 'RUN_A', kind: 'dag', status: 'completed' })]
    board.groups = [group('a', NOW - 3e6, 'idle', [['dag', 'RUN_A']])]
    await draw()

    expect(document.querySelector('.tkunassigned')).toBeNull()
  })

  it('scrolls the transcript to a step s turn when its text is clicked', async () => {
    const { revealTurnAt } = await import('../transcript/source')
    board.groups = [group('a', NOW - 3e6, 'idle')]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    fireEvent.click(document.querySelector('.tksteptext') as HTMLElement)
    expect(revealTurnAt).toHaveBeenCalledWith(board.groups[0]!.steps[0]!.turn_at)
  })

  it('shows, under a needs_you group s latest event, that event s summary', async () => {
    board.groups = [group('a', NOW - 3e6, 'needs_you', [], ['asked once', 'asked again'])]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    const steps = $$('.tkstep')
    expect(steps[1]!.querySelector('.tkstepsum')?.textContent).toBe('what asked again moved')
    expect(steps[0]!.querySelector('.tkstepsum')).toBeNull()
    expect(steps.map((s) => s.hasAttribute('data-asks'))).toEqual([false, true])
  })

  it('shows no event summary once the group no longer needs the user', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle')]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    expect(document.querySelector('.tkstepsum')).toBeNull()
    expect(document.querySelector('.tkstep[data-asks]')).toBeNull()
  })

  it('takes the reader to the event s turn from its summary too', async () => {
    const { revealTurnAt } = await import('../transcript/source')
    vi.mocked(revealTurnAt).mockClear()
    board.groups = [group('a', NOW - 3e6, 'needs_you')]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    fireEvent.click(document.querySelector('.tkstepsum') as HTMLElement)
    expect(revealTurnAt).toHaveBeenCalledWith(board.groups[0]!.steps[0]!.turn_at)
  })

  it('frames, on a needs_you card, the questions its latest event asked -- folded or open', async () => {
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you', [], ['asked once', 'asked again']), ['Which fix?', 'Clear it too?'])]
    await draw()

    const listed = (): (string | null)[] => $$('.tkq .tkqtext').map((q) => q.textContent)
    expect(listed()).toEqual(['Which fix?', 'Clear it too?'])
    fireEvent.click(groupButtons()[0]!)
    expect(listed()).toEqual(['Which fix?', 'Clear it too?'])
  })

  it('frames each question in a box of its own, with an Ignore of its own', async () => {
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you'), ['Which fix?', 'Clear it too?'])]
    await draw()

    expect($$('.tkq').map((q) => [q.querySelector('.tkqtext')?.textContent, q.querySelectorAll('button.tkqignore').length]))
      .toEqual([['Which fix?', 1], ['Clear it too?', 1]])
  })

  it('an Ignore sets its one question aside, and the answer draft drops it too', async () => {
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you'), ['Which fix?', 'Clear it too?'])]
    await draw()

    await act(async () => { fireEvent.click($$('.tkq')[1]!.querySelector('.tkqignore') as HTMLElement) })
    expect(ignoreCalls).toEqual([['s1', 'a', board.groups[0]!.steps[0]!.id, 'Clear it too?']])
    expect($$('.tkq .tkqtext').map((q) => q.textContent)).toEqual(['Which fix?'])
    expect(store.answerDraft(board.groups[0]!).text).not.toContain('Clear it too?')
  })

  it('setting the last question aside leaves the group idle and the card without questions', async () => {
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you'), ['Which fix?'])]
    await draw()

    await act(async () => { fireEvent.click(document.querySelector('.tkqignore') as HTMLElement) })
    expect(board.groups[0]!.status).toBe('idle')
    expect(document.querySelector('.tkask')).toBeNull()
  })

  /* Ignore, then Answer on the right: the order the confirm sheet keeps, the
     way ahead last. */
  /* The questions belong to the group's latest progress: folded, they close the
     card under its head; open, they follow the last event, and the row of
     actions -- Mark completed, Ignore, Answer -- closes the card. */
  it('puts the questions at the bottom of the card, after the latest event', async () => {
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you', [], ['asked once', 'asked again']), ['Which fix?'])]
    await draw()

    const card = $$('.tkgcard')[0]!
    expect(card.lastElementChild?.className).toBe('tkask')
    fireEvent.click(groupButtons()[0]!)

    const thread = card.querySelector('.tkthread')!
    expect(card.lastElementChild).toBe(thread)
    const latest = [...thread.querySelectorAll('.tkstep')].at(-1)!
    const list = thread.querySelector('.tkasklist')!
    expect(latest.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect([...thread.lastElementChild!.querySelectorAll('button')].map((b) => b.className)).toEqual([
      'tkgroupact', 'tkmove', 'tkaskbtn',
    ])
    expect(card.querySelectorAll('.tkasklist')).toHaveLength(1)
  })

  it('offers Ignore and Answer only while the group needs the user', async () => {
    board.groups = [
      asking(group('a', NOW - 3e6, 'needs_you'), ['Which fix?']),
      asking(group('b', NOW - 4e6, 'idle'), ['Which fix?']),
    ]
    await draw()

    expect($$('.tkgcard').map((c) => [...c.querySelectorAll<HTMLElement>('.tkaskbtn')].map((b) => b.dataset.kind)))
      .toEqual([['answer'], []])
    expect($$('.tkasklist')).toHaveLength(1)
  })

  it('offers both to a needs_you group whose latest event named no question, with no list', async () => {
    board.groups = [group('a', NOW - 3e6, 'needs_you')]
    await draw()

    expect(document.querySelector('.tkasklist')).toBeNull()
    expect($$('.tkaskbtn').map((b) => b.dataset.kind)).toEqual(['ignore', 'answer'])
  })

  it('Answer hands the composer this group s answer draft', async () => {
    const seen: [string, number][] = []
    store.lendAnswer((text, caret) => { seen.push([text, caret]) })
    board.groups = [asking(group('a', NOW - 3e6, 'needs_you'), ['Which fix?'])]
    await draw()

    fireEvent.click(document.querySelector('.tkaskbtn[data-kind="answer"]') as HTMLElement)
    const draft = store.answerDraft(board.groups[0]!)
    expect(seen).toEqual([[draft.text, draft.caret]])
    store.lendAnswer(null)
  })

  it('a group that asks no question keeps the one Ignore, which sets it idle', async () => {
    board.groups = [group('a', NOW - 3e6, 'needs_you')]
    await draw()

    await act(async () => { fireEvent.click(document.querySelector('.tkaskbtn[data-kind="ignore"]') as HTMLElement) })
    expect(statusCalls.at(-1)).toEqual(['s1', 'a', 'idle'])
    expect(document.querySelector('.tkask')).toBeNull()
  })

  it('carries each group s status on its row', async () => {
    board.groups = (['needs_you', 'running', 'idle'] as const).map((st, i) => group(st, NOW - (i + 1) * 1e6, st))
    await draw()

    expect(groupButtons().map((b) => [b.dataset.group, b.dataset.st])).toEqual([
      ['needs_you', 'needs_you'], ['running', 'running'], ['idle', 'idle'],
    ])
  })

  it('shows the group s summary under its title in plain text, not its last step', async () => {
    board.groups = [group('a', NOW - 1e6, 'idle', [], ['found it', 'fixed it'], 'fix in, the user checks it')]
    await draw()

    const line = groupButtons()[0]!.querySelector('.st')!
    expect(line.textContent).toBe('fix in, the user checks it')
    expect(line.tagName).toBe('SPAN')
  })

  it('draws no second line for a group with no summary yet', async () => {
    board.groups = [group('a', NOW - 1e6, 'idle', [], ['found it'], '')]
    await draw()

    expect(groupButtons()[0]!.querySelector('.st')).toBeNull()
  })

  it('draws a flat row s second line in plain text, not a struck-through s', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'completed', ended_at: 61_000 })]
    await draw()

    const line = document.querySelector('.tklistrow .st')!
    expect(line.textContent).not.toBe('')
    expect(line.tagName).toBe('SPAN')
  })

  it('marks each event by the Tasks it started: spinning while one runs, failed when one failed, else done', async () => {
    rows = [
      task({ id: 'n1', kind: 'spawn', status: 'running' }),
      task({ id: 'n2', kind: 'spawn', status: 'completed' }),
      task({ id: 'n3', kind: 'spawn', status: 'failed' }),
    ]
    const g = group('a', NOW - 3e6, 'idle', [], ['dispatched one', 'finished one', 'lost one', 'just talked'])
    const started = [['n1'], ['n2'], ['n3'], []]
    g.steps.forEach((s, i) => { s.tasks = started[i]!.map((id) => ({ kind: 'spawn' as const, id, summary: id })) })
    board.groups = [g]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    const events = $$('.tkstep')
    expect(events.map((e) => e.dataset.st)).toEqual(['run', 'ok', 'error', 'ok'])
    expect(events.map((e) => e.querySelector('.tksr')?.textContent)).toEqual([
      'gui.tasks.running', 'gui.tasks.st_completed', 'gui.tasks.st_failed', 'gui.tasks.st_completed',
    ])

    rows = [{ ...rows[0]!, status: 'completed' }, rows[1]!, rows[2]!]
    await act(async () => { await store.refresh() })
    expect($$('.tkstep')[0]!.dataset.st).toBe('ok')
  })

  it('draws the flat list a conversation with no groups has always had', async () => {
    rows = [task({ id: 'a', kind: 'dag', status: 'running' })]
    await draw()

    expect(groupButtons()).toHaveLength(0)
    expect(document.querySelector('.tkunassigned')).toBeNull()
    expect($$('.tklistrow .nm').map((n) => n.textContent)).toEqual(['summary of a'])
  })
})

describe('a focus request from the task board page', () => {
  let scrolled: Element[] = []
  const real = Element.prototype.scrollIntoView

  beforeEach(() => {
    scrolled = []
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) } as Element['scrollIntoView']
  })

  afterEach(() => {
    Element.prototype.scrollIntoView = real
  })

  const button = (id: string): HTMLButtonElement => groupButtons().find((b) => b.dataset.group === id)!

  it('a focus request expands its group once and clears', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle'), group('b', NOW - 1e6, 'idle')]
    store.requestFocus('s1', 'a')
    await draw()

    expect(button('a').getAttribute('aria-expanded')).toBe('true')
    expect(button('b').getAttribute('aria-expanded')).toBe('false')
    expect(scrolled).toEqual([button('a')])
    expect(store.focusFor('s1')).toBe(null)
  })

  it('a focus request for a group the board lacks is dropped', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle')]
    store.requestFocus('s1', 'gone')
    await draw()

    expect(store.focusFor('s1')).toBe(null)
    expect(button('a').getAttribute('aria-expanded')).toBe('false')
    expect(scrolled).toEqual([])
  })

  it('a focus request on a board with no groups is dropped too', async () => {
    store.requestFocus('s1', 'a')
    await draw()

    expect(store.focusFor('s1')).toBe(null)
  })

  it('a focus request on a completed group opens the Completed fold to it', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle'), group('c', NOW - 2e6, 'completed')]
    store.requestFocus('s1', 'c')
    await draw()

    expect(document.querySelector('.tkdonefold')!.getAttribute('aria-expanded')).toBe('true')
    expect(button('c').getAttribute('aria-expanded')).toBe('true')
    expect(scrolled).toEqual([button('c')])
  })

  it('a focus request for another conversation waits for it', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle')]
    store.requestFocus('s2', 'a')
    await draw()

    expect(button('a').getAttribute('aria-expanded')).toBe('false')
    expect(store.focusFor('s2')).toBe('a')
    expect(store.focusFor('s1')).toBe(null)
  })

  it('a focus request outlives the reset a conversation switch makes', () => {
    store.requestFocus('s2', 'a')
    store.reset()

    expect(store.focusFor('s2')).toBe('a')
    store.clearFocus()
    expect(store.focusFor('s2')).toBe(null)
  })

  it('a request made while the board is already drawn still lands', async () => {
    board.groups = [group('a', NOW - 3e6, 'idle')]
    await draw()
    await act(async () => { store.requestFocus('s1', 'a') })

    expect(button('a').getAttribute('aria-expanded')).toBe('true')
    expect(store.focusFor('s1')).toBe(null)
  })
})

describe('the group cards', () => {
  const pill = (id: string): HTMLElement =>
    groupButtons().find((b) => b.dataset.group === id)!.querySelector<HTMLElement>('.tkpill')!

  it('draws each group as a card: its status as a pill, the meta, the title and the summary', async () => {
    board.groups = [group('a', NOW - 1e6, 'needs_you', [], ['asked'], 'two findings so far')]
    await draw()

    const card = document.querySelector<HTMLElement>('.tkboard .tkgcard')!
    const head = card.querySelector<HTMLElement>('.tkgroup')!
    expect([pill('a').dataset.tone, pill('a').textContent]).toEqual(['needs_you', 'gui.tasks.page.col_needs_you'])
    expect(groupButtons()[0]!.hasAttribute('title')).toBe(false)
    expect(head.querySelector('.nm')!.textContent).toBe('Matter a')
    expect(head.querySelector('.st')!.textContent).toBe('two findings so far')
    expect(head.querySelector('.tkgmeta')!.textContent).not.toBe('')
  })

  it('a Task still running makes an open group running, and none makes it idle, whatever the model wrote', async () => {
    rows = [
      task({ id: 'n1', kind: 'spawn', status: 'running' }),
      task({ id: 'n2', kind: 'spawn', status: 'running' }),
      task({ id: 'n3', kind: 'spawn', status: 'completed' }),
    ]
    board.groups = [
      group('a', NOW - 1e6, 'idle', [['spawn', 'n1']]),
      group('w', NOW - 2e6, 'needs_you', [['spawn', 'n2']]),
      group('r', NOW - 3e6, 'running', [['spawn', 'n3']]),
      group('p', NOW - 4e6, 'idle'),
    ]
    await draw()

    expect([pill('a').dataset.tone, pill('a').textContent]).toEqual(['running', 'gui.tasks.page.col_running'])
    expect([pill('w').dataset.tone, pill('w').textContent]).toEqual(['needs_you', 'gui.tasks.page.col_needs_you'])
    expect([pill('r').dataset.tone, pill('r').textContent]).toEqual(['idle', 'gui.tasks.page.col_idle'])
    expect([pill('p').dataset.tone, pill('p').textContent]).toEqual(['idle', 'gui.tasks.page.col_idle'])
  })

  it('a completed group wears the completed pill, and reopening it makes it idle', async () => {
    board.groups = [group('a', NOW - 1e6, 'idle'), group('z', NOW - 2e6, 'completed')]
    await draw()
    fireEvent.click(document.querySelector('.tkdonefold') as HTMLElement)

    expect([pill('z').dataset.tone, pill('z').textContent]).toEqual(['completed', 'gui.tasks.page.col_completed'])
    fireEvent.click(groupButtons().find((b) => b.dataset.group === 'z')!)
    await act(async () => { fireEvent.click(document.getElementById('tkthread-z')!.querySelector('.tkgroupact') as HTMLElement) })
    expect(statusCalls.at(-1)).toEqual(['s1', 'z', 'idle'])
  })

  it('a folded card counts its events and sub-agents, and an open one shows its thread instead', async () => {
    rows = [
      task({ id: 'RUN_A', kind: 'dag', status: 'completed', counts: counts({ total: 2 }) }),
      task({ id: 'n1', kind: 'spawn', status: 'completed' }),
    ]
    board.groups = [group('a', NOW - 1e6, 'idle', [['dag', 'RUN_A'], ['spawn', 'n1'], ['spawn', 'gone']], ['one', 'two'])]
    await draw()

    const foot = document.querySelector('.tkgcard .tkgfoot')!
    expect(foot.textContent).toContain('gui.tasks.page.events {"n":2}')
    expect(foot.textContent).toContain('gui.tasks.page.sub_agents {"n":4}')

    fireEvent.click(groupButtons()[0]!)
    expect(document.querySelector('.tkgcard .tkgfoot')).toBeNull()
    expect(document.querySelector('.tkgcard #tkthread-a')).not.toBeNull()
  })

  it('moves a group on from its folded foot and from its open thread, without folding or unfolding it', async () => {
    const focus = vi.fn()
    store.lendFocus(focus)
    board.groups = [group('a', NOW - 1e6, 'idle')]
    await draw()

    expect(document.querySelector('.tkgroup .tkgfoot')).toBeNull()
    fireEvent.click(document.querySelector('.tkgcard > .tkgfoot .tkmove') as HTMLElement)
    expect(store.moveOnFor('s1')).toEqual({ sessionKey: 's1', groupId: 'a', title: 'Matter a', summary: 'where a stands' })
    expect(groupButtons()[0]!.getAttribute('aria-expanded')).toBe('false')
    expect(focus).toHaveBeenCalledTimes(1)

    store.clearMoveOn()
    fireEvent.click(groupButtons()[0]!)
    fireEvent.click(document.querySelector('#tkthread-a .tkthreadacts .tkmove') as HTMLElement)
    expect(store.moveOnFor('s1')?.groupId).toBe('a')
    store.lendFocus(null)
  })

  it('counts a Task placed on several events once', async () => {
    rows = [task({ id: 'n1', kind: 'spawn', status: 'completed' })]
    const g = group('a', NOW - 1e6, 'idle', [['spawn', 'n1']], ['sent it', 'checked on it'])
    g.steps[0]!.tasks = [...g.steps[1]!.tasks]
    board.groups = [g]
    await draw()

    expect(document.querySelector('.tkgcard .tkgfoot')!.textContent).toContain('gui.tasks.page.sub_agents {"n":1}')
  })

  it('keeps the thread and its action inside the card, the thread right after the head', async () => {
    board.groups = [group('a', NOW - 1e6, 'idle')]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    const card = document.querySelector('.tkgcard')!
    const thread = document.getElementById('tkthread-a')!
    expect(card.contains(thread) && card.contains(thread.querySelector('.tkgroupact'))).toBe(true)
    expect(groupButtons()[0]!.nextElementSibling).toBe(thread)
  })

  it('names the Task kind apart in a chip and keeps the chip s words', async () => {
    rows = [task({ id: 'n1', kind: 'spawn', status: 'completed', task_summary: 'look' })]
    board.groups = [group('a', NOW - 1e6, 'idle', [['spawn', 'n1']])]
    await draw()
    fireEvent.click(groupButtons()[0]!)

    const chip = document.querySelector('.tkchip')!
    expect(chip.querySelector('.tkchipk')!.textContent).toBe('spawn')
    expect(chip.textContent).toBe('spawn · look')
  })

  it('leaves the flat list of a conversation with no groups as it was', async () => {
    rows = [task({ id: 'n1', kind: 'spawn', status: 'completed' })]
    await draw()

    expect(document.querySelector('.tkboard')).toBeNull()
    expect(document.querySelector('.tkgcard')).toBeNull()
  })
})
