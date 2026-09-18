/* The page's language, resolved before the first frame, and the only place that
 * applies one.
 *
 * Two facts in one value, because both decide what a region renders and both
 * have to move it when they change.
 *
 * `lang` is the language the page is in, and it is never null. It is resolved as
 * this module loads: the pick the reader is remembered by if there is one
 * (localStorage, under the key state/lang/pick.ts owns), and otherwise what
 * <html lang> declares -- which for the served page is zh-CN. The gateway's own
 * answer arrives a moment later and wins, the way any pick does
 * (`config.language` through state/lang/pick.ts's `load`). So a region renders
 * one language from its first paint, rather than the served Chinese markup with
 * English words drawn over it by whatever ran from JavaScript.
 *
 * `picked` is whether a language was chosen rather than inherited from the
 * document, and it decides exactly one thing: an attribute the served markup
 * does not carry at all. applyI18n's five passes wrote those, and it ran only on
 * a pick, so a page nobody has picked for carries no aria-label, title or
 * data-tip it was not served with -- which is what both boot goldens record.
 * `attr(key)` is that rule and is the whole reason the second fact is here; a
 * text node has no such rule, because the markup carried a literal for every
 * one of them.
 *
 * Which is why there is no pass any more. It walked the document on every pick
 * and rewrote data-i18n / -ph / -title / -aria / -tip wherever it found them,
 * because the markup was static and nothing else could reach it; the markup is
 * a React tree now and every one of those keys is rendered beside the value it
 * decides, so the markers it looked for went with the pass that read them.
 *
 * This module is the only writer of <html lang>, and it writes it only for a
 * language the document does not already declare.
 */

import { type Lang, setCode, t } from '../../i18n/t'
import { makeStore } from '../store'

export type { Lang }

/** The key a pick is remembered under (state/lang/pick.ts persists it). */
const REMEMBERED = 'raven.gui.lang'

/** What the document was served declaring, as one of the two languages. */
const declared = (): Lang => (/^zh/i.test(document.documentElement.lang) ? 'zh' : 'en')

/* Read through a guard, because private mode throws on access rather than
   answering null -- the same guard the persist side uses. */
function remembered(): Lang | null {
  try {
    const kept = localStorage.getItem(REMEMBERED)
    return kept === 'en' || kept === 'zh' ? kept : null
  } catch {
    return null
  }
}

const tagOf = (v: Lang): string => (v === 'zh' ? 'zh-CN' : 'en')

/** The language the page is in, and whether a reader picked it. */
export interface LangState {
  readonly lang: Lang
  readonly picked: boolean
}

const kept = remembered()
const store = makeStore<LangState>({ lang: kept ?? declared(), picked: kept !== null })
const afterwards = new Set<() => void>()

/** The two facts. Resolved at load, so the language is never null. */
export const { get, subscribe } = store

/* Applied as this module loads, before anything reads a word: the catalogue's
   column is the language, and the declaration follows a remembered pick. */
setCode(get().lang)
if (get().picked) document.documentElement.lang = tagOf(get().lang)

/** What <html lang> says, which is the language this module resolved. */
export function tag(): string {
  return tagOf(get().lang)
}

/* The other kind of subscriber: not a component, and it has to run after the
   markup has moved. applyI18n rewrote the document and only then did the
   whole-page redraw run over what is drawn from JavaScript (state/lang/effects.ts
   is that redraw); the markup is rendered now, so the commit below is forced
   before this group is called. One subscriber, which is that redraw. */
export function onApplied(fn: () => void): () => void {
  afterwards.add(fn)
  return () => {
    afterwards.delete(fn)
  }
}

/* A key's text for an attribute the served markup does not carry: absent until
   a pick lands, which is exactly what the page shows today -- applyI18n was the
   only writer of these, and it ran only on a pick. `undefined` is what tells
   React to leave the attribute off. */
export function attr(key: string): string | undefined {
  return get().picked ? t(key) : undefined
}

/* The regions are committed with the write, synchronously, because applyI18n
   was synchronous: a pick rewrote the markup before it returned, and every
   caller -- the rollback in state/lang/pick.ts most of all -- reads the page
   straight afterwards. A plain notification would leave the commit to React's
   scheduler and a later task, which is a frame in the old language. The
   translator and <html lang> are set BEFORE the write, because the regions the
   write commits read both while they render. */
function apply(v: Lang): void {
  setCode(v)
  document.documentElement.lang = tagOf(v)
  /* A new value even for the language already in force: `picked` moved, and a
     pick the reader made has to reach the attributes that wait for one. */
  store.set({ lang: v, picked: true })
}

/** A pick: apply it, then tell everything that draws itself to draw again. */
export function set(v: Lang): void {
  apply(v)
  for (const fn of [...afterwards]) fn()
}

/* Test seam: back to the language this module resolved, with no pick applied. */
export function _resetForTests(): void {
  store._resetForTests()
  setCode(get().lang)
}
