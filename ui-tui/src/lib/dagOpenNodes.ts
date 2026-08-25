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
 * The key a click on this node opens.
 *
 * Every node opens: the slot under it holds the node's trace, and the detail
 * block names it even when neither a trace nor a prompt reached the client --
 * and with the graph carrying the run there is no row printing its id any more.
 * It used to return `null` for a template-less node -- correct while its row was
 * already showing the id, and a dead end once that row was gone.
 *
 * The one rule, so a node's row and its box in the picture cannot disagree about
 * what is expandable.
 *
 * Signature keeps the `null` a caller has to handle so `dagSpanToggleKey` can
 * still refuse a wire, which belongs to no node at all.
 */
export const dagNodeToggleKey = (runId: string, node: DagRunNode): string | null => dagNodeKey(runId, node.id)

/** The same rule reached from a picture span. `null` for a wire, which belongs
 * to no node at all. */
export const dagSpanToggleKey = (runId: string, span: DagPictureSpan, nodes: readonly DagRunNode[]): string | null => {
  const node = span.nodeId ? nodes.find(item => item.id === span.nodeId) : undefined

  return node ? dagNodeToggleKey(runId, node) : null
}

/**
 * Open one node, closing whatever else was open in the same run.
 *
 * One run shows one node's detail: with the graph carrying the topology, the
 * detail is the panel's only long block, and two of them open at once pushed
 * the graph they belong to off the top of the screen. The scope is the run, not
 * the store -- two graphs in one transcript are separate panels, and opening a
 * node in the second must not collapse what the reader left open in the first.
 */
export const toggleDagNode = (key: string) => {
  const runScope = `${key.slice(0, key.lastIndexOf('/'))}/`
  const wasOpen = $dagOpenNodes.get().has(key)
  const next = new Set([...$dagOpenNodes.get()].filter(open => !open.startsWith(runScope)))

  if (!wasOpen) {
    next.add(key)
  }

  // A new set every time: the store publishes by identity, so mutating in place
  // would not re-render the panel.
  $dagOpenNodes.set(next)
}
