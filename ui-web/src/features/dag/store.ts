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
     mutation is how this store works: a fold recorded only in `fold()` would
     miss one written straight onto the run and announced through here. Nothing
     does that today -- `dag.run_completed` used to fold the run itself and no
     longer touches the flag -- but the persistence still belongs on the
     announcement rather than on one of the ways to reach it. Cheap enough to do
     per event: two ids per open sheet, and there are never many. */
  for (const [key, r] of RUNS) KEPT.write(key, { run: r.run_id, folded: !!r.folded })
  for (const l of listeners) l()
}

/* A fold taken on the reader's behalf, and given back.
 *
 * Something docked over the sheet needs the room -- a question the reader has to
 * answer, and cannot answer if a tall graph pushes it below the fold. The graph
 * steps aside for as long as that stands, and steps back afterwards.
 *
 * "Afterwards" has to mean "if it is still ours". A reader who unfolds and then
 * folds again while the question stands has taken the decision back, and their
 * fold reads exactly like the one we took: `folded` is true either way, and
 * restoring on that alone threw their choice away. So the taking is recorded,
 * and `fold` -- the reader's only path -- drops the record.
 *
 * And "still ours" has to mean "about this run". Every path that ends a run
 * drops the record too, or a claim outlives what it described and the next
 * graph under the same key is never stepped aside. */
const TAKEN = new Set<string>()

export function set(key: string, r: DagRun): void {
  /* The claim below is about one run, not about the key it sits under. A run
     replaced while a question still stands leaves the old claim describing
     something that is gone, and `takeFold` then refuses to step the new graph
     aside because the key reads as already taken. */
  TAKEN.delete(key)
  RUNS.set(key, r)
  touch()
}

/* The reader closed the sheet, or the conversation went away. Either way there
   is nothing for the next reload to put back: a sheet that was dismissed must
   not return on refresh, which is the whole difference between this and the
   rack detaching one on a session switch. */
export function forget(key: string): void {
  /* Same reason as `set`: the run this claim was about is gone. */
  TAKEN.delete(key)
  RUNS.delete(key)
  KEPT.forget(key)
  touch()
}

export function fold(key: string, on: boolean): void {
  const r = RUNS.get(key)
  if (!r || r.folded === on) return
  /* The reader has just decided the fold, so it is theirs again -- whatever was
     folded on their behalf is no longer ours to put back. Dropped here rather
     than by the caller that took it, because this is the one line every reader
     path goes through and the taker cannot see them. */
  TAKEN.delete(key)
  r.folded = on
  touch()
}

export function takeFold(key: string): void {
  const r = RUNS.get(key)
  if (!r || r.folded || TAKEN.has(key)) return
  TAKEN.add(key)
  r.folded = true
  touch()
}

export function releaseFold(key: string): void {
  if (!TAKEN.delete(key)) return
  const r = RUNS.get(key)
  if (!r || !r.folded) return
  r.folded = false
  touch()
}

/* Test seam: the map and the slot both outlive a test file's DOM. */
export function _resetForTests(): void {
  TAKEN.clear()
  RUNS.clear()
  KEPT.clear()
  epoch = 0
  listeners.clear()
}
