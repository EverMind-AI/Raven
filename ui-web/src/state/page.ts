/* Which module page is open, and the only place that opens one.
 *
 * The open flags used to be the whole of the state: "which page is up" was
 * readable only by asking the DOM what carried data-open="true". The writes
 * stay, and stay here: the Escape chain and the desk's own stylesheet rule
 * both read the flag off the elements, the order they land in relative to the
 * effects below is the contract this module's gate pins, and src/App.tsx
 * renders each section with the value the page is served with and then never
 * writes it again. The answer is a field here as well, so a caller can ask
 * without a selector.
 *
 * The two tabs that used to decorate this function subscribe here instead.
 * What a switch asks of an island is registered rather than imported: three
 * calls used to reach into features/ from here, which put every store that
 * opens a page in the closure of every other and made this module the largest
 * single cost in the import graph. The slot below is filled by the page's
 * wiring (src/app/install.ts) and spent where `show` spells out.
 */

import * as detail from './detail'
import { PAGES, pageOf } from './pages'

import type { PageId } from './pages'

export type { PageId }

/* Which of a page's two buttons is lit, answered by the module whose own state
   decides it. The one page with two is the capabilities page and the answer is
   whichever of its tabs stands open, and reading that tab from here made this
   module and state/caps.ts import each other. Filled at that module's
   evaluation and empty until then: a page driven from a test lights the first
   of the two, which is the tab its section is served on. */
const buttonOf = new Map<string, () => string>()

/** Registers which of a page's two buttons its own state means. */
export function onNavButton(page: PageId, pick: () => string): void {
  buttonOf.set(page, pick)
}

/** The pages, and the button each lights up -- what markNew asks of the page
 *  registry. The More rows are not in here: the nav flyout marks its own.
 *
 *  Which button comes from the table (state/pages.ts); which of two comes from
 *  whoever registered the answer above. */
export function navState(): { pages: string[]; btnOf(p: string): string | undefined } {
  return {
    pages: PAGES.map((page) => page.id),
    btnOf: (p) => {
      const buttons = pageOf(p)?.navButtons
      if (!buttons) return undefined
      if (buttons.length < 2) return buttons[0]
      return buttonOf.get(p)?.() ?? buttons[0]
    },
  }
}

/** What a switch asks of an island. */
const SLOTS = ['markNav'] as const

/** The one callback `show` spends. */
export type ShowSlot = (typeof SLOTS)[number]

const slots = new Map<ShowSlot, () => void>()

/** Registers what a switch spends on an island. src/app/install.ts fills it. */
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
  /* The shared drawer, unconditionally. It used to stay open for the one page
     that filled it -- the memory page, which is a settings section now and has
     its detail beside its list instead. The one page that still raises the
     drawer raises it from a row, never from the switch, so a switch is always
     a reader leaving whatever it was showing. */
  detail.close()
  for (const fn of [...listeners]) fn()
}
