/* The theme, as small a store as it is: the value lives on the document
 * element, where the stylesheet reads it, and the effective one falls back to
 * the OS preference exactly as the CSS does. Nothing subscribes, so there is
 * no state to keep beside it -- reading the attribute IS reading the store.
 *
 * The *preference* is a different thing and still the look store's (CFG.theme
 * in the legacy settings layer, which is what the appearance page writes and
 * what persists to localStorage). Handing the pick back through the shell is
 * how the two stay one value, and it is also what tells the desktop shell
 * which ground colour this window now shows.
 */

import { shell } from './bridge'

export type Theme = 'dark' | 'light'

export function current(): Theme {
  const set = document.documentElement.dataset.theme
  if (set) return set === 'dark' ? 'dark' : 'light'
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/* Two steps, not three: the system option is a preference the appearance page
   offers, never a stop on the way between light and dark. */
export function toggle(): Theme {
  const next: Theme = current() === 'dark' ? 'light' : 'dark'
  document.documentElement.dataset.theme = next
  shell().themeSet?.(next)
  return next
}
