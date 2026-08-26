// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { atom } from 'nanostores'

import type { DelegationStatusResponse } from '../gatewayTypes.js'

export interface DelegationState {
  // Last known caps from `delegation.status` RPC.  null until fetched.
  maxConcurrentChildren: null | number
  maxSpawnDepth: null | number
  // True when spawning is globally paused (see tools/delegate_tool.py).
  paused: boolean
  // Monotonic clock of the last successful status fetch.
  updatedAt: null | number
}

const buildState = (): DelegationState => ({
  maxConcurrentChildren: null,
  maxSpawnDepth: null,
  paused: false,
  updatedAt: null
})

export const $delegationState = atom<DelegationState>(buildState())

export const getDelegationState = () => $delegationState.get()

export const patchDelegationState = (next: Partial<DelegationState>) =>
  $delegationState.set({ ...$delegationState.get(), ...next })

export const resetDelegationState = () => $delegationState.set(buildState())

// ── Overlay accordion open-state ──────────────────────────────────────
//
// Lifted out of OverlaySection's local useState so collapse choices
// survive:
//   - navigating to a different subagent (Detail remounts)
//   - switching list ↔ detail mode (Detail unmounts in list mode)
//   - walking history (←/→)
// Keyed by section title; missing entries fall back to the section's
// `defaultOpen` prop.

export const $overlaySectionsOpen = atom<Record<string, boolean>>({})

/** Whether one section is open, by key.
 *
 * The key must identify the section AND what it belongs to. Keying it on the
 * displayed title made every node in a spawn tree share one bit per section
 * name, and a title that changes when a run finishes (`Transcript / live` ->
 * `Transcript`) swapped the state out from under the reader at exactly the
 * moment they were watching for the result. */
export const toggleOverlaySection = (key: string, defaultOpen: boolean) => {
  const state = $overlaySectionsOpen.get()
  const current = key in state ? state[key]! : defaultOpen

  $overlaySectionsOpen.set({ ...state, [key]: !current })
}

export const getOverlaySectionOpen = (key: string, defaultOpen: boolean): boolean => {
  const state = $overlaySectionsOpen.get()

  return key in state ? state[key]! : defaultOpen
}

/** Merge a raw RPC response into the store.  Tolerant of partial/omitted fields. */
export const applyDelegationStatus = (r: DelegationStatusResponse | null | undefined) => {
  if (!r) {
    return
  }

  const patch: Partial<DelegationState> = { updatedAt: Date.now() }

  if (typeof r.max_spawn_depth === 'number') {
    patch.maxSpawnDepth = r.max_spawn_depth
  }

  if (typeof r.max_concurrent_children === 'number') {
    patch.maxConcurrentChildren = r.max_concurrent_children
  }

  if (typeof r.paused === 'boolean') {
    patch.paused = r.paused
  }

  patchDelegationState(patch)
}
