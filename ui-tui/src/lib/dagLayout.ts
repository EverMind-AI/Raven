// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Assign a DAG's nodes to dependency levels.
//
// A level is longest-path depth: a node sits one below *every* dependency, not
// just its first. `dagGraphLayout` consumes these as the columns of the drawn
// graph, which is why the "below every dependency" part is load-bearing -- a
// node placed one past its shallowest dependency would need an edge pointing
// leftwards, and the picture only routes rightwards.

import type { DagRunNode } from '../domain/dagRun.js'

/** Nodes that may run once the levels above them have completed. */
export type DagLevel = DagRunNode[]

/**
 * Group `nodes` into levels, each node one below its deepest dependency.
 *
 * Submitted order is preserved within a level: the graph is re-laid out on every
 * progress event, so ordering by anything that changes as the run advances would
 * make rows jump under the reader.
 *
 * A dependency not present in `nodes` counts as satisfied — it cannot be waited
 * on, and treating it as unmet would drop the node from the drawing. That is a
 * declared shape, not just a malformed-frame guard: `depends_on` may name a node
 * an earlier run of the session completed, which is never in this run. A cyclic
 * graph (which the backend rejects before running, so only a malformed frame
 * gets here) has its unresolvable remainder appended as one final level rather
 * than being dropped or spun on.
 */
export const layoutDag = (nodes: DagRunNode[]): DagLevel[] => {
  const present = new Set(nodes.map(node => node.id))
  const placed = new Set<string>()
  const levels: DagLevel[] = []
  let remaining = nodes

  while (remaining.length > 0) {
    // Every dependency must already sit on an *earlier* level, which is what
    // puts a join below its deepest dependency rather than its first.
    const ready = remaining.filter(node => node.dependsOn.every(dep => !present.has(dep) || placed.has(dep)))
    // Nothing placeable means a cycle; emit what is left as one level.
    const batch = ready.length > 0 ? ready : remaining

    batch.forEach(node => placed.add(node.id))
    levels.push(batch)
    remaining = remaining.filter(node => !placed.has(node.id))
  }

  return levels
}
