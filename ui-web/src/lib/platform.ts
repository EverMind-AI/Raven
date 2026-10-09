/* Browser and host facts shared by page chrome and feature islands. */

import { tag as langTag } from '../state/lang'

export const isMac = (): boolean => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

/* The GATEWAY host's OS family -- host-side actions (reveal in Finder) happen
   there, not in this browser. The UA is only the prior for the usual localhost
   case; `system.hello` corrects it through the setter, which is why this is a
   field with one writer rather than a function of the UA. */
let host = /Mac/.test(navigator.platform) ? 'mac'
  : /Win/.test(navigator.platform) ? 'windows' : 'linux'

export const hostPlatform = (): string => host

export function hostPlatformSet(v: string): void {
  host = v
}

/* Whether the gateway is this desktop. Host-side actions -- reveal, open with
   an app -- run where the gateway runs, so on a remote serve they would drive
   somebody else's machine. */
export const hostIsLocal = (): boolean => /^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname)

export const modKey = (): string => (isMac() ? '⌘' : 'Ctrl +')

/* The chord a sheet that blocks the turn is answered with, the way agent
   products spell "go ahead": Cmd+Enter, and with Shift for the broader grant.
   A modifier rather than a bare key, because a bare Enter or a digit is what a
   reader types into the composer beneath the sheet. Either modifier is read on
   every platform, as the page's other chords are. */
export function sendChord(e: KeyboardEvent): 'plain' | 'shift' | null {
  if (e.key !== 'Enter' || e.altKey || !(e.metaKey || e.ctrlKey)) return null
  return e.shiftKey ? 'shift' : 'plain'
}

/* The same chords as a key cap reads them. */
export const chordLabel = (shift = false): string => (isMac()
  ? (shift ? '\u21e7\u2318\u21b5' : '\u2318\u21b5')
  : (shift ? 'Ctrl+Shift+Enter' : 'Ctrl+Enter'))

export const ESC_LABEL = 'Esc'

/* The language declaration, from the store that writes it rather than off the
   element. Same answer either way, including before any pick has been applied:
   the store hands back the document's own declaration until then. */
export const language = (): 'zh' | 'en' =>
  langTag().startsWith('zh') ? 'zh' : 'en'
