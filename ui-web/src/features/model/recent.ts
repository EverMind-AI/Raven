/* The models this browser picked last, for the head of the composer's picker.
 *
 * A list of a few hundred models needs a short way back to the three or four a
 * reader actually moves between, and nothing on the wire says which those are:
 * the conversation rows carry a folder but not a model. So the picker keeps
 * its own memory, in localStorage, of the last picks made from the composer --
 * the model and the provider it was chosen under, because the same id can be
 * served by two accounts and a pick names one.
 *
 * Only the composer's picks are remembered: a settings slot edits a default or
 * a role, which is not something the reader "used".
 */

const KEY = 'raven.models.recent'

/** How many the picker's head offers. */
export const RECENT_MAX = 3

export interface Recent {
  readonly model: string
  readonly provider: string
}

/* Private mode throws on read, and an older build may have written another
   shape; either is a reason for an empty list rather than a throw. */
export function recent(): Recent[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '[]') as unknown
    if (!Array.isArray(raw)) return []
    return raw
      .filter((r): r is Recent => !!r && typeof (r as Recent).model === 'string' && typeof (r as Recent).provider === 'string')
      .slice(0, RECENT_MAX)
  } catch {
    return []
  }
}

/** A pick made: it goes to the head, once, and the list stays short. */
export function remember(model: string, provider: string): void {
  const next = [{ model, provider }, ...recent().filter((r) => r.model !== model || r.provider !== provider)]
    .slice(0, RECENT_MAX)
  try {
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    /* nothing to do about it */
  }
}
