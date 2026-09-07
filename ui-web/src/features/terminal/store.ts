/** State and polling lifecycle for the hosted terminal tab surface. */

import { ds } from '../../shell/bridge'

import type { TerminalRow, TerminalSource } from './types'

export const TRANSCRIPT_TAB = 'raven-transcript'
export const POLL_INTERVAL_MS = 2000

export interface TerminalState {
  taskId: string | null
  terminals: TerminalRow[]
  activeTab: string
  loading: boolean
  error: string
}

const initial: TerminalState = {
  taskId: null,
  terminals: [],
  activeTab: TRANSCRIPT_TAB,
  loading: false,
  error: '',
}

let state: TerminalState = { ...initial }
let pollTimer: ReturnType<typeof setInterval> | null = null
let requestGeneration = 0
const listeners = new Set<() => void>()

export const getState = (): TerminalState => state
export const source = (): TerminalSource => ds<TerminalSource>('terminal')

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function set(patch: Partial<TerminalState>): void {
  const next = { ...state, ...patch }
  if (Object.keys(patch).every((key) => next[key as keyof TerminalState] === state[key as keyof TerminalState])) return
  state = next
  for (const listener of listeners) listener()
}

export async function refresh(): Promise<void> {
  const taskId = state.taskId
  if (!taskId) return
  const generation = requestGeneration
  set({ loading: true })
  try {
    const reply = await source().list(taskId)
    if (generation !== requestGeneration || taskId !== state.taskId) return
    const handles = new Set(reply.terminals.map((row) => row.handle))
    set({
      terminals: reply.terminals,
      activeTab:
        state.activeTab === TRANSCRIPT_TAB || handles.has(state.activeTab) ? state.activeTab : TRANSCRIPT_TAB,
      loading: false,
      error: '',
    })
  } catch (error) {
    if (generation !== requestGeneration || taskId !== state.taskId) return
    set({ loading: false, error: (error as Error)?.message || String(error) })
  }
}

export function setTask(taskId: string | null): void {
  if (state.taskId === taskId) return
  requestGeneration += 1
  set({ taskId, terminals: [], activeTab: TRANSCRIPT_TAB, loading: false, error: '' })
  if (taskId) void refresh()
}

export function selectTab(tab: string): void {
  if (tab !== TRANSCRIPT_TAB && !state.terminals.some((row) => row.handle === tab)) return
  set({ activeTab: tab })
}

export function start(): void {
  if (pollTimer) return
  pollTimer = setInterval(() => void refresh(), POLL_INTERVAL_MS)
}

export function stop(): void {
  if (!pollTimer) return
  clearInterval(pollTimer)
  pollTimer = null
}

export function _resetForTests(): void {
  stop()
  requestGeneration += 1
  state = { ...initial }
  listeners.clear()
}
