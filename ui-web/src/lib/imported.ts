/* Whether the conversation on screen is an Imported Conversation.
 *
 * One is addressed as `imported:<id>` (the runtime defines it in the repo's
 * CONTEXT.md), and the page needs the answer before `session.resume` has said
 * `info.read_only`: the column is laid out read-only the moment a switch
 * starts. So the key's own prefix answers, for any layer that asks.
 */

import { current } from './session'

export const IMPORTED_PREFIX = 'imported:'

export function isImportedKey(key: string | null | undefined): boolean {
  return typeof key === 'string' && key.startsWith(IMPORTED_PREFIX)
}

/** The conversation open now is an imported one, which nothing on the page may change. */
export const readingImported = (): boolean => isImportedKey(current())
