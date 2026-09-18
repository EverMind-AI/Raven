/* The confirm dialog: what it asks, and what the answer runs.
 *
 * One sheet serves the whole page -- delete this conversation, forget this
 * document, replace this server -- and the asking used to be four DOM writes
 * and a module-level callback: the title, the body and the yes button's label
 * were written by id, the answer was parked in a `let`, and the two buttons
 * were bound to it at install time.
 *
 * The sheet's interior is React's now (src/App.tsx), so the question is state
 * here and the buttons call back in. What stays imperative is the flag on the
 * container, because src/App.tsx renders div#veil with the flag the page is
 * served with and never writes it again: the Escape order reads that attribute
 * to decide the sheet is what an Escape should take back (state/escapeOrder.ts),
 * and the CSS shows the sheet from it.
 *
 * The question is committed synchronously, and that is a contract rather than a
 * detail. The flag goes up AFTER the text lands, the order the ask before this
 * one wrote them in, so no frame can show yesterday's question under today's
 * veil -- and the ask is called from timers and socket replies as often as from
 * a click, where React would otherwise commit in a later task. Same reason the
 * page's own root is committed with flushSync at boot (src/main.tsx).
 */

import { makeStore } from './store'

export type ConfirmState = {
  readonly open: boolean
  /** Null until something has asked, so the served literal stands. */
  readonly title: string | null
  /** The question's body; empty is what the markup ships with. */
  readonly body: string
  /** The yes button's label, null while the served literal stands. */
  readonly label: string | null
}

const store = makeStore<ConfirmState>({ open: false, title: null, body: '', label: null })

/* The answer, beside the state rather than in it: nothing renders it. Only yes
   has one -- no caller of this dialog has ever wanted a no branch. */
let onYes: (() => void) | null = null

/** What the sheet is asking; `set` is what asks and what answers. */
export const { get, set, subscribe } = store

/* One writer for the flag, so it cannot disagree with the state. */
function paint(): void {
  const el = document.getElementById('veil')
  if (el) el.dataset.open = String(get().open)
}

/* Asking, in the order confirmAsk wrote it: the question first, then the veil,
   then the focus. The cancel button takes the focus so that a stray Enter or
   Escape answers no -- Escape reaches the chain, which clicks it. */
export function ask(title: string, body: string, label: string, fn: () => void): void {
  set({ open: true, title, body, label })
  onYes = fn
  paint()
  document.getElementById('cfNo')?.focus()
}

/* Answering, in the order the two buttons did it: the veil comes down first and
 * the callback runs after, so a callback that asks the next question is not
 * closing the sheet it just raised. The question itself is left standing in the
 * markup, as the textContent writes left it: nothing reads it while the veil is
 * down, and blanking it would be a DOM write the old dialog never made.
 */
export function answer(ok: boolean): void {
  const fn = onYes
  onYes = null
  set({ ...get(), open: false })
  paint()
  if (ok) fn?.()
}

/** Test seam only: the question and its answer are the module's. */
export function _resetForTests(): void {
  store._resetForTests()
  onYes = null
}
