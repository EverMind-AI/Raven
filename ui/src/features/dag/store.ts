/* The graphs the page is watching, one per conversation.
 *
 * A `run_subagent_dag` call is the whole picture of a turn's work, so it gets a
 * sheet above the composer rather than one clamped line in the scrollback. The
 * live layer feeds the three `dag.*` events in through mount.tsx; what the sheet
 * draws is decided here and in DagSheet.tsx.
 *
 * The runs are shared objects, not copies: the live layer mutates a node's
 * status and times in place and then calls `touch()`, the same arrangement the
 * settings island has with PROVIDERS. That keeps the event handlers reading as
 * they did, and keeps one answer to "what is this node doing" rather than two
 * that have to be held in step.
 */

import type { DagRun } from './types'

const RUNS = new Map<string, DagRun>()
let epoch = 0
const listeners = new Set<() => void>()

export const version = (): number => epoch
export const run = (key: string): DagRun | null => RUNS.get(key) || null
export const keys = (): string[] => [...RUNS.keys()]

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

/* Every change to a run goes through here, because the runs themselves are
   mutated in place: identity cannot tell the component that anything moved. */
export function touch(): void {
  epoch += 1
  for (const l of listeners) l()
}

export function set(key: string, r: DagRun): void {
  RUNS.set(key, r)
  touch()
}

export function forget(key: string): void {
  RUNS.delete(key)
  touch()
}

export function fold(key: string, on: boolean): void {
  const r = RUNS.get(key)
  if (!r || r.folded === on) return
  r.folded = on
  touch()
}

/* Test seam: the map outlives a test file's DOM. */
export function _resetForTests(): void {
  RUNS.clear()
  epoch = 0
  listeners.clear()
}
