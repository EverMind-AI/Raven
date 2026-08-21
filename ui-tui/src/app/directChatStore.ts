// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { atom } from 'nanostores'

import type { InstanceRow } from '../rpc/generated.js'
import type { Msg } from '../types.js'

import { patchUiState } from './uiStore.js'

export interface DirectTargetRef {
  agent: string
  handle: string
}

export interface DirectChatState {
  // null = the main Raven conversation.  Which view is on screen; NOT which
  // conversation an arriving event belongs to -- that is read off the event's
  // own `target`, because a direct turn keeps streaming after the user leaves.
  active: DirectTargetRef | null
  // The view keys (`viewKeyOf`) whose turn is in flight. A list, not one slot:
  // each instance runs on its own lane server-side, so the main agent and any
  // number of instances can be answering at once. `uiState.busy` is this read
  // through the view on screen -- see `syncBusy`.
  running: string[]
  instances: InstanceRow[]
  pendingHandoffCount: number
  // Both keyed by directKey(agent, handle).
  scrollPos: Map<string, number>
  transcripts: Map<string, Msg[]>
}

const buildState = (): DirectChatState => ({
  active: null,
  running: [],
  instances: [],
  pendingHandoffCount: 0,
  scrollPos: new Map(),
  transcripts: new Map()
})

export const $directChat = atom<DirectChatState>(buildState())

export const getDirectChat = () => $directChat.get()

export const patchDirectChat = (next: Partial<DirectChatState>) => $directChat.set({ ...$directChat.get(), ...next })

export const resetDirectChat = () => $directChat.set(buildState())

/**
 * The map key for one instance.
 *
 * Length-prefixed rather than a bare join: a handle is free-form text the model
 * chose, so `a/b` + `c` and `a` + `b/c` would otherwise share one transcript.
 */
export const directKey = (agent: string, handle: string) => `${agent.length}:${agent}/${handle}`

export const rememberScroll = (key: string, offset: number) => {
  const scrollPos = new Map($directChat.get().scrollPos)
  scrollPos.set(key, offset)
  patchDirectChat({ scrollPos })
}

/** The scroll-memory key of the main conversation. `directKey` always starts
 * with a digit, so it can never collide with this. */
export const MAIN_VIEW_KEY = 'main'

export const viewKeyOf = (active: DirectTargetRef | null) =>
  active === null ? MAIN_VIEW_KEY : directKey(active.agent, active.handle)

// Set by the component that owns the ScrollBox. Reading the offset at switch
// time is the only moment it is still the outgoing view's: by the time an
// effect could observe the change, the rows have already been swapped and
// re-laid-out, so the number is the incoming view's.
let readScrollTop: (() => number) | null = null

export const bindScrollReader = (fn: (() => number) | null) => {
  readScrollTop = fn
}

const switchTo = (active: DirectTargetRef | null) => {
  const from = $directChat.get().active

  if (viewKeyOf(from) === viewKeyOf(active)) {
    return
  }

  rememberScroll(viewKeyOf(from), readScrollTop?.() ?? 0)
  patchDirectChat({ active })
  syncBusy()
}

export const enterDirect = (agent: string, handle: string) => switchTo({ agent, handle })

export const leaveDirect = () => switchTo(null)

export const isDirectTarget = (a: DirectTargetRef | null, b: DirectTargetRef | null) =>
  a !== null && b !== null && a.agent === b.agent && a.handle === b.handle

export const setDirectTranscript = (key: string, msgs: Msg[]) => {
  const transcripts = new Map($directChat.get().transcripts)
  transcripts.set(key, msgs)
  patchDirectChat({ transcripts })
}

export const getDirectTranscript = (key: string): Msg[] => $directChat.get().transcripts.get(key) ?? []

export const appendDirectMessage = (key: string, msg: Msg) => {
  const transcripts = new Map($directChat.get().transcripts)
  transcripts.set(key, [...(transcripts.get(key) ?? []), msg])
  patchDirectChat({ transcripts })
}

/**
 * Append to the last message when it is the same role, else start a new one.
 *
 * A reply arrives as a run of token deltas, and one message per delta would
 * render a column of one-word rows.
 */
