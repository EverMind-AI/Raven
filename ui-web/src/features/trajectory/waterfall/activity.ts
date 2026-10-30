/* The activity strip's figures: how many calls started in each stretch of
 * real time, over the whole session.
 *
 * A call is counted once, in the bucket its first entry starts in -- a model
 * call writes its input, its thinking and its answer as three entries of one
 * span. Buckets are real time from the session's start, so in a busy stretch
 * they fall evenly on the folded axis; a folded idle stretch holds none.
 */

import { entryStart } from './model'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis } from './axis'

export interface ActivityBucket {
  start: number
  end: number
  llm: number
  tool: number
  failed: boolean
}

const BUCKETS = [10_000, 30_000, 60_000, 120_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000]

/** The shortest bucket that still draws at least `minPx` wide at this width:
 *  projected positions are milliseconds of time something ran. */
export function bucketWidth(axis: TimeAxis, widthPx: number, minPx = 6): number {
  const perPx = axis.length / Math.max(1, widthPx)
  return BUCKETS.find((ms) => ms / perPx >= minPx) ?? BUCKETS.at(-1)!
}

export function activity(entries: readonly TrajectoryEntry[], axis: TimeAxis, bucketMs: number): ActivityBucket[] {
  const calls = new Map<string, { at: number; kind: 'llm' | 'tool' }>()
  const failures: number[] = []
  for (const entry of entries) {
    const kind = entry.kind.startsWith('llm.') ? 'llm' : entry.kind.startsWith('tool.') ? 'tool' : null
    if (kind !== null) {
      const at = entryStart(entry)
      const held = calls.get(entry.span_id)
      if (held === undefined || at < held.at) calls.set(entry.span_id, { at, kind })
    }
    if (entry.failure_entry) failures.push(entryStart(entry))
  }
  const buckets = new Map<number, ActivityBucket>()
  const bucketAt = (at: number): ActivityBucket => {
    const index = Math.floor((at - axis.start) / bucketMs)
    let bucket = buckets.get(index)
    if (bucket === undefined) {
      bucket = { start: axis.start + index * bucketMs, end: axis.start + (index + 1) * bucketMs, llm: 0, tool: 0, failed: false }
      buckets.set(index, bucket)
    }
    return bucket
  }
  for (const { at, kind } of calls.values()) bucketAt(at)[kind] += 1
  for (const at of failures) bucketAt(at).failed = true
  return [...buckets.values()].sort((a, b) => a.start - b.start)
}
