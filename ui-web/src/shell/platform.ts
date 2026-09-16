/* Browser and host facts shared by page chrome and feature islands. */

import { tag as langTag } from '../state/lang'

export const isMac = (): boolean => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

export const modKey = (): string => (isMac() ? '⌘' : 'Ctrl +')

/* The language declaration, from the store that writes it rather than off the
   element. Same answer either way, including before any pick has been applied:
   the store hands back the document's own declaration until then. */
export const language = (): 'zh' | 'en' =>
  langTag().startsWith('zh') ? 'zh' : 'en'
