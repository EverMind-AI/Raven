/* The models this browser last sent a message with, for the head of the
 * composer's picker.
 *
 * A list of a few hundred models needs a short way back to the three or four a
 * reader actually moves between, and nothing on the wire says which those are:
 * the conversation rows carry a folder but not a model. So the picker keeps
 * its own memory, in localStorage -- the model and the provider, because the
 * same id can be served by two accounts and a turn runs on one.
 *
 * Written when a message goes out (`rememberSent`, called from the composer's
 * send in state/session/runtime.ts), under what the composer's chip names at
 * that moment, which is what the turn runs on. It used to be written on a pick
 * in the picker instead, and that listed what was reached for rather than what
 * was used: a model picked and never sent with stayed, and the default -- set
 * in settings, never picked, and the one every message went out on -- never
 * appeared at all. A settings slot is not a use either way.
 */

const KEY = 'raven.models.used'
/* Where the picks were kept. Dropped rather than carried over: it names
   models that may never have carried a message. */
const PICKED_KEY = 'raven.models.recent'

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

/** A model used: it goes to the head, once, and the list stays short. */
export function remember(model: string, provider: string): void {
  const next = [{ model, provider }, ...recent().filter((r) => r.model !== model || r.provider !== provider)]
    .slice(0, RECENT_MAX)
  try {
    localStorage.setItem(KEY, JSON.stringify(next))
    localStorage.removeItem(PICKED_KEY)
  } catch {
    /* nothing to do about it */
  }
}
