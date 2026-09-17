/* Which switch the visible page belongs to.
 *
 * `session.resume` is a round trip, and a reader who clicks a second session
 * -- or the new-task button -- while it is in flight leaves the answer with
 * nowhere to land: the stage it was cleared for now holds somebody else's
 * conversation. Every switch takes the next ticket, and an answer whose ticket
 * has been spent is dropped rather than painted, which is lossless because a
 * re-open reads the same transcript back off disk.
 *
 * The ticket is the registry's own switch count -- only a switch can spend one,
 * and the registry is where a switch happens. This module is the reader's name
 * for it, so the provider refresh, the permission-mode refresh and the
 * default-model write-back name the thing rather than importing the switch.
 */

import { switchToken } from './registry'

/** The ticket a refresh started under. */
export const generation = (): number => switchToken()
