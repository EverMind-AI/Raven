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

import { slot } from '../../shell/persist'

import type { DagRun } from './types'

const RUNS = new Map<string, DagRun>()
let epoch = 0
const listeners = new Set<() => void>()

/* What a reload needs to put a sheet back: which run it was watching, and
   whether the reader had folded it. The graph is not in here on purpose -- it is
   read back from `dag.get`, the only source that can say what the nodes are
   doing now (see shell/persist.ts). */
interface Kept {
  run: string
  folded: boolean
}

const KEPT = slot<Kept>('dag', 1)

/* The sheet this conversation had open before the page was replaced, if it had
   one. Answered from storage, so it is available before any run is. */
export const saved = (key: string): Kept | null => KEPT.read(key)

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
  /* Written from here rather than from the three callers, because in-place
     mutation is how this store works: `dag.run_completed` folds the run by
     writing `d.folded` and calling this, so a fold recorded only in `fold()`
     would miss the one the run does to itself. Cheap enough to do per event --
     two ids per open sheet, and there are never many. */
  for (const [key, r] of RUNS) KEPT.write(key, { run: r.run_id, folded: !!r.folded })
  for (const l of listeners) l()
}

export function set(key: string, r: DagRun): void {
  RUNS.set(key, r)
  touch()
}

/* The reader closed the sheet, or the conversation went away. Either way there
   is nothing for the next reload to put back: a sheet that was dismissed must
   not return on refresh, which is the whole difference between this and the
   rack detaching one on a session switch. */
export function forget(key: string): void {
  RUNS.delete(key)
  KEPT.forget(key)
  touch()
}

export function fold(key: string, on: boolean): void {
  const r = RUNS.get(key)
  if (!r || r.folded === on) return
  r.folded = on
  touch()
}

/* Test seam: the map and the slot both outlive a test file's DOM. */
export function _resetForTests(): void {
  RUNS.clear()
  KEPT.clear()
  epoch = 0
  listeners.clear()
}
