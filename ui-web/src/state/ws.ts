/* The workspace pane's own state: whether it stands, which view it shows,
 * whether it has taken the window, and how many changed files nobody has read.
 *
 * Four module lets and six verbs, and not to be confused with
 * features/workspace/store.ts, which is the ISLAND: what a view draws inside
 * #wsBody, and the record it draws from (features/workspace/record.ts). What is
 * here is the chrome around that box -- the pane, the tab strip and the two
 * action buttons, which src/chrome/WsPane.tsx renders.
 *
 * The DOM writes stay imperative, and each lands on an element this module is
 * the only writer of:
 *   - #split's data-open and data-full. The grid is where the stylesheet reads
 *     "is the pane up" off (`.split[data-open="true"] #wsBtn{display:none}`,
 *     src/styles/page.css), and it is a container src/page.html still carries.
 *   - #wsBtn's aria-expanded, tooltip and label, and #wsBdg's count. Both are
 *     in the chat header, which is a region of its own.
 *   - #wsWide's four attributes and the tab strip's aria-selected. Those
 *     elements are this region's and the component renders them -- but React
 *     diffs against the props it rendered last rather than against the
 *     document, so an attribute it renders a constant for is one it never
 *     writes again, and keeping the write here keeps each value to one writer
 *     and to exactly the moment it lands today. The tab strip is the one where
 *     that moment is observable: draw() is its only repaint, so a restore that
 *     arrives while the pane is collapsed leaves the strip showing the view it
 *     last drew, and only the next open repaints it.
 *
 * The four facts are exported as bindings as well as through view(), because
 * that is how the three modules that ask "is the pane up" read them -- the
 * arrangement the catalogue's language column already has (src/i18n/t.ts's
 * `code`).
 */

import { notifyDesk, openDeskTab, reset as resetDesk } from '../features/workspace/deskStore'
import { reset as resetSubagents } from '../features/subagents/store'
import * as workspace from '../features/workspace/store'
import { T } from '../i18n/t'

import type { DeskTab } from '../features/workspace/deskTypes'
import type { WsPanelView } from './wsPanel'

/** Which view the pane shows: diff, file, browser or agents. */
let tab = 'diff'
/** Whether the pane stands. Served collapsed -- page.html carries no data-open. */
let open = false
/** Whether the pane has taken the whole window. */
let wide = false
/** Whether the reader picked the view, rather than the launcher standing in. */
let picked = false

/* Bumped on every redraw. A view that fetches before it can render must
   re-check this before appending, or a slow answer lands in whatever view the
   user switched to meanwhile. */
let epoch = 0

/* Unguarded on purpose: every element below is markup the page always has, and
   a missing one is a boot step that failed loudly rather than a pane half
   open. */
const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement

/* Expanded is a display mode, not a width: the pane leaves the grid and covers
   the window, so nothing here touches --wsw and the dragged width is waiting
   unchanged on the way back. */
export function setFull(on: boolean): void {
  wide = !!on
  el('split').dataset.full = String(wide)
  const b = el('wsWide')
  b.classList.toggle('on', wide)
  const k = wide ? 'gui.ws.restore_panel' : 'gui.ws.expand_panel'
  b.dataset.tip = T(k)
  b.setAttribute('aria-label', T(k))
  b.setAttribute('aria-pressed', String(wide))
}

/* The header chip is the only thing allowed to interrupt, and only by
   counting. Running commands add a dot so "still going" reads at a glance.

   The count is CHANGED FILES, not tool calls: five edits to one file is one
   thing to look at, and "+5" for a single file reads as a lie. */
export function bump(): void {
  const ws = workspace.shared()
  ws.unseen = ws.changes.filter((c) => !c.seen).length
  const n = ws.unseen
  const chip = el('wsBdg')
  chip.textContent = n ? `+${n}` : ''
  chip.hidden = n === 0
  const u = document.getElementById('wsUnseen')
  if (u) {
    u.textContent = n ? `+${n}` : ''
    u.hidden = n === 0
  }
  notifyDesk()
}

export function draw(): void {
  /* The island owns #wsBody; the agents and browser views draw into it
     through their own islands, dispatched by the workspace island's draw. */
  epoch += 1
  ;[...el('wsTabs').children].forEach((b) => {
    b.setAttribute('aria-selected', String((b as HTMLElement).dataset.w === tab))
  })
  workspace.draw()
}

