/* A usage report onto the composer's context ring.
 *
 * Three reports carry how full the window is: a finished turn's
 * message.complete, the error a turn that ended without an answer sends, and a
 * resumed session's usage. They all reach the ring through here, so the fields
 * are read one way whichever of them arrives.
 */

import { set } from '../ctxChip'

/* The two fields of the wire's usage shape the ring is drawn from. */
export interface UsageReport {
  context_used?: number
  context_max?: number
}

/* `fallbackUsed` stands in for a report that measured no window, and only a
   finished turn has one to give: its prompt count. A resumed session's zero is
   a reading -- an empty session -- so without a fallback it is drawn as zero. */
export function applyUsage(u: UsageReport | undefined, fallbackUsed?: number): void {
  if (!u) return
  set(fallbackUsed !== undefined && !u.context_used ? fallbackUsed : u.context_used, u.context_max)
}
