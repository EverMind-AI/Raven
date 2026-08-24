/* The rail's foot row.
 *
 * One door to settings. The sub-line carries the running build, which is the
 * single fact the old identity row was actually showing anyone, and the hint
 * on the right spells the platform's own shortcut for that door.
 *
 * Two text slots inside a button the shell owns the click of, so this stays a
 * writer rather than a component: a React root cannot hold two spans out of
 * someone else's markup, and the label beside them is translated in place by
 * applyI18n over the static attributes.
 */

import { ds } from './bridge'
import { isMac, modKey } from './platform'

import type { SettingsSource } from '../features/settings/types'

export function draw(): void {
  const version = ds<SettingsSource>('settings').version()
  const sub = document.getElementById('meSub')
  if (sub) sub.textContent = `Raven ${version ? 'v' + version : '--'}`
  const kbd = document.getElementById('meKbd')
  /* Mac spells the modifier as a glyph that reads as one key with the comma;
     every other platform needs the gap. */
  if (kbd) {
    const mod = modKey()
    const mac = isMac()
    kbd.textContent = `${mod}${mac ? '' : ' '},`
  }
}
