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
 * (features/skills/wire.ts, features/plugins/wire.ts). Registration order is the
 * order their side effects are visible in: every effect below runs first, then
 * each subscriber in turn.
 *
 * What a switch asks of an island is registered rather than imported: three
 * calls used to reach into features/ from here, which put every store that
 * opens a page in the closure of every other and made this module the largest
 * single cost in the import graph. The slots below are filled by the page's
 * wiring (src/app/install.ts) and spent in the order `show` spells out.
 */

import * as caps from './caps'
import * as detail from './detail'
import { PAGES, pageOf } from './pages'

import type { PageId } from './pages'

export type { PageId }

/** The pages, and the button each lights up -- what markNew asks of the page
 *  registry. The More rows are not in here: the nav flyout marks its own.
 *
 *  Which button comes from the table (state/pages.ts); which of two is asked
 *  here, because the one page with two is the capabilities page and the answer
 *  is whichever of its tabs stands open -- and this module is where the tab is
 *  already read. */
export function navState(): { pages: string[]; btnOf(p: string): string | undefined } {
  return {
    pages: PAGES.map((page) => page.id),
    btnOf: (p) => {
      const buttons = pageOf(p)?.navButtons
      if (!buttons) return undefined
      if (buttons.length < 2) return buttons[0]
      return caps.get().tab === 'plugin' ? 'plugBtn' : 'skillBtn'
    },
  }
}

/** The three things a switch asks of an island, in the order it asks them. */
const SLOTS = ['markNav', 'closeConnDialog', 'closeCronSheet'] as const

/** One of the three callbacks `show` spends. */
export type ShowSlot = (typeof SLOTS)[number]

const slots = new Map<ShowSlot, () => void>()

/** Registers what a switch spends on an island. src/app/install.ts fills all three. */
export function onShow(name: ShowSlot, fn: () => void): void {
  slots.set(name, fn)
}

/* An unfilled slot is a switch that asks nothing: a page driven from a test
   registers the ones its case is about and the rest stay empty. */
const spend = (name: ShowSlot): void => {
  const fn = slots.get(name)
  if (fn) fn()
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

/* Test seam only: which page stands open is the module's. */
export function _resetForTests(): void {
  current = null
}

export function show(id: PageId | null): void {
  current = id
  for (const page of PAGES) {
    document.getElementById(page.id)!.dataset.open = String(page.id === id)
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
  spend('markNav')
  /* caps and memory both use the shared detail drawer */
  if (id !== 'capsPage' && id !== 'memoryPage') detail.close()
  /* Same rule for the overlays a single page owns: the channel drawer and the
     new-job sheet used to survive the switch and sit over whatever came next,
     still showing the entry the reader had left behind. */
  if (id !== 'connectionsPage') spend('closeConnDialog')
  if (id !== 'cronPage') spend('closeCronSheet')
  for (const fn of [...listeners]) fn()
}
