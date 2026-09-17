/* The page's language, and the only place that applies one.
 *
 * Two facts, not one. `applied` is which language has been applied to THIS
 * page, and it is null until something applies one -- which is the state every
 * page boots in: src/page.html declares lang="zh-CN", the regions src/App.tsx
 * renders carry the literals that markup was served with, and nothing moves
 * them until a pick arrives from the settings dialog, from localStorage or from
 * config.language. The catalogue's own column (src/i18n/t.ts's `code`) starts
 * at English regardless, which is why an untouched page shows Chinese text
 * beside English text drawn from JavaScript. Reproduced here, not fixed: this
 * is the state the reader sees today, and moving the language out of the legacy
 * kernel may not change a pixel of it.
 *
 * `text(key, literal)` and `attr(key)` are what that buys the components: the
 * served literal while nothing has been applied, the catalogue afterwards --
 * and, for an attribute the served markup did not carry at all, nothing at all
 * until a pick lands. Between them they are applyI18n's five passes: a
 * component renders the value the pass would have written over it.
 *
 * Which is why there is no pass any more. It walked the document on every pick
 * and rewrote data-i18n / -ph / -title / -aria / -tip wherever it found them,
 * because the markup was static and nothing else could reach it; the markup is
 * a React tree now and every one of those keys is rendered beside the value it
 * decides. The keys stay on the elements -- the region goldens record them, and
 * they say which phrase a line of chrome speaks -- but nothing reads them any
 * more.
 *
 * This module is the only writer of <html lang>, and it never writes it on
 * load: a page nobody has picked a language for keeps the declaration it was
 * served with.
 */

import { type Lang, setCode, T } from '../../i18n/t'
import { makeStore } from '../store'

export type { Lang }

const store = makeStore<Lang | null>(null)
const afterwards = new Set<() => void>()

/** Which language has been applied to the page, or null while none has. */
export const { get, subscribe, _resetForTests } = store

/* What <html lang> says: what `set` wrote, or -- while nothing has been
   applied -- the declaration the document was served with. The three modules
   that used to read the attribute read this instead, so each keeps the answer
   it gets today in both load modes: the served page declares zh-CN, and a test
   document that declares nothing still answers nothing. */
export function tag(): string {
  return get() === null ? document.documentElement.lang : get() === 'zh' ? 'zh-CN' : 'en'
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

/** A key's text, or the literal the markup carries while no pick has landed. */
export function text(key: string, literal: string): string {
  return get() === null ? literal : T(key)
}

/* A key's text for an attribute the served markup does not carry: absent until
   a pick lands, which is exactly what the page shows today -- applyI18n was the
   only writer of these, and it ran only on a pick. `undefined` is what tells
   React to leave the attribute off. */
export function attr(key: string): string | undefined {
  return get() === null ? undefined : T(key)
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
  document.documentElement.lang = v === 'zh' ? 'zh-CN' : 'en'
  store.set(v)
}

/** A pick: apply it, then tell everything that draws itself to draw again. */
export function set(v: Lang): void {
  apply(v)
  for (const fn of [...afterwards]) fn()
}

/* The same move without the whole-page redraw, for the one caller that has
   never wanted it: `restore` applies the remembered language ahead of the
   socket, before anything drawn from JavaScript exists, and is the one language
   call site that does not repaint any of it (see state/lang/pick.ts). The markup
   still moves -- that half was applyI18n, which every call site ran -- so the
   regions are committed here too; what is skipped is state/lang/effects.ts. */
export function setQuiet(v: Lang): void {
  apply(v)
}
