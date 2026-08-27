// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { atom } from 'nanostores'
import { useSyncExternalStore } from 'react'

import type { DagRunState } from '../domain/dagRun.js'
import type { SpawnRunState } from '../domain/spawnRun.js'
import type { ActiveTool, ActivityItem, Episode, Msg, SubagentProgress, TodoItem } from '../types.js'

import { isTodoDone } from '../lib/liveProgress.js'

const buildTurnState = (): TurnState => ({
  activity: [],
  dagRuns: [],
  episodes: [],
  foldId: '',
  outcome: '',
  reasoning: '',
  reasoningActive: false,
  reasoningStreaming: false,
  reasoningTokens: 0,
  spawnRuns: [],
  streamPendingTools: [],
  streamSegments: [],
  streaming: '',
  subagents: [],
  todoCollapsed: false,
  todos: [],
  toolTokens: 0,
  tools: [],
  turnTrail: []
})

export const $turnState = atom<TurnState>(buildTurnState())

export const getTurnState = () => $turnState.get()

const subscribeTurn = (cb: () => void) => $turnState.listen(() => cb())

export const useTurnSelector = <T>(selector: (state: TurnState) => T): T =>
  useSyncExternalStore(
    subscribeTurn,
    () => selector($turnState.get()),
    () => selector($turnState.get())
  )

export const patchTurnState = (next: Partial<TurnState> | ((state: TurnState) => TurnState)) =>
  $turnState.set(typeof next === 'function' ? next($turnState.get()) : { ...$turnState.get(), ...next })

export const toggleTodoCollapsed = () => patchTurnState(state => ({ ...state, todoCollapsed: !state.todoCollapsed }))

export const archiveDoneTodos = () => archiveTodosAtTurnEnd()

export const archiveTodosAtTurnEnd = () => {
  const state = $turnState.get()

  if (!state.todos.length) {
    return []
  }

  const done = isTodoDone(state.todos)

  const msg: Msg = {
    kind: 'trail',
    role: 'system',
    text: '',
    todos: state.todos,
    ...(done ? { todoCollapsedByDefault: true } : { todoIncomplete: true })
  }

  patchTurnState({ todoCollapsed: false, todos: [] })

  return [msg]
}

export const resetTurnState = () => $turnState.set(buildTurnState())

export interface TurnState {
  activity: ActivityItem[]
  // In-flight run_subagent_dag graphs, keyed by run id in submission order. One
  // turn may issue several DAG calls, so this is a list, not a single run.
  dagRuns: DagRunState[]
  episodes: Episode[]
  // This turn's fold namespace, minted at message.start and stamped onto the
  // episodes message the turn commits. The live view and the settled row read
  // the same string, which is what keeps a fold the reader opened mid-turn open
  // once the turn lands (see `turnFoldScope`).
  foldId: string
  outcome: string
  reasoning: string
  reasoningActive: boolean
  reasoningStreaming: boolean
  reasoningTokens: number
  // In-flight spawn runs, keyed by task id in dispatch order -- the single-run
  // counterpart of `dagRuns`, folded from `subagent.status` frames that carry a
  // `tool_call_id`. Frames without one belong to no row of this turn and stay
  // on `$liveAgents` alone.
  spawnRuns: SpawnRunState[]
  streamPendingTools: string[]
  streamSegments: Msg[]
  streaming: string
  subagents: SubagentProgress[]
  todoCollapsed: boolean
  todos: TodoItem[]
  toolTokens: number
  tools: ActiveTool[]
  turnTrail: string[]
}
