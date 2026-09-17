/* The message catalogue and the text the page renders from it.
 *
 * One file feeds both front ends: i18n/messages.json is what the TUI generates
 * its own copy from, so one edit moves both. The lookup and its fallback chain
 * come from the legacy kernel unchanged -- the entry in the current language,
 * then its English, then the caller's fallback, then the key itself -- so a key
 * the catalogue never grew still renders as something rather than as nothing.
 *
 * The language is module state rather than a parameter because every caller has
 * always meant "the current one". `code` is which column of the catalogue to
 * read, and src/state/lang/store.ts is its only writer: that module resolves the
 * page's language before the first frame and moves this column with it.
 *
 * `t` is the whole of what the page calls. The lookup itself is module-private,
 * because `t` is the same lookup behind one indirection and that indirection is
 * the seam a case replaces (`setTranslator`): a caller that reached for the raw
 * lookup was a caller a case could not stand in for, and two names for one
 * question is one too many.
 */

import catalog from '../../../i18n/messages.json'

export type Lang = 'en' | 'zh'

/* Shapes, not the keys: every lookup below is by a key computed at run time,
   so what the callers need from the catalogue is that a missing entry reads as
   undefined rather than as a type error. */
interface SlashEntry {
  name?: string
  help?: string
}
type Entries<T> = Record<string, Record<string, T | undefined> | undefined>

const I18N = {
  slash: catalog.slash as Entries<SlashEntry>,
  ui: catalog.ui as Entries<string>,
}

let code: Lang = 'en'

/* Moves the column T reads. src/state/lang/store.ts calls this as one step of
   applying a language, which is also what writes <html lang> and repaints the
   static markup; nothing else may call it, or the page would answer in one
   language and be marked up in the other. */
export function setCode(v: Lang): void {
  code = v
}

/* `unknown` values, not `string | number`: every one is stringified, and the
   callers reach for this with what they have -- a caught error, a count that
   may be absent -- which is what the page has always put in the sentence. */
const fillVars = (s: unknown, vars?: Record<string, unknown> | null): string =>
  vars ? String(s).replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m)) : String(s)

const lookup = (key: string, vars?: Record<string, unknown> | null, fallback?: string | null): string => {
  const e = I18N.ui[key] || {}
  return fillVars(e[code] != null ? e[code] : (e.en != null ? e.en : (fallback != null ? fallback : key)), vars)
}

/** What `t` asks. `fallback` is what to show when the catalogue has no entry. */
export type Translator = (
  key: string,
  vars?: Record<string, unknown> | null,
  fallback?: string | null,
) => string

let translate: Translator = lookup

/* For a test: a translator that answers the key, so a case can assert on the
   key rather than on a sentence the catalogue owns. The page never calls this. */
export function setTranslator(fn: Translator): void {
  translate = fn
}

/** Back to the catalogue, so a translator one case handed in does not outlive it. */
export function resetTranslator(): void {
  translate = lookup
}

/** An island's words. The catalogue, unless a case has replaced the lookup. */
export const t: Translator = (key, vars, fallback) => translate(key, vars, fallback)

const slashText = (id: string): SlashEntry => {
  const e = I18N.slash[id] || {}
  return (code !== 'en' && e[code]) || e.en || {}
}
const slashName = (id: string): string => slashText(id).name || id
const slashHelp = (id: string): string => slashText(id).help || ''

export { I18N, code, fillVars, slashText, slashName, slashHelp }
