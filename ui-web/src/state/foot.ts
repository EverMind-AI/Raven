/* The rail's foot row: the two text slots inside the door to settings.
 *
 * One door to settings. The sub-line carries the running build, which is the
 * single fact the old identity row was actually showing anyone, and the hint
 * on the right spells the platform's own shortcut for that door.
 *
 * Two facts and no DOM. They were two writes by id -- #meSub's and #meKbd's
 * textContent -- back when the row was someone else's markup; the column is
 * rendered by src/chrome/Rail.tsx now, so <RailFoot/> reads them from here and
 * the label beside them comes from the lang store (src/state/lang/store.ts)
 * like every other word in the column.
 */

import { isMac, modKey } from '../lib/platform'
import { ds } from './sources'
import { makeStore } from './store'

import type { SettingsSource } from '../features/settings/types'

export interface FootState {
  /** The running build, or a dash while the install has not answered. */
  readonly sub: string
  /** This platform's spelling of the shortcut for the door. */
  readonly kbd: string
}

/* Served empty: page.html handed both spans over with nothing in them, and
   drawFoot() is a boot step rather than a first-paint value. */
const store = makeStore<FootState>({ sub: '', kbd: '' })

/** The two slots, for <RailFoot/>. */
export const { get, subscribe, _resetForTests } = store

/* The same two strings are not a write: this is a boot step and a step of the
   language repaint, and both read the page straight afterwards. */
export function set(next: FootState): void {
  const now = get()
  if (next.sub === now.sub && next.kbd === now.kbd) return
  store.set(next)
}

export function draw(): void {
  const version = ds<SettingsSource>('settings').version()
  /* Mac spells the modifier as a glyph that reads as one key with the comma;
     every other platform needs the gap. */
  const mod = modKey()
  const mac = isMac()
  set({ sub: `Raven ${version ? 'v' + version : '--'}`, kbd: `${mod}${mac ? '' : ' '},` })
}
