/* The Task Board page's data: every host conversation's cards from
 * `tasks.boards`, kept current by a poll while the page is open, and by a
 * slower one for the rail's badge while it is not; and, for each card the
 * reader unfolds in place, its conversation's board.
 *
 * A poll rather than the board events: the gateway sends a client the events of
 * the conversations it subscribed to, so the page would never hear the others.
 * The one event it does hear still refreshes at once (`onBoardEvent`).
 */

import * as page from '../../state/page'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import * as tasks from './store'

import type { PageId } from '../../state/page'
import type { TaskBoard, TaskBoardCard, TaskGroup, TaskRow, TasksBoardResult } from './types'

export type BoardsView = 'board' | 'list'
export type Category = TaskBoardCard['category']

/* The four types a Task Group can be in, the page's and the sidebar's alike. */
export const CATEGORY_WORD: Record<Category, string> = {
  needs_you: 'gui.tasks.page.col_needs_you',
  running: 'gui.tasks.page.col_running',
  idle: 'gui.tasks.page.col_idle',
  completed: 'gui.tasks.page.col_completed',
}

/* The server's own rule (overview.category_of), for the sidebar, which reads
   the conversation's board and rows rather than the page's cards: what the
   model marked as needing the owner, or completed, stands; any other group
   runs only while a Task it links still runs. */
export function categoryOf(status: TaskGroup['status'], running: boolean): Category {
  if (status === 'needs_you' || status === 'completed') return status
  return running ? 'running' : 'idle'
}

/* What an unfolded card draws: its conversation's board, and that
   conversation's Tasks, which say whether each event's work still runs. */
export interface CardThread {
  board: TaskBoard
  lineage: Record<string, string>
  rows: TaskRow[]
}

export interface BoardsState {
  cards: TaskBoardCard[]
  completedCount: number
  needsYouCount: number
  view: BoardsView
  showCompleted: boolean
  filter: string | null
  error: boolean
  loaded: boolean
  /* The cards unfolded in place (`cardKey`), and each of their
     conversations' thread, keyed by session. */
  unfolded: string[]
  threads: Record<string, CardThread>
}

export const cardKey = (card: Pick<TaskBoardCard, 'session_key' | 'group_id'>): string => `${card.session_key}/${card.group_id}`

export const PAGE_ID: PageId = 'tasksBoardPage'

const PAGE_POLL_MS = 10_000
const BADGE_POLL_MS = 60_000
const VIEW_KEY = 'raven.tasks.board.view'
const COMPLETED_KEY = 'raven.tasks.board.completed'

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function persist(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* A storage the browser refuses leaves the choice for this load only. */
  }
}

const initialState = (): BoardsState => ({
  cards: [],
  completedCount: 0,
  needsYouCount: 0,
  view: stored(VIEW_KEY) === 'list' ? 'list' : 'board',
  showCompleted: stored(COMPLETED_KEY) === 'true',
  filter: null,
  error: false,
  loaded: false,
  unfolded: [],
  threads: {},
})

const store = makeStore<BoardsState>(initialState())
export const { get, set, subscribe } = store

let open = false
let pageTimer: ReturnType<typeof setInterval> | null = null
let badgeTimer: ReturnType<typeof setInterval> | null = null

export async function refresh(): Promise<void> {
  const ask = sources.tasks?.boards
  if (!ask) return
  try {
    const got = await ask(store.get().showCompleted)
    store.set((s) => ({
      ...s,
      cards: got.cards,
      completedCount: got.completed_count,
      needsYouCount: got.needs_you_count,
      error: false,
      loaded: true,
    }))
  } catch {
    store.set((s) => ({ ...s, error: true }))
    return
  }
  /* The badge's poll wants the count alone: unfolded cards are read again
     only while the page that shows them is open. */
  if (open) await Promise.all(unfoldedSessions().map(readThread))
}

function unfoldedSessions(): string[] {
  const { cards, unfolded } = store.get()
  return [...new Set(cards.filter((c) => unfolded.includes(cardKey(c))).map((c) => c.session_key))]
}

async function readThread(sessionKey: string): Promise<void> {
  const src = sources.tasks
  if (!src?.board) return
  try {
    const [got, rows] = await Promise.all([src.board(sessionKey), src.list(sessionKey)])
    keepThread(sessionKey, got, rows)
  } catch {
    /* The thread drawn stays; the open page's next refresh asks again. */
  }
}

