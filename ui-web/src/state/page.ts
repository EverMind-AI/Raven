/* Which module page is open, and the only place that opens one.
 *
 * Seven writes used to be the whole of the state: "which page is up" was
 * readable only by asking the DOM what carried data-open="true". The writes
 * stay, and stay here: the flyout,
 * the Escape chain and the desk's own stylesheet rule all read the flag off the
 * elements, the order these seven land in relative to the four effects below is
 * the contract this module's gate pins, and src/App.tsx renders each section
 * with the value the page is served with and then never writes it again. The
 * answer is a field here as well, so a caller can ask without a selector.
 *
 * The two tabs that used to decorate this function subscribe here instead
 * (features/skills/tab.ts, features/plugins/tab.ts). Registration order is the
 * order their side effects are visible in: every effect below runs first, then
 * each subscriber in turn.
 */

import { islands } from '../islands'
import * as caps from './caps'
import * as detail from './detail'

/** The seven module pages, keyed as their <section> ids. */
export type PageId = 'capsPage' | 'xaPage' | 'connPage' | 'memPage' | 'pbPage' | 'kbPage' | 'cronPage'

/* Which rail button each page lights up. Only one module page is open at a
   time: they used to cover the whole window, so two open at once was invisible;
   now that the rail stays put, the one behind shows through.

   Named rather than derived, and the capabilities page's entry is a function
   because that one section serves two modules and lights whichever tab stands
   open. An outside gate reads this table against the rail's own list of the
   buttons it writes (scripts/rail-nav-registry.test.mjs), because a page in
   here that the rail does not mark has no selected state at all and nothing
   else fails. */
const NAV_OF = {
  capsPage: () => (caps.get().tab === 'plugin' ? 'plugBtn' : 'skillBtn'),
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  memPage: 'memBtn',
  pbPage: 'pbBtn',
  kbPage: 'kbBtn',
  cronPage: 'moreBtn',
}

/** The pages, and the button each lights up -- what markNew asks of the page
 *  registry. The More rows are not in here: the nav flyout marks its own. */
export function navState(): { pages: string[]; btnOf(p: string): string | undefined } {
  return {
    pages: Object.keys(NAV_OF),
    btnOf: (p) => {
      const of = NAV_OF[p as keyof typeof NAV_OF]
      return typeof of === 'function' ? of() : of
    },
  }
}

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
  islands.rail.markNew()
  /* caps and memory both use the shared detail drawer */
  if (id !== 'capsPage' && id !== 'memPage') detail.close()
  /* Same rule for the overlays a single page owns: the channel drawer and the
     new-job sheet used to survive the switch and sit over whatever came next,
     still showing the entry the reader had left behind. */
  if (id !== 'connPage') islands?.connections?.closeDialog?.()
  if (id !== 'cronPage') islands?.cron?.closeSheet?.()
  for (const fn of [...listeners]) fn()
}
