/* The page's language, and the only place that applies one.
 *
 * Two facts, not one. `applied` is which language has been applied to THIS
 * page, and it is null until something applies one -- which is the state every
 * page boots in: src/page.html declares lang="zh-CN" and carries Chinese
 * literals in its static markup, and nothing rewrites them until a pick arrives
 * from the settings dialog, from localStorage or from config.language. The
 * catalogue's own column (src/i18n/t.ts's `code`) starts at English regardless,
 * which is why an untouched page shows Chinese markup beside English text drawn
 * from JavaScript. Reproduced here, not fixed: this is the state the reader
 * sees today, and moving the language out of the legacy kernel may not change a
 * pixel of it.
 *
 * `text(key, literal)` is what that buys the components stage C converts: the
 * literal while nothing has been applied, the catalogue afterwards. A component
 * rendering a region page.html still provides must not translate text the page
 * has not translated, and must not put the literal back over text a flip has
 * already moved.
 *
 * This module is the only writer of <html lang> -- it was applyI18n in the
 * legacy kernel -- and it never writes it on load: a page nobody has picked a
 * language for keeps the declaration it was served with.
 */

import { type Lang, setCode, T } from '../i18n/t'

export type { Lang }

let applied: Lang | null = null
const listeners = new Set<() => void>()

/** Which language has been applied to the page, or null while none has. */
export function get(): Lang | null {
  return applied
}

/* What <html lang> says: what `set` wrote, or -- while nothing has been
   applied -- the declaration the document was served with. The three modules
   that used to read the attribute read this instead, so each keeps the answer
   it gets today in both load modes: the served page declares zh-CN, and a test
   document that declares nothing still answers nothing. */
export function tag(): string {
  return applied === null ? document.documentElement.lang : applied === 'zh' ? 'zh-CN' : 'en'
}

/** For useSyncExternalStore: called on every applied pick. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** A key's text, or the literal the markup carries while no pick has landed. */
export function text(key: string, literal: string): string {
  return applied === null ? literal : T(key)
}

/* The catalogue over the static markup, then the document's own declaration --
   applyI18n's five passes in applyI18n's order. The markup carries keys, not
   copy: data-i18n for text, -ph for a placeholder, -title / -aria for the two
   attributes that are read out, -tip for the hover pill. */
function apply(v: Lang): void {
  applied = v
  setCode(v)
  document.querySelectorAll<HTMLElement>('[data-i18n]').forEach((n) => { n.textContent = T(n.dataset.i18n!) })
  document.querySelectorAll<HTMLInputElement>('[data-i18n-ph]').forEach((n) => { n.placeholder = T(n.dataset.i18nPh!) })
  document.querySelectorAll<HTMLElement>('[data-i18n-title]').forEach((n) => { n.title = T(n.dataset.i18nTitle!) })
  document.querySelectorAll<HTMLElement>('[data-i18n-aria]').forEach((n) => {
    n.setAttribute('aria-label', T(n.dataset.i18nAria!))
  })
  document.querySelectorAll<HTMLElement>('[data-i18n-tip]').forEach((n) => {
    n.dataset.tip = T(n.dataset.i18nTip!)
  })
  document.documentElement.lang = v === 'zh' ? 'zh-CN' : 'en'
}

/** A pick: apply it, then tell everything that draws itself to draw again. */
export function set(v: Lang): void {
  apply(v)
  for (const fn of [...listeners]) fn()
}

/* The same move without the notification, for the one caller that has never
   wanted it: `langRestore` applies the remembered language ahead of the socket,
   before anything drawn from JavaScript exists, and is the one language call
   site that does not repaint any of it (see legacy/live/120-settings.js). The
   whole-page redraw subscribes here now, so a notification would run it at the
   top of the boot sequence, where today nothing runs. */
export function setQuiet(v: Lang): void {
  apply(v)
}
