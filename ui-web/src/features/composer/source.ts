/* The composer's ambient wire calls -- the ones that belong to the dock but
 * not to a conversation's composer source (`state/sources.ts` installs that
 * seam at boot). Input history is the only one: it is read once per page and
 * written on every send, neither of which is a turn action.
 *
 * It lives here rather than in `history.ts` because the page's own gate
 * (`scripts/gates/rpc-names.test.mjs`) keeps every `gateway()` call in the
 * domain's `source.ts`, so "what does this page ask of the server for the
 * composer" has one answer to open.
 */

import { gateway } from '../../rpc/gateway'

/** The newest `limit` entries, oldest first (read from the TUI's own file). */
export const historyList = (limit: number) => gateway().call('input.history.list', { limit })

/** Append one sent prompt, if it is neither blank nor the newest entry. */
export const historyAppend = (text: string): Promise<{ ok: boolean; appended: boolean }> =>
  gateway().call('input.history.append', { text })