export function setOpen(next: boolean, view?: string): void {
  if (next && view && document.documentElement.classList.contains('desk-ready')) {
    if (view !== 'browser') {
      openDeskTab(view as DeskTab)
      return
    }
  }
  open = next
  if (view) tab = view
  /* Collapsing leaves expanded mode too: coming back to a full-window pane
     that was dismissed is never what the reader meant. */
  if (!next) setFull(false)
  el('split').dataset.open = String(next)
  const b = el('wsBtn')
  b.setAttribute('aria-expanded', String(next))
  const k = next ? 'gui.collapse_ws' : 'gui.expand_ws'
  b.dataset.tip = T(k)
  b.setAttribute('aria-label', T(k))
  if (next) draw()
  bump()
}

/* Putting one view on screen: the pane opens if it is not already, and then the
   view is picked. One verb because both callers mean exactly that, and a pick
   against a collapsed pane would draw into a panel nobody can see. */
export function show(view: string): void {
  if (!open) setOpen(true)
  pick(view)
}

/* Picking a view is a commitment: from then on that view shows its own empty
   note rather than being replaced by the launcher. */
export function pick(view: string): void {
  if (document.documentElement.classList.contains('desk-ready')) {
    if (view !== 'browser') {
      /* Cast, not validated: the desk's own opener is written for these two
         call sites and stops a view name it does not know (openDeskTab in
         features/workspace/deskStore.ts). */
      openDeskTab(view as DeskTab)
      return
    }
  }
  tab = view
  picked = true
  draw()
  bump()
}

/* The pane's state, out and back. The residency rule is the caller
   (src/state/session/residency.ts): it saves this when the reader leaves a
   session mid-turn and hands it back on return. Two functions rather than two
   bindings for it to read and write -- which view is up and whether the reader
   chose it belong to the pane, and only the pane knows a restore is not a
   fresh pick. */
export function view(): WsPanelView {
  return { tab, open, picked }
}

/* `wasPicked` is unknown rather than boolean because the caller's is: a parked
   runtime's pane state comes back off a snapshot, and the coercion belongs
   here. */
export function restore(next: string, wasPicked: unknown): void {
  tab = next || 'diff'
  picked = !!wasPicked
}

export function reset(): void {
  tab = 'diff'
  picked = false
  /* A different session is a different workspace state: the island clears the
     record and drops the file tree listings it read before the switch. */
  workspace.reset()
  resetDesk()
  /* Subagents belong to the session that spawned them, so they leave with it
     -- carrying the list into the next conversation would attribute one
     conversation's background work to another. The open dag node goes for the
     same reason, and because `dag.node` is addressed by session: left set, the
     pane would ask the newly opened conversation for a run it never made. */
  resetSubagents()
}

export const stale = (mine: number): boolean => mine !== epoch

/* Whether the view on screen draws anything a tool call changes. Redrawing on
   every tool.start and every tool.complete of a running turn -- for a view
   that shows none of that state -- is a pane that flickers once per call for
   the length of the turn. The sub-agent tab shows nothing a tool event
   repaints, so it is exempt. */
export const showsTurn = (): boolean => open && tab !== 'agents'

/* Shrinking past the split point must not leave the conversation hidden. There
   is deliberately no initial check: this is the window taking a standing pane
   down, not a rule about how narrow pages open.

   The registration MOMENT belongs to src/main.tsx, which calls this after
   every listener the page registers itself, because it is one of the listeners
   the boot order is measured over; what happens on a match belongs here. The
   query is
   handed back for that caller to hold, so nothing keeps a reference to it here.
*/
export function watchNarrow(): MediaQueryList {
  const query = matchMedia('(max-width: 1040px)')
  query.addEventListener('change', (e) => {
    if (e.matches && open) setOpen(false)
  })
  return query
}

export { open, tab, wide, picked, epoch }

/* Test seam only: which tab the panel shows, whether it stands open and how
   wide are all the module's. */
export function _resetForTests(): void {
  tab = 'diff'
  open = false
  wide = false
  picked = false
  epoch = 0
}
