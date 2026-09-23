/* Waiting for a new conversation's name, and every way that wait can end.
 *
 * The server names a session: it reads the opening message and answers with
 * `session.titled`. Until that lands the row and the top bar hold a
 * placeholder, and this module is the whole of what raises the placeholder,
 * what takes it down, and what the row shows when no generated name arrives --
 * the timer, the three endings the server reports, and the one it cannot.
 *
 * The wait itself stays a `SessionRuntime` field rather than state of this
 * module's own: it belongs to one conversation and outlives a look at another,
 * which is what a runtime is for (state/session/runtime.ts). So this reads the
 * registry for it, and the sends that start a conversation call in here.
 *
 * It writes the title element directly, which the rest of state/ does not: the
 * top bar's heading is markup src/page.html carries, and the skeleton bar that
 * stands in for a name is built here (titlePlaceholder).
 */

import { draw as sessionDraw } from '../../features/rail/store'
import { plainTitle } from '../../features/rail/title'
import { I18N, t } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { ensure, get } from './registry'
import { sess } from './rows'

/* Naming a new session belongs to the server now: it reads the opening message
   and answers with `session.titled`. Until that lands the row and the top bar
   hold a placeholder, which is what the animation is for -- this used to write
   the truncated first line here and then rewrite it a second later, and no
   client-side cap is left to disagree with the server's.

   The server now says when naming ends without a title
   (`session.naming_ended`), so the ordinary quiet endings settle as fast as a
   success does. This timer is the backstop for the case no message covers -- a
   server too old to send either event, or a connection that drops mid-turn --
   and it outlives the server's own timeout on the call
   (`session_title.timeout_seconds`, 8s by default) so it cannot fire while an
   answer is still legitimately on its way. A placeholder waiting on something
   that is not coming is the one state a reader cannot leave by waiting.

   It gives up onto the opening line held here, NOT onto the stored title: the
   session's auto-name is written by `SessionManager.save`, which runs at turn
   END (agent/loop/main.py), so a turn still working at the 12s mark has no
   stored title to read and the row would sit on the default name until a
   reload -- nothing re-reads it, since `refreshList` fires only for sessions
   the reader is not looking at. The stored read is still tried first, because
   a turn that has already ended has the better text. */
export const NAMING_GRACE_MS = 12000

export function titlePlaceholder(on: boolean): void {
  const h = $('#title')
  if (!h) return
  h.textContent = ''
  h.classList.toggle('skel', !!on)
  if (!on) return
  const bar = document.createElement('span')
  bar.className = 'sk'
  bar.style.width = '180px'
  bar.style.height = '14px'
  bar.setAttribute('aria-label', t('gui.sess.naming'))
  h.appendChild(bar)
}

/* Stop waiting on `id` and show `title`, or what the row already had. Looks the
   row up rather than holding one: `refreshList` replaces the row objects, so a
   row captured when the wait started can be off the list by the time it ends. */
export function settleNaming(id: string, title?: string | null): void {
  const rt = get(id)
  if (rt && rt.naming) clearTimeout(rt.naming.timer)
  if (rt) rt.naming = null
  const s = sess(id)
  if (!s) return
  s.naming = false
  if (title) s.title = title
  if (id === sessionCurrent()) {
    titlePlaceholder(false)
    const h = $('#title')
    if (h) h.textContent = plainTitle(s.title)
  }
  sessionDraw()
}

/* The grace period ran out. Prefer the stored title -- a turn that has ended
   has an auto-name on disk and it is the one every other client shows -- and
   otherwise use the opening line captured when the wait started. A read that
   succeeds and answers null is the ordinary case here, not an error: the turn
   is still running. */
export async function namingGaveUp(id: string): Promise<void> {
  const pending = get(id)?.naming
  let title = ''
  try {
    const r = await gateway().call('session.title', { session_id: id })
    title = (r && r.title) || ''
  } catch { /* fall through to the captured line */ }
  settleNaming(id, title || (pending && pending.fallback) || '')
}

/* The server told us no name is coming for this one -- the opening line was
   too short to name after, or the session already had a name, or the feature is
   off. Settle now on the line we captured: waiting the full grace period for an
   event that will never arrive is what made a two-character "hi" the SLOWEST
   thing you could send, since a long message actually generates and lands in a
   second or two while a short one always burned the whole timeout.

   Only when a wait is actually open: `beginNaming` declines to start one for a
   session that is already named, and this must not then blank its title. */
export function namingDeclined(id: string): void {
  const pending = get(id)?.naming
  if (!pending) return
  settleNaming(id, pending.fallback || '')
}

/* A name arrived from a person while the model was still writing one, so the
   session is already better named than anything we hold. End the wait WITHOUT
   the captured opening line: settling on it here overwrote the name that had
   just been typed with the message it was typed over, for as long as the page
   stayed open.

   The stored title is read rather than assumed, because the rename may have
   been typed in another client -- this row would still be showing the default
   and clearing the placeholder alone would leave it there. If that read fails,
   keep whatever the row has; it is at worst the default, and never the wrong
   name. */
export async function namingSuperseded(id: string): Promise<void> {
  if (!get(id)?.naming) return
  let title = ''
  try {
    const r = await gateway().call('session.title', { session_id: id })
    title = (r && r.title) || ''
  } catch { /* keep what the row shows */ }
  settleNaming(id, title)
}

/* One place to decide what a `session.naming_ended` reason means for the row,
   so the dispatcher carries no policy and this is reachable from a test. Three
   of the four reasons mean "nothing better than the opening line exists"; the
   fourth means the opposite. */
export function namingEnded(id: string, reason: string): void | Promise<void> {
  if (reason === 'renamed') return namingSuperseded(id)
  return namingDeclined(id)
}

/* Every spelling of the default title, not only the one this reader is in. A
   conversation is created with the default the message catalogue gives whoever
   created it and the string is what gets stored, so the same conversation read
   in the other language carries a title that is still a default rather than a
   name. The current language stays in the list for a message catalogue with no entry at
   all, where `t` answers the key. */
const unnamed = (): string[] => [t('gui.new_task'), ...Object.values(I18N.ui['gui.new_task'] ?? {})]
  .filter((s): s is string => s != null)

export function beginNaming(text: string): void {
  const s = sess(sessionCurrent())
  if (!s || (s.title && !unnamed().includes(s.title))) return
  if (!String(text || '').trim()) return
  const id = s.id
  /* Not capped here: how a title fits a row is the front end's own business and
     both places that draw one already ellipsise. */
  const fallback = String(text).trim().split('\n')[0]!.trim()
  s.naming = true
  if (id === sessionCurrent()) titlePlaceholder(true)
  sessionDraw()
  ensure(id).naming = { fallback, timer: setTimeout(() => { namingGaveUp(id) }, NAMING_GRACE_MS) }
}