export const appendDirectDelta = (key: string, role: Msg['role'], text: string) => {
  const transcripts = new Map($directChat.get().transcripts)
  const rows = transcripts.get(key) ?? []
  const last = rows[rows.length - 1]
  if (last !== undefined && last.role === role) {
    transcripts.set(key, [...rows.slice(0, -1), { ...last, text: last.text + text }])
  } else {
    transcripts.set(key, [...rows, { role, text }])
  }
  patchDirectChat({ transcripts })
}

export const recallScroll = (key: string) => $directChat.get().scrollPos.get(key) ?? 0

/**
 * `uiState.busy` is "is the conversation I am looking at working?".
 *
 * Kept as one boolean deliberately: every reader of it -- the spinner, the
 * status line, the queue dispatcher, the long-run tool charms -- means exactly
 * that, and would mean nothing useful if it became "is anything working". So
 * the set of running turns lives here and `busy` is its projection through the
 * view on screen, recomputed on every start, end and switch.
 */
const syncBusy = () => {
  const state = $directChat.get()
  patchUiState({ busy: state.running.includes(viewKeyOf(state.active)) })
}

export const markRunning = (target: DirectTargetRef | null) => {
  const key = viewKeyOf(target)
  const state = $directChat.get()
  if (!state.running.includes(key)) {
    patchDirectChat({ running: [...state.running, key] })
  }
  syncBusy()
}

export const clearRunningKey = (key: string) => {
  patchDirectChat({ running: $directChat.get().running.filter(k => k !== key) })
  syncBusy()
}

export const clearRunning = (target: DirectTargetRef | null) => clearRunningKey(viewKeyOf(target))

export const isRunning = (state: DirectChatState, target: DirectTargetRef | null) =>
  state.running.includes(viewKeyOf(target))

// The one line a direct view shows before its first read lands. Muted, because
// it is a status and not something anybody said.
const READING = 'reading this instance\u2019s conversation\u2026'

/**
 * Which rows the chat view shows: the main conversation, or one instance's.
 *
 * A direct view is never zero rows. An instance is read on entering, over rpc,
 * so there is a round trip during which its transcript is empty -- and a render
 * with nothing in it has nothing to paint over the rows that were on screen a
 * moment ago, so the *main* conversation stayed visible until the read landed
 * and then vanished. One line is what makes the swap paint, and saying what is
 * happening beats a blank.
 */
export const visibleRows = (state: DirectChatState, main: Msg[]): Msg[] => {
  const active = state.active

  if (active === null) {
    return main
  }

  const rows = state.transcripts.get(directKey(active.agent, active.handle)) ?? []

  return rows.length > 0 ? rows : [{ kind: 'slash', role: 'system', text: READING }]
}

/** Statuses the registry uses for a turn that has not finished. */
const IN_FLIGHT = new Set(['pending', 'running'])

/**
 * Whether the instance on screen has a turn in flight, from either signal.
 *
 * Two signals because neither sees both cases. `running` is turns dispatched
 * from here, known the instant they start and before any strip refresh. The
 * strip row is turns dispatched somewhere else -- a `spawn` the main agent made,
 * a DAG node -- which nothing tells this store about: the wire tags an instance
 * on the four events of a *direct* turn only, so `markRunning` is never reached
 * for the other two lanes. Reading only `running` is what left a spawned turn
 * showing nothing while it worked.
 */
export const isViewWorking = (state: DirectChatState): boolean => {
  const active = state.active

  if (active === null) {
    return false
  }

  if (isRunning(state, active)) {
    return true
  }

  const row = state.instances.find(r => r.agent === active.agent && r.handle === active.handle)

  return row !== undefined && IN_FLIGHT.has(row.status ?? '')
}

/**
 * Why sending is paused in the view now on screen, or null when it is live.
 *
 * Only the view's *own* turn pauses it now that each instance runs on its own
 * lane: talking to one instance while another answers is the point. What is
 * still refused is a second prompt to an instance that is mid-reply -- it would
 * serialise on that instance's handle anyway, behind a wait with no bound.
 *
 * The main agent is not covered here: its own turn is what the busy-input modes
 * (interrupt / steer / queue) are for, and those act on the turn the user is
 * looking at. A sub-agent's turn is not cancellable (spec D3), so there is
 * nothing for them to act on.
 */
export const sendingPausedReason = (state: DirectChatState): null | string => {
  const active = state.active
  if (active === null || !isRunning(state, active)) {
    return null
  }

  return `${active.agent}/${active.handle} is still replying; you can continue once it lands`
}
