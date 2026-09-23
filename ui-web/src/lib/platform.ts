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

/* The language declaration, from the store that writes it rather than off the
   element. Same answer either way, including before any pick has been applied:
   the store hands back the document's own declaration until then. */
export const language = (): 'zh' | 'en' =>
  langTag().startsWith('zh') ? 'zh' : 'en'
