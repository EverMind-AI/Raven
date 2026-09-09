// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { atom, computed } from 'nanostores'

import type { OverlayState } from './interfaces.js'

const buildOverlayState = (): OverlayState => ({
  agents: false,
  agentsFocusId: null,
  agentsInitialHistoryIndex: 0,
  approval: null,
  clarify: null,
  confirm: null,
  modelPicker: false,
  newInstance: false,
  pager: null,
  picker: false,
  secret: null,
  skillsHub: false,
  subagentsHub: false,
  sudo: null
})

export const $overlayState = atom<OverlayState>(buildOverlayState())

export const $isBlocked = computed(
  $overlayState,
  ({
    agents,
    approval,
    clarify,
    confirm,
    modelPicker,
    newInstance,
    pager,
    picker,
    secret,
    skillsHub,
    subagentsHub,
    sudo
  }) =>
    Boolean(
      agents ||
      approval ||
      clarify ||
      confirm ||
      modelPicker ||
      newInstance ||
      pager ||
      picker ||
      secret ||
      skillsHub ||
      subagentsHub ||
      sudo
    )
)

export const getOverlayState = () => $overlayState.get()

export const patchOverlayState = (next: Partial<OverlayState> | ((state: OverlayState) => OverlayState)) =>
  $overlayState.set(typeof next === 'function' ? next($overlayState.get()) : { ...$overlayState.get(), ...next })

/** Full reset — used by session/turn teardown and tests. */
export const resetOverlayState = () => $overlayState.set(buildOverlayState())

/**
 * Soft reset: drop FLOW-scoped overlays (approval / confirm / sudo / secret /
 * pager) but PRESERVE user-toggled ones — agents dashboard, model
 * picker, skills hub, session picker, new-instance picker.  Those are opened deliberately and
 * shouldn't vanish when a turn ends.  Called from turnController.idle() on
 * every turn completion / interrupt; the old "reset everything" behaviour
 * silently closed /agents the moment delegation finished.
 *
 * `clarify` is preserved too, and for a different reason: a question is not
 * turn-scoped.  A `spawn`ed sub-agent asks after the spawning turn has already
 * replied — an ACP sub-agent's `elicitation/create` arrives on a background run
 * — so dropping the sheet at idle took the question off screen while the
 * backend went on waiting out its whole budget for an answer that could no
 * longer be given.  The backend owns the lifetime instead and says when a
 * question dies, via `clarify.closed`, the same way `approval.closed` works.
 */
export const resetFlowOverlays = () =>
  $overlayState.set({
    ...buildOverlayState(),
    agents: $overlayState.get().agents,
    agentsFocusId: $overlayState.get().agentsFocusId,
    agentsInitialHistoryIndex: $overlayState.get().agentsInitialHistoryIndex,
    // A pending approval outlives the turn that opened it, exactly as a
    // pending clarify does: a sub-agent keeps working after its parent turn
    // ends, and the command it asks about is still waiting on an answer.
    // Wiped here, the prompt left the screen with nobody having answered it
    // and the call died at the runtime's hard timeout instead.
    approval: $overlayState.get().approval,
    clarify: $overlayState.get().clarify,
    modelPicker: $overlayState.get().modelPicker,
    newInstance: $overlayState.get().newInstance,
    picker: $overlayState.get().picker,
    skillsHub: $overlayState.get().skillsHub,
    subagentsHub: $overlayState.get().subagentsHub
  })
