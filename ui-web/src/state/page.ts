/* Which module page is open, and the only place that opens one.
 *
 * Was showPageBase in legacy/demo/120-capabilities.js, whose seven writes were
 * the whole of the state: "which page is up" was readable only by asking the
 * DOM what carried data-open="true". The writes stay -- the seven <section> of
 * src/page.html are static markup until the end of stage C, and the flyout, the
 * Escape chain and the desk's own stylesheet rule all read them off the
 * elements -- but the answer is a field here as well, so a caller can ask
 * without a selector.
 *
 * NAV_OF stays in the legacy part: an outside gate parses the table out of that
 * file's text (scripts/rail-nav-registry.test.mjs), and the capsPage entry is a
 * function so that it can read the layer's own extTab. So this module imports
 * the one table rather than keeping a second copy of the keys; the import goes
 * when the part does.
 *
 * The two layers that used to decorate showPage subscribe here instead
 * (legacy/demo/152-skills.js, 153-plugins.js). Registration order is the order
 * their side effects were visible in, which is what the decorator reduce gave
 * them: every effect below runs first, then each subscriber in turn.
 */

import { islands } from '../islands'
import { markNewCurrent } from '../legacy/demo/050-rail.js'
import { NAV_OF } from '../legacy/demo/120-capabilities.js'
import * as detail from './detail'

/** The seven module pages, keyed as their <section> ids. */
export type PageId = 'capsPage' | 'xaPage' | 'connPage' | 'memPage' | 'pbPage' | 'kbPage' | 'cronPage'

let current: PageId | null = null
const listeners = new Set<() => void>()

/** Which module page is open, or null while the conversation is. */
export function get(): PageId | null {
  return current
}

/** For useSyncExternalStore: called after every show, effects done. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* Only one module page at a time. They used to cover the whole window, so two
   open at once was invisible; now that the rail stays put, the one behind shows
   through. */
export function show(id: PageId | null): void {
  current = id
  for (const p of Object.keys(NAV_OF)) {
    document.getElementById(p)!.dataset.open = String(p === id)
  }
  /* From the top, every time: the scroller keeps its position across a close
     and reopen, so a page could greet the reader halfway down its own list. */
  if (id) {
    const sc = document.getElementById(id)!.querySelector<HTMLElement>('.work')
    if (sc) sc.scrollTop = 0
  }
  /* Read by the rail: while a page is up it owns the selected state, so the
     session behind it stops claiming one too. */
  ;(document.querySelector('.app') as HTMLElement).dataset.page = id ? 'on' : 'off'
  markNewCurrent()
  /* caps and memory both use the shared detail drawer */
  if (id !== 'capsPage' && id !== 'memPage') detail.close()
  /* Same rule for the overlays a single page owns: the channel drawer and the
     new-job sheet used to survive the switch and sit over whatever came next,
     still showing the entry the reader had left behind. */
  if (id !== 'connPage') islands?.connections?.closeDialog?.()
  if (id !== 'cronPage') islands?.cron?.closeSheet?.()
  for (const fn of [...listeners]) fn()
}
