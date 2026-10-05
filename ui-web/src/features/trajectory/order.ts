/* The list's order, and the two edits that keep it.
 *
 * Entries arrive sorted from `trajectory.list`, and every change feed batch
 * then has to land each upsert where the index would have put it: the index
 * sorts by `sort_key`, a tuple the wire carries on every entry, so the page
 * compares the same tuple rather than guessing the rule from the fields.
 * Pure functions, so the order is testable without a store or a document.
 */

import type { JsonValue, TrajectoryEntry } from './types'

/* The tuple's elements are timestamps, small integers and ids; null stands for
   "no parent" and sorts first. Anything else the wire might grow is ranked
   after those three kinds rather than thrown on. */
const rank = (v: JsonValue): number => {
  if (v === null) return 0
  if (typeof v === 'number') return 1
  if (typeof v === 'string') return 2
  return 3
}

function compareElement(a: JsonValue, b: JsonValue): number {
  const ra = rank(a)
  const rb = rank(b)
  if (ra !== rb) return ra - rb
  if (typeof a === 'number' && typeof b === 'number') return a - b
  if (typeof a === 'string' && typeof b === 'string') return a < b ? -1 : a > b ? 1 : 0
  return 0
}

/** The index's own order: element by element, a shorter tuple first. */
export function compareSortKey(a: readonly JsonValue[], b: readonly JsonValue[]): number {
  const n = Math.min(a.length, b.length)
  for (let i = 0; i < n; i += 1) {
    const c = compareElement(a[i] as JsonValue, b[i] as JsonValue)
    if (c !== 0) return c
  }
  return a.length - b.length
}

/* Where a key would go: the first position whose entry sorts after it. */
function insertionPoint(entries: readonly TrajectoryEntry[], key: readonly JsonValue[]): number {
  let lo = 0
  let hi = entries.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (compareSortKey(entries[mid]!.sort_key, key) <= 0) lo = mid + 1
    else hi = mid
  }
  return lo
}

/* A new array with the entry in place: replacing the one that carries its id
   when there is one (an update may move it, so the old position is dropped
   first), or inserted by its key. The input is never mutated: the store hands
   React the array it holds. */
export function insertSorted(entries: readonly TrajectoryEntry[], entry: TrajectoryEntry): TrajectoryEntry[] {
  const without = entries.filter((e) => e.entry_id !== entry.entry_id)
  const at = insertionPoint(without, entry.sort_key)
  return [...without.slice(0, at), entry, ...without.slice(at)]
}

/** A new array without the ids named, in the order the rest already had. */
export function removeIds(entries: readonly TrajectoryEntry[], ids: Iterable<string>): TrajectoryEntry[] {
  const gone = new Set(ids)
  if (!gone.size) return [...entries]
  return entries.filter((e) => !gone.has(e.entry_id))
}

/** `entry_id` to position, rebuilt whenever the array is replaced. */
export function indexOf(entries: readonly TrajectoryEntry[]): Record<string, number> {
  const out: Record<string, number> = {}
  entries.forEach((e, i) => { out[e.entry_id] = i })
  return out
}
