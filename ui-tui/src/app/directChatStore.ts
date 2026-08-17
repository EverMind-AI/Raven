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
