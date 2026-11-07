/* One rule for placing an incoming report against the attempt a node holds.
 *
 * The same history decides staleness on every surface: a frame (live.ts), a
 * manifest row (live.ts), a `dag.get` snapshot (nodes.ts) and a list read
 * (nodes.ts). Keeping the comparison here means a change to "older" or "newer"
 * moves every reader together instead of five copies drifting apart.
 */

export type AttemptOrder = 'stale' | 'same' | 'newer'

/* A node's attempt history, as a frame or snapshot row carries it. Absent and
   malformed are the same answer: a half-valid list would silently drop or
   misplace an attempt, so the node keeps whatever history it already had. */
export const attemptsOf = (v: unknown): string[] | undefined =>
  Array.isArray(v) && v.every((a) => typeof a === 'string' && a) ? [...v] : undefined

/* Where an incoming report sits relative to the attempt the node holds.
   A report that cannot claim a longer history than the node holds is from the
   attempt it has moved past: the frames of the agent that gave up can still be
   in flight when the next one starts. A longer history is a later attempt, and
   it supersedes what the node says however it ended. */
export const attemptOrder = (
  held: string[] | undefined,
  history: string[] | undefined,
): AttemptOrder => {
  if (held !== undefined && (history === undefined || history.length < held.length)) return 'stale'
  if (history !== undefined && (held === undefined || history.length > held.length)) return 'newer'
  return 'same'
}

/* Presence, not value: a present null clears the handle a stateless retry left
   behind, while an absent key says this frame does not speak for it. */
export const instanceOf = (source: object): { instance?: string | null } => {
  if (!Object.hasOwn(source, 'instance')) return {}
  const value = (source as { instance?: unknown }).instance
  return { instance: typeof value === 'string' && value ? value : null }
}
