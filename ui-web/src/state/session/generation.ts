/* Which switch the visible page belongs to.
 *
 * `session.resume` is a round trip, and a reader who clicks a second session
 * -- or the new-task button -- while it is in flight leaves the answer with
 * nowhere to land: the stage it was cleared for now holds somebody else's
 * conversation. Every switch takes the next ticket, and an answer whose ticket
 * has been spent is dropped rather than painted, which is lossless because a
 * re-open reads the same transcript back off disk.
 *
 * A monotonic counter is the whole of it today, which is what the live layer's
 * `viewGen` was. It has a module of its own so the readers -- the provider
 * refresh, the permission-mode refresh and the default-model write-back, none
 * of which are in the file that spends the tickets -- name the thing rather
 * than an import from the page's override layer.
 */

let current = 0

/** The ticket a refresh started under. */
export function generation(): number {
  return current
}

/** Spend a ticket. Every view switch does, and only a view switch does. */
export function bumpGeneration(): number {
  current += 1
  return current
}

/** Back to a fresh page's counter. For tests. */
export function resetGeneration(): void {
  current = 0
}