function keepThread(sessionKey: string, got: TasksBoardResult, rows?: TaskRow[]): void {
  store.set((s) => ({
    ...s,
    threads: {
      ...s.threads,
      [sessionKey]: { board: got.board, lineage: got.lineage || {}, rows: rows ?? s.threads[sessionKey]?.rows ?? [] },
    },
  }))
}

/* A card unfolds in place, its conversation's board read as it does; a
   second press folds it. */
export function toggleCard(card: TaskBoardCard): void {
  const key = cardKey(card)
  const was = store.get().unfolded.includes(key)
  store.set((s) => ({ ...s, unfolded: was ? s.unfolded.filter((k) => k !== key) : [...s.unfolded, key] }))
  if (!was) void readThread(card.session_key)
}

/* The reader's writes on an unfolded card go to that card's conversation,
   which need not be the one on screen. The board they answer with is the
   card's thread now, the sidebar's too when that conversation is the one open
   there, and the cards are read again for where the group stands. */
async function adopt(sessionKey: string, got: TasksBoardResult): Promise<void> {
  keepThread(sessionKey, got)
  tasks.onBoardUpdated({ session_key: sessionKey, board: got.board, lineage: got.lineage })
  await refresh()
}

export async function ignoreQuestion(sessionKey: string, groupId: string, stepId: string, question: string): Promise<void> {
  const src = sources.tasks
  if (!src?.ignoreQuestion) return
  await adopt(sessionKey, await src.ignoreQuestion(sessionKey, groupId, stepId, question))
}

export async function setGroupStatus(sessionKey: string, groupId: string, status: 'completed' | 'idle'): Promise<void> {
  const src = sources.tasks
  if (!src?.setGroupStatus) return
  await adopt(sessionKey, await src.setGroupStatus(sessionKey, groupId, status))
}

const hidden = (): boolean => typeof document !== 'undefined' && document.visibilityState === 'hidden'

function runPagePoll(): void {
  if (!open || pageTimer !== null || hidden()) return
  void refresh()
  pageTimer = setInterval(() => void refresh(), PAGE_POLL_MS)
}

function pausePagePoll(): void {
  if (pageTimer === null) return
  clearInterval(pageTimer)
  pageTimer = null
}

/* The page is shown: answer now, then every PAGE_POLL_MS while the tab is visible. */
export function start(): void {
  open = true
  runPagePoll()
}

export function stop(): void {
  open = false
  pausePagePoll()
}

/* The badge's own poll, idle while the page's runs: the page's answer carries
   the same count. */
export function startBadge(): void {
  if (badgeTimer !== null) return
  if (!open) void refresh()
  badgeTimer = setInterval(() => {
    if (!open) void refresh()
  }, BADGE_POLL_MS)
}

export function onBoardEvent(): void {
  if (open) void refresh()
}

export function openBoards(): void {
  page.show(PAGE_ID)
}

/* Followed off the page switch rather than started by `openBoards`, the way
   state/hub.ts follows it: Escape, another rail row and a conversation all
   leave the page without passing through this module. */
page.subscribe(() => (page.get() === PAGE_ID ? start() : stop()))

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => (hidden() ? pausePagePoll() : runPagePoll()))
}

export function setView(view: BoardsView): void {
  persist(VIEW_KEY, view)
  store.set((s) => ({ ...s, view }))
}

export function setShowCompleted(on: boolean): void {
  persist(COMPLETED_KEY, String(on))
  store.set((s) => ({ ...s, showCompleted: on }))
  void refresh()
}

export function setFilter(sessionKey: string | null): void {
  store.set((s) => ({ ...s, filter: sessionKey }))
}

export function visible(cards: TaskBoardCard[] = store.get().cards): TaskBoardCard[] {
  const { filter } = store.get()
  return filter === null ? cards : cards.filter((c) => c.session_key === filter)
}

export function countsOf(cards: TaskBoardCard[]): Record<Category, number> {
  const counts: Record<Category, number> = { needs_you: 0, running: 0, idle: 0, completed: 0 }
  for (const c of cards) counts[c.category] += 1
  return counts
}

export function _resetForTests(): void {
  stop()
  if (badgeTimer !== null) {
    clearInterval(badgeTimer)
    badgeTimer = null
  }
  store.set(initialState())
}
