// @vitest-environment happy-dom
/* The Task Board page: the columns, the card face, and the footer each
   category speaks; a card unfolding its events in place, and an event going to
   its conversation. */

import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { setCurrent } from '../../lib/session'
import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import * as registry from '../../state/session/registry'
import { resetSources, setSources } from '../../state/sources'
import * as ws from '../../state/ws'
import { revealTurnAt } from '../transcript/source'
import * as boards from './boards'
import { BoardView, ListView, TasksApp } from './BoardsPage'
import * as tasks from './store'

import type { RailSource, SessRow } from '../rail/types'
import type { CardThread } from './boards'
import type { ProgressStep, TaskBoard, TaskBoardCard, TaskGroup, TaskRow, TasksBoardsResult, TasksSource } from './types'

vi.mock('../transcript/source', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../transcript/source')>()),
  revealTurnAt: vi.fn(() => true),
}))

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const NOW = Date.parse('2026-11-01T12:00:00Z')
const MIN = 60_000
const DAY = 24 * 60 * MIN

const card = (groupId: string, over: Partial<TaskBoardCard> = {}): TaskBoardCard => ({
  session_key: 'web:a', session_title: 'Chat a', group_id: groupId, title: `Task ${groupId}`,
  summary: `where ${groupId} stands`, status: 'idle', created_at: NOW - DAY, updated_at: NOW - DAY,
  category: 'idle', running_since_ms: null, events: 1, sub_agents: 0,
  ...over,
})

const said = (key: string, vars: Record<string, unknown>): string => `${key} ${JSON.stringify(vars)}`

const T1 = '2026-11-01T10:00:00Z'
const T2 = '2026-11-01T11:00:00Z'

const step = (id: string, turnAt: string, over: Partial<ProgressStep> = {}): ProgressStep => ({
  id, turn_at: turnAt, turn_id: null, title: `moved ${id}`, summary: `what ${id} moved`, questions: [], tasks: [],
  ...over,
})

const groupOf = (gid: string, status: TaskGroup['status'], steps: ProgressStep[]): TaskGroup => ({
  id: gid, title: `Task ${gid}`, summary: `where ${gid} stands`, status, created_at: NOW - DAY, updated_at: NOW - DAY, steps,
})

const boardWith = (...groups: TaskGroup[]): TaskBoard => ({ version: 1, groups, unassigned: [] })

const spawn = (id: string, status: TaskRow['status']): TaskRow => ({
  id, kind: 'spawn', status, task_summary: `summary of ${id}`, started_at: 1000, ended_at: null, agent: 'raven-code', handle: null,
  counts: { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0, not_executed: 0 },
  nodes: [],
})

/* g1 waits on two questions its second event asked; its first event started a
   sub-agent that still runs. */
const asking = (): TaskGroup => groupOf('g1', 'needs_you', [
  step('s1', T1, { tasks: [{ kind: 'spawn', id: 'n1', summary: 'look' }] }),
  step('s2', T2, { summary: 'Asked which fix', questions: ['Which fix?', 'Ship today?'] }),
])

const threadOf = (board: TaskBoard, rows: TaskRow[] = []): CardThread => ({ board, lineage: {}, rows })

const draw = (
  cards: TaskBoardCard[], showCompleted = false, toggle = vi.fn(),
  unfolded: string[] = [], threads: Record<string, CardThread> = {},
) => render(<BoardView cards={cards} showCompleted={showCompleted} unfolded={unfolded} threads={threads} toggle={toggle} />)

const columns = (root: HTMLElement): HTMLElement[] => Array.from(root.querySelectorAll<HTMLElement>('.tkbcol'))
const column = (root: HTMLElement, cat: string): HTMLElement => root.querySelector<HTMLElement>(`.tkbcol[data-cat="${cat}"]`)!

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date', 'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  setTranslator((key, vars) => (vars ? said(key, vars) : key))
})

afterEach(() => {
  cleanup()
  resetTranslator()
  vi.useRealTimers()
})

