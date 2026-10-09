/* The connections hub: the agent page and the channel page, reached through
 * one rail row and switched between by the tabs over both (components/HubHead.tsx).
 *
 * They stay two module pages, each its own domain's island, because each
 * already owns its store, its sheet and its fetch; what makes them one place is
 * that they share a rail button (state/pages.ts), a header, and this: the
 * module the reader was last on, so the rail row goes back to it.
 *
 * Each domain registers what opening its module costs at its own module
 * evaluation, the way features/cron/store.ts registers on state/settings.ts:
 * state/ may not import an island, and the rail and the header would otherwise
 * each need both.
 */

import * as page from './page'

import type { PageId } from './pages'

export type HubModule = 'agents' | 'channels'

const PAGE_OF: Record<HubModule, PageId> = {
  agents: 'extAgentsPage',
  channels: 'connectionsPage',
}

const openers = new Map<HubModule, () => void>()

let last: HubModule = 'agents'

/** Registers what a domain does to open its module. */
export function onOpen(which: HubModule, fn: () => void): void {
  openers.set(which, fn)
}

/** Opens one module, or the one the reader was last on. */
export function open(which: HubModule = last): void {
  openers.get(which)?.()
}

/* Followed off the page switch rather than set by `open`: the greeting's entry
   opens the channel page through its own store, and the rail should come back
   to where the reader actually was, whichever door they used. */
page.subscribe(() => {
  const at = page.get()
  for (const which of Object.keys(PAGE_OF) as HubModule[]) {
    if (PAGE_OF[which] === at) last = which
  }
})

/** The module the rail row opens next. */
export function lastOpened(): HubModule {
  return last
}

export function _resetForTests(): void {
  last = 'agents'
  openers.clear()
}
