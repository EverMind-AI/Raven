/* The listeners the page holds on the document itself, installed once.
 *
 * One of them so far: the keydown that reads the Escape order
 * (state/overlays.ts) and carries the three platform shortcuts that shared it
 * in the legacy chrome. They share it because they were written in one handler,
 * and splitting them would add a registration -- the order document listeners
 * are registered in is a contract of its own (plan section 9), so the rest of
 * stage C moves the remaining ones here one at a time and this file grows a
 * seed at a time rather than all at once.
 *
 * Registered from the legacy chrome's install(), at the point in that file
 * where the handler used to be added, so the sequence stays what it was.
 */

import { composing } from '../features/composer/store'
import { toggle as toggleFind } from '../shell/find'
import * as overlays from './overlays'
import { get as railOpen, set as setRail } from './rail'

/** The one bubble-phase keydown: the Escape order and the three shortcuts. */
export function installEscapeChain(): void {
  document.addEventListener('keydown', (e) => {
    /* Escape ends an open composition; it must not also close a panel or halt
       the running turn behind the reader's back. The guard is the composer
       field's own, which is where the page's other Enter handlers ask it. */
    if (composing(e)) return
    const inField = /INPUT|TEXTAREA/.test(document.activeElement?.tagName ?? '')
    if (e.key === 'Escape' && overlays.dispatch()) return
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault(); setRail(true); toggleFind(true)
    }
    if ((e.metaKey || e.ctrlKey) && e.key === '\\') {
      e.preventDefault(); setRail(!railOpen())
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n' && !inField) {
      e.preventDefault(); document.getElementById('newBtn')?.click()
    }
  })
}
