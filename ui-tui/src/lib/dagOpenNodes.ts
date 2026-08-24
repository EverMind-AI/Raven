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

import type { DagRunNode } from '../domain/dagRun.js'
import type { DagPictureSpan } from './dagGraphRender.js'

export const $dagOpenNodes = atom<ReadonlySet<string>>(new Set())

/** Node ids are unique only within a run, so a bare id would expand the
 * same-named node of every graph in the transcript. */
export const dagNodeKey = (runId: string, nodeId: string) => `${runId}/${nodeId}`

/**
 * The key a click on this node opens, or `null` when it opens nothing.
 *
 * The one rule, so a node's row and its box in the picture cannot disagree about
 * what is expandable. `null` when no prompt reached the client: there is nothing
 * to reveal, and an affordance that swallows a click is worse than none.
 */
export const dagNodeToggleKey = (runId: string, node: DagRunNode): string | null =>
  node.promptTemplate ? dagNodeKey(runId, node.id) : null

/** The same rule reached from a picture span. `null` for a wire, which belongs
 * to no node at all. */
export const dagSpanToggleKey = (
  runId: string,
  span: DagPictureSpan,
  nodes: readonly DagRunNode[]
): string | null => {
  const node = span.nodeId ? nodes.find(item => item.id === span.nodeId) : undefined

  return node ? dagNodeToggleKey(runId, node) : null
}

export const toggleDagNode = (key: string) => {
  const next = new Set($dagOpenNodes.get())

  if (!next.delete(key)) {
    next.add(key)
  }

  // A new set every time: the store publishes by identity, so mutating in place
  // would not re-render the panel.
  $dagOpenNodes.set(next)
}
