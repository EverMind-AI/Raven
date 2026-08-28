// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Which spawn panels' trace boxes are expanded.
//
// A module store for the same reasons `dagOpenNodes` is one: the panel has two
// render sites and the transcript remounts it when a turn settles, so state
// held in the component would collapse at exactly the moment the run finished.
//
// Unlike a dag node, a spawn's box has a *moving* default: open while the run
// still works -- the live trace is the panel's whole point, and a reader should
// not have to click to see the sub-agent moving -- and closed once it settles,
// when the transcript wants its rows back. So the store holds overrides rather
// than the open set itself, and `spawnTraceOpen` resolves a panel against them.

import { atom } from 'nanostores'

import type { SpawnRunState } from '../domain/spawnRun.js'

import { spawnRunSettled } from '../domain/spawnRun.js'

export const $spawnOpenOverrides = atom<ReadonlyMap<string, boolean>>(new Map())

/** The run's key in the shared trace store (`dagNodeStore`). Keyed by the
 * record id, since that is what `subagent.context` reads; the `spawn/` scope
 * cannot collide with a `runId/nodeId` key -- run ids carry a timestamp. */
export const spawnTraceKey = (callId: string): string => `spawn/${callId}`

/** Open while the run works, closed once it settles; a reader's toggle wins. */
export const spawnTraceOpen = (
  run: Pick<SpawnRunState, 'status' | 'taskId'>,
  overrides: ReadonlyMap<string, boolean>
): boolean => overrides.get(run.taskId) ?? !spawnRunSettled(run)

export const toggleSpawnTrace = (run: Pick<SpawnRunState, 'status' | 'taskId'>): void => {
  const overrides = $spawnOpenOverrides.get()
  const next = new Map(overrides)

  next.set(run.taskId, !spawnTraceOpen(run, overrides))

  // A new map every time: the store publishes by identity, so mutating in
  // place would not re-render the panel.
  $spawnOpenOverrides.set(next)
}

export const resetSpawnOpen = (): void => $spawnOpenOverrides.set(new Map())
