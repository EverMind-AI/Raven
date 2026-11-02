// @vitest-environment happy-dom
/* The Task Board page's store: one fetch, the poll that keeps it current while
   the page is open, the badge's slower poll, and the switches that persist. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import { resetSources, setSources } from '../../state/sources'
import * as boards from './boards'

import type { TaskBoard, TaskBoardCard, TaskRow, TasksBoardsResult, TasksSource } from './types'

const card = (groupId: string, over: Partial<TaskBoardCard> = {}): TaskBoardCard => ({
  session_key: 'web:a', session_title: 'Chat a', group_id: groupId, title: `Task ${groupId}`, summary: 'where it stands',
  status: 'idle', created_at: 1, updated_at: 2, category: 'idle', running_since_ms: null, events: 1, sub_agents: 0,
  ...over,
})
const answer = (cards: TaskBoardCard[]): TasksBoardsResult => ({ cards, completed_count: 3, needs_you_count: 1 })

let replies: Array<TasksBoardsResult | Error> = []
let calls: boolean[] = []
let boardCalls: string[] = []

const asked = (questions: string[]): TaskBoard => ({
  version: 1,
  unassigned: [],
  groups: [{
    id: 'g1', title: 'Task g1', summary: '', status: 'needs_you', created_at: 1, updated_at: 2,
    steps: [{ id: 's1', turn_at: '2026-11-01T10:00:00Z', turn_id: null, title: 'moved', summary: '', questions, tasks: [] }],
  }],
})
let board: TaskBoard = asked(['Which fix?'])
const ROWS = [{ id: 'n1', kind: 'spawn', status: 'running' }] as unknown as TaskRow[]

const source = (): TasksSource => ({
  one: async () => null,
  stop: async () => true,
  node: async () => ({ dispatch: null, steps: [], answer: null, outputTruncated: false }),
  roster: async () => [],
  boards: async (includeCompleted) => {
    calls.push(includeCompleted)
    const next = replies.length > 1 ? replies.shift()! : replies[0]!
    if (next instanceof Error) throw next
    return next
  },
  board: async (key) => {
    boardCalls.push(key)
    return { board, lineage: {} }
  },
  list: async (key) => (key === 'web:a' ? ROWS : []),
  ignoreQuestion: async () => {
    board = asked([])
    return { board, lineage: {} }
  },
})

const setVisibility = (state: DocumentVisibilityState): void => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  localStorage.clear()
  replies = [answer([card('g1')])]
  calls = []
  boardCalls = []
  board = asked(['Which fix?'])
  vi.useFakeTimers()
  setSources({ tasks: source() })
  boards._resetForTests()
})

afterEach(() => {
  boards._resetForTests()
  setVisibility('visible')
  resetSources()
  vi.useRealTimers()
})

describe('the task board page store', () => {
  it('start polls every 10 seconds and stop ends it', async () => {
    boards.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(calls).toHaveLength(2)
    boards.stop()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(calls).toHaveLength(2)
  })

  it('the badge polls every 60 seconds while the page is closed', async () => {
    boards.startBadge()
    boards.startBadge()
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(calls).toHaveLength(2)
    boards.start()
    await vi.advanceTimersByTimeAsync(0)
    const withPage = calls.length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(calls.length - withPage).toBe(6)
  })

  it('a failed poll keeps the cards and says so', async () => {
    replies = [answer([card('g1'), card('g2')]), new Error('down'), answer([card('g1')])]
    await boards.refresh()
    expect(boards.get().cards).toHaveLength(2)
    await boards.refresh()
    expect(boards.get().cards).toHaveLength(2)
    expect(boards.get().error).toBe(true)
    await boards.refresh()
    expect(boards.get().error).toBe(false)
    expect(boards.get().cards).toHaveLength(1)
  })

  it('keeps the counts the server sent', async () => {
    await boards.refresh()
    expect([boards.get().completedCount, boards.get().needsYouCount, boards.get().loaded]).toEqual([3, 1, true])
  })

  it('the view and the completed switch persist', () => {
    boards.setView('list')
    boards.setShowCompleted(true)
    expect(localStorage.getItem('raven.tasks.board.view')).toBe('list')
    expect(localStorage.getItem('raven.tasks.board.completed')).toBe('true')
    boards._resetForTests()
    expect([boards.get().view, boards.get().showCompleted]).toEqual(['list', true])
  })

  it('the filter narrows the cards and their counts', async () => {
    replies = [answer([
      card('g1', { category: 'needs_you' }),
      card('g2', { session_key: 'web:b', category: 'running' }),
      card('g3', { category: 'idle' }),
    ])]
    await boards.refresh()
    boards.setFilter('web:a')
    expect(boards.visible().map((c) => c.group_id)).toEqual(['g1', 'g3'])
    expect(boards.countsOf(boards.visible())).toEqual({ needs_you: 1, running: 0, idle: 1, completed: 0 })
    boards.setFilter(null)
    expect(boards.visible()).toHaveLength(3)
  })

  it('turning completed on asks for completed cards', async () => {
    boards.setShowCompleted(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(calls.at(-1)).toBe(true)
  })

  it('a board event refreshes the open page at once', async () => {
    boards.onBoardEvent()
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(0)
    boards.start()
    await vi.advanceTimersByTimeAsync(0)
    boards.onBoardEvent()
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(2)
  })

  it('follows the page: opening it starts the poll and any switch away stops it', async () => {
    document.body.innerHTML = '<div class="app"></div>'
      + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"></section>`).join('')
      + '<aside class="detail" id="detail" data-open="false"></aside>'
    boards.openBoards()
    await vi.advanceTimersByTimeAsync(0)
    expect([page.get(), calls.length]).toEqual(['tasksBoardPage', 1])
    page.show('knowledgePage')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(calls).toHaveLength(1)
    boards.openBoards()
    page.show(null)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(calls).toHaveLength(2)
    page._resetForTests()
  })

  it('a hidden tab pauses the page poll and a visible one resumes it', async () => {
    boards.start()
    await vi.advanceTimersByTimeAsync(0)
    setVisibility('hidden')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(calls).toHaveLength(1)
    setVisibility('visible')
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(2)
  })

  it('a card unfolds into its conversation s board and Tasks, and folds again', async () => {
    const one = card('g1')
    boards.toggleCard(one)
    await vi.advanceTimersByTimeAsync(0)

    expect(boards.get().unfolded).toEqual([boards.cardKey(one)])
    expect(boardCalls).toEqual(['web:a'])
    expect(boards.get().threads['web:a']).toEqual({ board, lineage: {}, rows: ROWS })

    boards.toggleCard(one)
    expect(boards.get().unfolded).toEqual([])
  })

  it('the open page reads its unfolded cards boards again, and the badge poll never does', async () => {
    await boards.refresh()
    boards.toggleCard(card('g1'))
    await vi.advanceTimersByTimeAsync(0)

    await boards.refresh()
    expect(boardCalls).toHaveLength(1)
    boards.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(boardCalls).toHaveLength(2)
  })

  it('a write lands in its card s thread and refreshes the cards', async () => {
    await boards.refresh()
    boards.toggleCard(card('g1'))
    await vi.advanceTimersByTimeAsync(0)

    await boards.ignoreQuestion('web:a', 'g1', 's1', 'Which fix?')

    expect(boards.get().threads['web:a']!.board.groups[0]!.steps[0]!.questions).toEqual([])
    expect(boards.get().threads['web:a']!.rows).toEqual(ROWS)
    expect(calls).toHaveLength(2)
  })
})
