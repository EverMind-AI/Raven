/* The conversation list, reached through the seam that answers for it.
 *
 * Four verbs over `sources.sessions`, one line each: the rail island renders
 * the rows, the page's boot owns the array behind them (state/boot.ts's
 * sessionsSource), and what is here is what everything else asks of it.
 *
 * `sess` is the fifth, and the reason this is a module rather than four call
 * sites: the pipeline, the registry, the residency rule, the runtime, the
 * language repaint and the boot all turn a session id back into its row, and
 * asking the source for the array on every one of them is what that is.
 */

import { sources } from '../sources'

import type { RailSource, SessRow } from '../../features/rail/types'

/** The installed source. Unguarded on purpose: the boot puts it on the seam
 *  before anything below can be reached. */
export const source = (): RailSource => sources.sessions as RailSource

export const rows = (): SessRow[] => source().snapshot().rows

export const replace = (next: SessRow[]): void => {
  source().replace(next)
}

export const open = (s: SessRow): void | Promise<void> => source().open(s)

/** The row for an id, or undefined while the list does not hold one. */
export const sess = (id: string | null | undefined): SessRow | undefined =>
  rows().find((s) => s.id === id)
