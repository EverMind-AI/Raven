/* The connections hub: the agents, the machines and the channels, reached
 * through one rail row and switched between by the tabs over all three
 * (components/HubHead.tsx).
 *
 * Agents and channels stay two module pages, each its own domain's island,
 * because each already owns its store, its sheet and its fetch; the machines
 * are the agents island's other module -- the same page, drawn by the same
 * store (features/extAgents/store.ts) -- since a machine is where an agent
 * row runs and the two share their cards, sheets and drawer. What makes the
 * three one place is a shared rail button (state/pages.ts), a header, and
 * this: the module the reader was last on, so the rail row goes back to it.
 *
 * Each domain registers what opening its module costs at its own module
 * evaluation, the way features/cron/store.ts registers on state/settings.ts:
 * state/ may not import an island, and the rail and the header would otherwise
 * each need both.
 */

import * as page from './page'

export type HubModule = 'agents' | 'channels' | 'machines'

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
   to where the reader actually was, whichever door they used. The agents page
   carries two modules, which the switch cannot tell apart: the island says
   which it shows (`visited`), and a switch onto that page only moves the hub
   off the channels. */
page.subscribe(() => {
  const at = page.get()
  if (at === 'connectionsPage') last = 'channels'
  else if (at === 'extAgentsPage' && last === 'channels') last = 'agents'
})

/** What the island on the shared page is showing, so the rail row returns to it. */
export function visited(which: HubModule): void {
  last = which
}

/** The module the rail row opens next. */
export function lastOpened(): HubModule {
  return last
}

export function _resetForTests(): void {
  last = 'agents'
  openers.clear()
}