describe('the board view', () => {
  it('the board shows three columns, completed only with the switch', () => {
    const cards = [card('g1', { category: 'needs_you', status: 'needs_you' }), card('g2', { category: 'idle' }), card('g3', { category: 'idle' })]
    const { container, unmount } = draw(cards)

    expect(columns(container).map((c) => c.dataset.cat)).toEqual(['needs_you', 'running', 'idle'])
    const head = (cat: string) => column(container, cat).querySelector('.tkbhead')!.textContent
    expect(head('needs_you')).toBe('gui.tasks.page.col_needs_you1')
    expect(head('running')).toBe('gui.tasks.page.col_running0')
    expect(head('idle')).toBe('gui.tasks.page.col_idle2')
    unmount()

    const shown = draw([...cards, card('g4', { category: 'completed', status: 'completed' })], true)
    expect(columns(shown.container).map((c) => c.dataset.cat)).toEqual(['needs_you', 'running', 'idle', 'completed'])
    expect(column(shown.container, 'completed').querySelector('.tkbhead')!.textContent).toBe('gui.tasks.page.col_completed1')
  })

  it('each card names its conversation, title, summary and counters', () => {
    const { container } = draw([card('g1', { session_title: 'Fix the login page', events: 2, sub_agents: 3 })])

    const face = container.querySelector<HTMLElement>('.tkbcard')!
    expect(face.querySelector('.tkbconv')!.textContent).toBe('Fix the login page')
    expect(face.textContent).toContain('Task g1')
    expect(face.textContent).toContain('where g1 stands')
    expect(face.textContent).toContain(said('gui.tasks.page.events', { n: 2 }))
    expect(face.textContent).toContain(said('gui.tasks.page.sub_agents', { n: 3 }))
  })

  it('a card s face is a button that unfolds the card in place', () => {
    const toggle = vi.fn()
    const one = card('g1')
    const { container } = draw([one], false, toggle)

    const face = container.querySelector<HTMLButtonElement>('.tkbcard > .tkbface')!
    expect(face.tagName).toBe('BUTTON')
    expect(face.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(face)
    expect(toggle).toHaveBeenCalledWith(one)
    expect(container.querySelector('.tkthread')).toBeNull()
  })

  it('an unfolded card lists its events, then the questions its latest event asked', () => {
    const one = card('g1', { category: 'needs_you', status: 'needs_you' })
    const { container } = draw([one], false, vi.fn(), [boards.cardKey(one)], { 'web:a': threadOf(boardWith(asking())) })

    const face = container.querySelector<HTMLElement>('.tkbcard > .tkbface')!
    const thread = container.querySelector<HTMLElement>('.tkbcard > .tkthread')!
    expect(face.getAttribute('aria-expanded')).toBe('true')
    expect(face.getAttribute('aria-controls')).toBe(thread.id)
    expect(Array.from(thread.querySelectorAll('.tkstept')).map((t) => t.textContent)).toEqual(['moved s1', 'moved s2'])
    expect(thread.querySelector('.tkstepsum')!.textContent).toBe('Asked which fix')
    expect(Array.from(thread.querySelectorAll('.tkq .tkqtext')).map((q) => q.textContent)).toEqual(['Which fix?', 'Ship today?'])
    expect(thread.querySelectorAll('.tkq .tkqignore')).toHaveLength(2)
    expect(thread.querySelector('.tkaskbtn[data-kind="answer"]')).not.toBeNull()
  })

  it('an unfolded card marks the event whose work still runs, and names no Task it cannot open', () => {
    const one = card('g1', { category: 'needs_you', status: 'needs_you' })
    const threads = { 'web:a': threadOf(boardWith(asking()), [spawn('n1', 'running')]) }
    const { container } = draw([one], false, vi.fn(), [boards.cardKey(one)], threads)

    expect(Array.from(container.querySelectorAll<HTMLElement>('.tkstep')).map((s) => s.dataset.st)).toEqual(['run', 'ok'])
    expect(container.querySelector('.tkchip')).toBeNull()
  })

  it('an unfolded card whose board has not come yet shows its face alone', () => {
    const one = card('g1')
    const { container } = draw([one], false, vi.fn(), [boards.cardKey(one)])

    expect(container.querySelector('.tkbcard > .tkbface')!.getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelector('.tkthread')).toBeNull()
  })

  it('the footer says the card s type and its time, the same way for all four', () => {
    const { container } = draw([
      card('ask', { category: 'needs_you', status: 'needs_you', updated_at: NOW - DAY }),
      card('run', { category: 'running', status: 'running', running_since_ms: NOW - 6 * MIN }),
      card('rest', { category: 'idle', updated_at: NOW - 2 * DAY }),
      card('done', { category: 'completed', status: 'completed', created_at: NOW - 80 * MIN, updated_at: NOW - 8 * MIN }),
    ], true)
    const face = (gid: string): HTMLElement =>
      Array.from(container.querySelectorAll<HTMLElement>('.tkbcard')).find((c) => c.textContent!.includes(`Task ${gid}`))!

    expect(face('ask').textContent).toContain(said('gui.tasks.page.foot_needs_you', { t: said('gui.time.for_d', { n: 1 }) }))
    expect(face('ask').querySelector('.tkbdot[data-cat="needs_you"]')).not.toBeNull()

    expect(face('run').querySelector('.tkbring[data-st="run"]')).not.toBeNull()
    expect(face('run').textContent).toContain(said('gui.tasks.page.foot_running', { t: '6m 0s' }))

    expect(face('rest').textContent).toContain(said('gui.tasks.page.foot_idle', { t: said('gui.time.for_d', { n: 2 }) }))
    expect(face('rest').querySelector('.tkbdot[data-cat="idle"]')).not.toBeNull()

    expect(face('done').textContent).toContain(said('gui.tasks.page.foot_completed', { t: '1h 12m' }))
    expect(face('done').querySelector('.tkbdot[data-cat="completed"]')).not.toBeNull()
    expect(container.querySelector('.tkbbang')).toBeNull()
  })

  it('cards in a column are newest first', () => {
    const { container } = draw([card('older', { updated_at: 1 }), card('newer', { updated_at: 2 })])

    const titles = Array.from(column(container, 'idle').querySelectorAll('.tkbtitle')).map((t) => t.textContent)
    expect(titles).toEqual(['Task newer', 'Task older'])
  })

  it('an empty column says so', () => {
    const { container } = draw([card('g1', { category: 'idle' })])

    expect(column(container, 'running').querySelector('.tkbcard')).toBeNull()
    expect(column(container, 'running').textContent).toContain('gui.tasks.page.empty_col')
    expect(column(container, 'idle').textContent).not.toContain('gui.tasks.page.empty_col')
  })
})

const list = (
  cards: TaskBoardCard[], showCompleted = false, toggle = vi.fn(),
  unfolded: string[] = [], threads: Record<string, CardThread> = {},
) => render(<ListView cards={cards} showCompleted={showCompleted} unfolded={unfolded} threads={threads} toggle={toggle} />)

const chips = (root: HTMLElement): string[] =>
  Array.from(root.querySelectorAll<HTMLElement>('.tkbchip')).map((c) => c.textContent!)
const rows = (root: HTMLElement): string[] =>
  Array.from(root.querySelectorAll<HTMLElement>('.tkbrow .tkbtitle')).map((t) => t.textContent!)

const mixed = (): TaskBoardCard[] => [
  card('ask', { category: 'needs_you', status: 'needs_you', updated_at: NOW - 1 }),
  card('go', { category: 'running', running_since_ms: NOW - MIN, updated_at: NOW - 2 }),
  card('go2', { category: 'running', running_since_ms: NOW - MIN, updated_at: NOW - 3 }),
  card('rest', { category: 'idle', updated_at: NOW - 4 }),
  card('done', { category: 'completed', status: 'completed', updated_at: NOW - 5 }),
]

describe('the list view', () => {
  it('the list chips count each category and All leaves completed out', () => {
    const { container, unmount } = list(mixed().filter((c) => c.category !== 'completed'))
    expect(chips(container)).toEqual([
      'gui.tasks.page.all4', 'gui.tasks.page.col_needs_you1', 'gui.tasks.page.col_running2', 'gui.tasks.page.col_idle1',
    ])
    unmount()

    const shown = list(mixed(), true)
    expect(chips(shown.container)).toEqual([
      'gui.tasks.page.all4', 'gui.tasks.page.col_needs_you1', 'gui.tasks.page.col_running2', 'gui.tasks.page.col_idle1',
      'gui.tasks.page.col_completed1',
    ])
  })

  it('a chip filters the list to its category', () => {
    const { container } = list(mixed())
    const chip = (word: string) => Array.from(container.querySelectorAll<HTMLButtonElement>('.tkbchip'))
      .find((c) => c.textContent!.startsWith(word))!

    expect(chip('gui.tasks.page.all').getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(chip('gui.tasks.page.col_running'))

    expect(rows(container)).toEqual(['Task go', 'Task go2'])
    expect(chip('gui.tasks.page.col_running').getAttribute('aria-pressed')).toBe('true')
    expect(chip('gui.tasks.page.all').getAttribute('aria-pressed')).toBe('false')
  })

  it('rows sit in sections in column order', () => {
    const { container } = list([...mixed()].reverse())

    const heads = Array.from(container.querySelectorAll<HTMLElement>('.tkbsec')).map((s) => s.dataset.cat)
    expect(heads).toEqual(['needs_you', 'running', 'idle'])
    expect(rows(container)).toEqual(['Task ask', 'Task go', 'Task go2', 'Task rest'])
  })

  it('the completed section starts collapsed', () => {
    const { container } = list(mixed(), true)

    const fold = container.querySelector<HTMLButtonElement>('.tkbsec[data-cat="completed"] .tkbfold')!
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    expect(rows(container)).not.toContain('Task done')

    fireEvent.click(fold)
    expect(fold.getAttribute('aria-expanded')).toBe('true')
    expect(rows(container).at(-1)).toBe('Task done')
  })

  it('a picked Completed chip falls back to All when the switch takes it away', () => {
    const { container, rerender } = list(mixed(), true)
    fireEvent.click(container.querySelector<HTMLButtonElement>('.tkbchip[data-cat="completed"]')!)
    expect(rows(container)).toEqual(['Task done'])

    rerender(<ListView cards={mixed().filter((c) => c.category !== 'completed')} showCompleted={false} unfolded={[]} threads={{}} toggle={vi.fn()} />)

    expect(container.querySelector('.tkbchip[data-cat="all"]')!.getAttribute('aria-pressed')).toBe('true')
    expect(rows(container)).toEqual(['Task ask', 'Task go', 'Task go2', 'Task rest'])
  })

  it('a row speaks its status on the right and unfolds in place', () => {
    const toggle = vi.fn()
    const one = card('ask', { category: 'needs_you', status: 'needs_you', updated_at: NOW - 3 * 60 * MIN })
    const { container } = list([one], false, toggle)

    const face = container.querySelector<HTMLButtonElement>('.tkbrow > .tkbface')!
    expect(face.querySelector('.tkbmark .tkbdot[data-cat="needs_you"]')).not.toBeNull()
    expect(face.querySelector('.tkbmeta')!.textContent).toContain(said('gui.tasks.page.foot_needs_you', { t: said('gui.time.for_h', { n: 3 }) }))
    expect(face.querySelector('.tkbconvt')!.textContent).toBe('Chat a')
    expect(face.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(face)
    expect(toggle).toHaveBeenCalledWith(one)
  })

  it('an unfolded row shows its events under it', () => {
    const one = card('g1', { category: 'needs_you', status: 'needs_you' })
    const { container } = list([one], false, vi.fn(), [boards.cardKey(one)], { 'web:a': threadOf(boardWith(asking())) })

    const thread = container.querySelector<HTMLElement>('.tkbrow > .tkthread')!
    expect(container.querySelector('.tkbrow > .tkbface')!.getAttribute('aria-controls')).toBe(thread.id)
    expect(Array.from(thread.querySelectorAll('.tkstept')).map((t) => t.textContent)).toEqual(['moved s1', 'moved s2'])
    expect(thread.querySelectorAll('.tkq')).toHaveLength(2)
  })
})

describe('the page', () => {
  let railRows: SessRow[] = []
  let asked: boolean[] = []
  let replies: Array<TasksBoardsResult | Error> = []

  const answer = (): TasksBoardsResult => ({
    cards: [
      card('g1', { session_key: 'web:a', session_title: 'Chat a', category: 'needs_you', status: 'needs_you', updated_at: NOW - 1 }),
      card('g2', { session_key: 'web:b', session_title: 'Chat b', category: 'idle', updated_at: NOW - 2 }),
    ],
    completed_count: 2,
    needs_you_count: 1,
  })

  /* Each conversation's own board, and what was asked of it. */
  let boardOf: Record<string, TaskBoard> = {}
  let rowsOf: Record<string, TaskRow[]> = {}
  let boardAsks: string[] = []
  let writes: string[][] = []

  const tasksSource = (): TasksSource => ({
    list: async (key) => rowsOf[key] ?? [],
    one: async () => null,
    stop: async () => true,
    node: async () => ({ dispatch: null, steps: [], answer: null, outputTruncated: false }),
    roster: async () => [],
    boards: async (includeCompleted) => {
      asked.push(includeCompleted)
      const next = replies.length > 1 ? replies.shift()! : replies[0]!
      if (next instanceof Error) throw next
      return next
    },
    board: async (key) => {
      boardAsks.push(key)
      return { board: boardOf[key] ?? boardWith(), lineage: {} }
    },
    /* The server's rule: the question leaves its step, and a group whose
       latest step then asks nothing goes idle. */
    ignoreQuestion: async (key, gid, sid, question) => {
      writes.push(['ignore', key, gid, sid, question])
      boardOf[key] = boardWith(...boardOf[key]!.groups.map((g) => {
        if (g.id !== gid) return g
        const steps = g.steps.map((s) => (s.id === sid ? { ...s, questions: s.questions.filter((q) => q !== question) } : s))
        return { ...g, steps, status: steps.at(-1)!.questions.length ? g.status : 'idle' }
      }))
      return { board: boardOf[key]!, lineage: {} }
    },
    setGroupStatus: async (key, gid, status) => {
      writes.push(['status', key, gid, status])
      boardOf[key] = boardWith(...boardOf[key]!.groups.map((g) => (g.id === gid ? { ...g, status } : g)))
      return { board: boardOf[key]!, lineage: {} }
    },
  })

  const settle = async (): Promise<void> => {
    for (let i = 0; i < 6; i++) await act(async () => {})
  }

  const mount = async (): Promise<HTMLElement> => {
    const { container } = render(<TasksApp />)
    await act(async () => { await boards.refresh() })
    return container
  }

  const cardOf = (root: HTMLElement, gid: string): HTMLElement =>
    Array.from(root.querySelectorAll<HTMLElement>('.tkbcard, .tkbrow')).find((c) => c.textContent!.includes(`Task ${gid}`))!

  const faceOf = (root: HTMLElement, gid: string): HTMLButtonElement =>
    cardOf(root, gid).querySelector<HTMLButtonElement>(':scope > .tkbface')!

  /* A card's face unfolds it; its thread is there once its board has come. */
  const unfold = async (root: HTMLElement, gid: string): Promise<HTMLElement> => {
    if (faceOf(root, gid).getAttribute('aria-expanded') !== 'true') fireEvent.click(faceOf(root, gid))
    await settle()
    return cardOf(root, gid).querySelector<HTMLElement>(':scope > .tkthread')!
  }

  const goToEvent = async (root: HTMLElement, gid: string, n = 0): Promise<void> => {
    const thread = await unfold(root, gid)
    fireEvent.click(thread.querySelectorAll<HTMLButtonElement>('.tksteptext')[n]!)
    await settle()
  }

  beforeEach(() => {
    document.body.innerHTML = '<div class="app"></div>'
      + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"></section>`).join('')
      + '<aside class="detail" id="detail" data-open="false"></aside>'
    railRows = []
    asked = []
    replies = [answer()]
    boardOf = { 'web:a': boardWith(asking()), 'web:b': boardWith(groupOf('g2', 'idle', [step('s3', T1)])) }
    rowsOf = { 'web:a': [spawn('n1', 'running')] }
    boardAsks = []
    writes = []
    localStorage.clear()
    setSources({
      tasks: tasksSource(),
      rail: { snapshot: () => ({ rows: railRows, cur: null, busy: false }) } as unknown as RailSource,
    })
    boards._resetForTests()
    tasks._resetForTests()
    vi.mocked(revealTurnAt).mockClear()
    setCurrent('web:z')
  })

  afterEach(() => {
    boards._resetForTests()
    page._resetForTests()
    tasks.lendAnswer(null)
    resetSources()
    setCurrent(null)
    vi.restoreAllMocks()
  })

  it('the toolbar switches views and completed, and filters by conversation', async () => {
    const root = await mount()
    expect(root.querySelectorAll('.tkbcol')).toHaveLength(3)

    fireEvent.click(root.querySelector('.tkbseg [data-view="list"]')!)
    expect(boards.get().view).toBe('list')
    expect(root.querySelector('.tkbchip')).not.toBeNull()
    expect(root.querySelector('.tkbseg [data-view="list"]')!.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(root.querySelector('.tkbseg [data-view="board"]')!)
    expect(boards.get().view).toBe('board')

    const sw = root.querySelector<HTMLButtonElement>('.tkbswitch')!
    expect(sw.textContent).toBe(said('gui.tasks.page.show_completed', { n: 2 }))
    expect(sw.getAttribute('aria-checked')).toBe('false')
    await act(async () => { fireEvent.click(sw) })
    expect([boards.get().showCompleted, asked.at(-1), sw.getAttribute('aria-checked')]).toEqual([true, true, 'true'])

    const pick = root.querySelector<HTMLSelectElement>('select.tkbconvsel')!
    expect(Array.from(pick.options).map((o) => [o.value, o.textContent])).toEqual([
      ['', 'gui.tasks.page.all_conversations'], ['web:a', 'Chat a'], ['web:b', 'Chat b'],
    ])
    fireEvent.change(pick, { target: { value: 'web:b' } })
    expect(Array.from(root.querySelectorAll('.tkbcard .tkbtitle')).map((t) => t.textContent)).toEqual(['Task g2'])
    fireEvent.change(pick, { target: { value: '' } })
    expect(root.querySelectorAll('.tkbcard')).toHaveLength(2)
  })

  it('a card unfolds its events and questions in place, and the page stays', async () => {
    const switchTo = vi.spyOn(registry, 'switchTo').mockImplementation(async () => {})
    page.show('tasksBoardPage')
    const root = await mount()

    const thread = await unfold(root, 'g1')

    expect(boardAsks).toEqual(['web:a'])
    expect(Array.from(thread.querySelectorAll('.tkstept')).map((t) => t.textContent)).toEqual(['moved s1', 'moved s2'])
    expect(thread.querySelector('.tkstep[data-st="run"] .tkstept')!.textContent).toBe('moved s1')
    expect(Array.from(thread.querySelectorAll('.tkq .tkqtext')).map((q) => q.textContent)).toEqual(['Which fix?', 'Ship today?'])
    expect([switchTo.mock.calls.length, page.get()]).toEqual([0, 'tasksBoardPage'])

    fireEvent.click(faceOf(root, 'g1'))
    expect(cardOf(root, 'g1').querySelector('.tkthread')).toBeNull()
  })

  it('an event opens its conversation, the tasks tab on its group, and that turn s reply', async () => {
    const order: string[] = []
    const switchTo = vi.spyOn(registry, 'switchTo').mockImplementation(async (s) => {
      order.push(`switch:${s.id}`)
      setCurrent(s.id)
    })
    const refreshList = vi.spyOn(registry, 'refreshList').mockImplementation(async () => { order.push('list') })
    vi.spyOn(ws, 'pick').mockImplementation((view) => { order.push(`pick:${view}`) })
    vi.mocked(revealTurnAt).mockImplementation((at) => { order.push(`reveal:${at}`); return true })
    railRows = [{ id: 'web:a', title: 'Chat a' }]
    page.show('tasksBoardPage')
    const root = await mount()

    await goToEvent(root, 'g1', 1)

    expect(order).toEqual(['switch:web:a', 'pick:tasks', `reveal:${T2}`])
    expect(switchTo.mock.calls[0]![0]).toBe(railRows[0])
    expect(refreshList).not.toHaveBeenCalled()
    expect(tasks.focusFor('web:a')).toBe('g1')
    expect(page.get()).toBe(null)
  })

  it('a conversation the rail does not hold is listed once, then opened bare if still absent', async () => {
    const switchTo = vi.spyOn(registry, 'switchTo').mockImplementation(async (s) => { setCurrent(s.id) })
    const refreshList = vi.spyOn(registry, 'refreshList').mockImplementation(async () => {})
    vi.spyOn(ws, 'pick').mockImplementation(() => {})
    const root = await mount()

    await goToEvent(root, 'g2')

    expect(refreshList).toHaveBeenCalledTimes(1)
    expect(switchTo.mock.calls[0]![0]).toEqual({ id: 'web:b', title: 'Chat b' })

    const listed: SessRow = { id: 'web:b', title: 'Chat b (listed)' }
    refreshList.mockImplementation(async () => { railRows.push(listed) })
    setCurrent('web:z')
    await goToEvent(root, 'g2')

    expect(refreshList).toHaveBeenCalledTimes(2)
    expect(switchTo.mock.calls[1]![0]).toBe(listed)
  })

  it('an event another switch overtook opens no tab, scrolls nothing and takes back only its own request', async () => {
    const pending: Array<() => void> = []
    vi.spyOn(registry, 'switchTo').mockImplementation(() => new Promise<void>((resolve) => { pending.push(resolve) }))
    const pick = vi.spyOn(ws, 'pick').mockImplementation(() => {})
    railRows = [{ id: 'web:a', title: 'Chat a' }]
    const root = await mount()

    await goToEvent(root, 'g1')
    expect(tasks.focusFor('web:a')).toBe('g1')
    setCurrent('web:elsewhere')
    pending.shift()!()
    await settle()

    expect(pick).not.toHaveBeenCalled()
    expect(revealTurnAt).not.toHaveBeenCalled()
    expect(tasks.focusFor('web:a')).toBe(null)

    await goToEvent(root, 'g1')
    tasks.requestFocus('web:b', 'g2')
    setCurrent('web:b')
    pending.shift()!()
    await settle()

    expect(pick).not.toHaveBeenCalled()
    expect(revealTurnAt).not.toHaveBeenCalled()
    expect(tasks.focusFor('web:b')).toBe('g2')
  })

  it('an event of the conversation already open skips the switch', async () => {
    const switchTo = vi.spyOn(registry, 'switchTo').mockImplementation(async () => {})
    const pick = vi.spyOn(ws, 'pick').mockImplementation(() => {})
    setCurrent('web:a')
    page.show('tasksBoardPage')
    const root = await mount()

    await goToEvent(root, 'g1')

    expect(switchTo).not.toHaveBeenCalled()
    expect(pick).toHaveBeenCalledWith('tasks')
    expect(revealTurnAt).toHaveBeenCalledWith(T1)
    expect([tasks.focusFor('web:a'), page.get()]).toEqual(['g1', null])
  })

  it('Ignore and Mark completed write to the card s own conversation, not the one on screen', async () => {
    const root = await mount()
    const thread = await unfold(root, 'g1')

    fireEvent.click(thread.querySelector<HTMLButtonElement>('.tkq .tkqignore')!)
    await settle()
    expect(writes).toEqual([['ignore', 'web:a', 'g1', 's2', 'Which fix?']])
    expect(Array.from(cardOf(root, 'g1').querySelectorAll('.tkq .tkqtext')).map((q) => q.textContent)).toEqual(['Ship today?'])
    expect(asked).toHaveLength(2)

    fireEvent.click(cardOf(root, 'g1').querySelector<HTMLButtonElement>('.tkgroupact')!)
    await settle()
    expect(writes.at(-1)).toEqual(['status', 'web:a', 'g1', 'completed'])
    expect(asked).toHaveLength(3)
  })

  it('a write from the page reaches the sidebar when the card s conversation is the one on screen', async () => {
    setCurrent('web:a')
    const root = await mount()
    const thread = await unfold(root, 'g1')

    fireEvent.click(thread.querySelector<HTMLButtonElement>('.tkq .tkqignore')!)
    await settle()

    expect(tasks.get().board!.groups[0]!.steps.at(-1)!.questions).toEqual(['Ship today?'])
  })

  it('Answer opens the card s conversation, then drafts the answers there', async () => {
    const order: string[] = []
    vi.spyOn(registry, 'switchTo').mockImplementation(async (s) => {
      order.push(`switch:${s.id}`)
      setCurrent(s.id)
    })
    vi.spyOn(ws, 'pick').mockImplementation((view) => { order.push(`pick:${view}`) })
    const drafts: string[] = []
    tasks.lendAnswer((draft) => { order.push('answer'); drafts.push(draft) })
    railRows = [{ id: 'web:a', title: 'Chat a' }]
    const root = await mount()
    const thread = await unfold(root, 'g1')

    fireEvent.click(thread.querySelector<HTMLButtonElement>('.tkaskbtn[data-kind="answer"]')!)
    await settle()

    expect(order).toEqual(['switch:web:a', 'pick:tasks', 'answer'])
    expect(drafts[0]).toContain('1. Which fix?')
    expect(drafts[0]).toContain('2. Ship today?')
  })

  it('an unfolded card s board is read again with every refresh, a folded one s never', async () => {
    page.show('tasksBoardPage')
    const root = await mount()
    await unfold(root, 'g1')

    await act(async () => { await boards.refresh() })

    expect(boardAsks).toEqual(['web:a', 'web:a'])
  })

  it('Move on opens the card s conversation, then holds the group for its next message', async () => {
    const order: string[] = []
    vi.spyOn(registry, 'switchTo').mockImplementation(async (s) => {
      order.push(`switch:${s.id}`)
      setCurrent(s.id)
    })
    vi.spyOn(ws, 'pick').mockImplementation((view) => { order.push(`pick:${view}`) })
    railRows = [{ id: 'web:a', title: 'Chat a' }]
    page.show('tasksBoardPage')
    const root = await mount()

    expect(cardOf(root, 'g1').querySelector('.tkbface .tkmove')).toBeNull()
    fireEvent.click(cardOf(root, 'g1').querySelector<HTMLButtonElement>(':scope > .tkbfoot .tkmove')!)
    await settle()

    expect(order).toEqual(['switch:web:a', 'pick:tasks'])
    expect(tasks.moveOnFor('web:a')).toEqual({ sessionKey: 'web:a', groupId: 'g1', title: 'Task g1', summary: 'where g1 stands' })
    expect(faceOf(root, 'g1').getAttribute('aria-expanded')).toBe('false')
    expect(page.get()).toBe(null)
  })

  it('a list row moves its group on too', async () => {
    vi.spyOn(registry, 'switchTo').mockImplementation(async (s) => { setCurrent(s.id) })
    vi.spyOn(ws, 'pick').mockImplementation(() => {})
    railRows = [{ id: 'web:b', title: 'Chat b' }]
    const root = await mount()
    fireEvent.click(root.querySelector('.tkbseg [data-view="list"]')!)

    fireEvent.click(cardOf(root, 'g2').querySelector<HTMLButtonElement>(':scope > .tkmove')!)
    await settle()

    expect(tasks.moveOnFor('web:b')).toEqual({ sessionKey: 'web:b', groupId: 'g2', title: 'Task g2', summary: 'where g2 stands' })
  })

  it('a Move on another switch overtook holds nothing', async () => {
    const pending: Array<() => void> = []
    vi.spyOn(registry, 'switchTo').mockImplementation(() => new Promise<void>((resolve) => { pending.push(resolve) }))
    vi.spyOn(ws, 'pick').mockImplementation(() => {})
    railRows = [{ id: 'web:a', title: 'Chat a' }]
    const root = await mount()

    fireEvent.click(cardOf(root, 'g1').querySelector<HTMLButtonElement>(':scope > .tkbfoot .tkmove')!)
    await settle()
    setCurrent('web:elsewhere')
    pending.shift()!()
    await settle()

    expect(tasks.moveOnFor('web:a')).toBeNull()
  })

  it('a card is named by its title, its category and its conversation, in both views', async () => {
    const root = await mount()
    const name = said('gui.tasks.page.card_label', { title: 'Task g1', cat: 'gui.tasks.page.col_needs_you', conv: 'Chat a' })

    expect(faceOf(root, 'g1').getAttribute('aria-label')).toBe(name)
    fireEvent.click(root.querySelector('.tkbseg [data-view="list"]')!)
    expect(faceOf(root, 'g1').getAttribute('aria-label')).toBe(name)
  })

  it('a picked conversation whose cards have all gone falls back to every conversation', async () => {
    const root = await mount()
    fireEvent.change(root.querySelector('select.tkbconvsel')!, { target: { value: 'web:b' } })
    expect(boards.get().filter).toBe('web:b')

    replies = [{ ...answer(), cards: answer().cards.filter((c) => c.session_key !== 'web:b') }]
    await act(async () => { await boards.refresh() })

    expect(boards.get().filter).toBe(null)
    expect(Array.from(root.querySelectorAll('.tkbcard .tkbtitle')).map((t) => t.textContent)).toEqual(['Task g1'])
  })

  it('a failed refresh keeps the last answer and says so', async () => {
    replies = [answer(), new Error('down')]
    const root = await mount()
    expect(root.querySelector('.tkberr')).toBeNull()

    await act(async () => { await boards.refresh() })

    expect(root.querySelector('.tkberr')!.textContent).toBe('gui.tasks.page.error')
    expect(root.querySelectorAll('.tkbcard')).toHaveLength(2)
  })
})
