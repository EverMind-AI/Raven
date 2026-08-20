// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Which DAG node rows are expanded to their full prompt.
//
// A module store rather than panel state because the panel has two render sites
// (the episodes view draws a graph under its own tool row, the legacy transcript
// draws the turn's live graphs) and because the transcript remounts it: a work
// segment renders its graph from a different branch once the turn settles, which
// would collapse an expanded prompt at the moment the run finished.

import { atom } from 'nanostores'

export const $dagOpenNodes = atom<ReadonlySet<string>>(new Set())

/** Node ids are unique only within a run, so a bare id would expand the
 * same-named node of every graph in the transcript. */
export const dagNodeKey = (runId: string, nodeId: string) => `${runId}/${nodeId}`

export const toggleDagNode = (key: string) => {
  const next = new Set($dagOpenNodes.get())

  if (!next.delete(key)) {
    next.add(key)
  }

  // A new set every time: the store publishes by identity, so mutating in place
  // would not re-render the panel.
  $dagOpenNodes.set(next)
}
