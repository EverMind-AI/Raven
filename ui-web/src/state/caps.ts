/* The capabilities page: which tab it shows, what its filter bar says, and the
 * chrome around #capsBody.
 *
 * Was four things in three legacy parts: `extTab` and `capFilter` in
 * demo/120-capabilities.js, the chrome half of `drawCaps` in demo/152-skills.js
 * and of `drawPlugTab` in demo/153-plugins.js, and the filter bar's three
 * handlers in demo/150-chrome.js. One section serves two modules -- the skill
 * market and the plugin market -- so every one of those wrote the same handful
 * of elements from whichever tab was up, by id, on every draw.
 *
 * What is state here is what src/chrome/CapsPage.tsx renders: the title, the
 * search field's placeholder, the status pills' pressed flag and the two
 * "installed" buttons. What is still written by hand is the three elements
 * src/page.html carries whose interiors are NOT this component's -- the
 * section's aria-label, the manual-add fold's `hidden`, the filter bar's
 * `display` -- and #pageHero, which sits between two static children of .wrap
 * and so cannot join a portal's child list before stage C14.
 *
 * The draw is dispatched from here rather than from a decorator chain. The two
 * tabs' renderers live in the parts that own the islands they draw, and this
 * declares the order their steps were visible in (see `draw`), the way
 * state/detail.ts declares the drawer's close order.
 *
 * Committed synchronously (flushSync), like the four stores stage C4 moved: a
 * draw writes what it writes and then hands over to an island, and the page's
 * boot takes its snapshot in the same task.
 */

import { flushSync } from 'react-dom'

import { T } from '../i18n/t'
import { drawCapsBadge } from '../legacy/demo/120-capabilities.js'
import { drawCaps } from '../legacy/demo/152-skills.js'
import { show as toast } from '../shell/toast'
import * as detail from './detail'
import { sources } from './sources'

/** The two modules the one section serves. */
export type Tab = 'skill' | 'plugin'

/** One tab's "installed" button: absent until its part creates it. */
export interface Installed {
  readonly hidden: boolean
  /** null while it has never been synced, which is how it is created. */
  readonly label: string | null
  readonly badge: string | null
}

export interface CapsState {
  readonly tab: Tab
  /** Which status pill is pressed (`data-k`). */
  readonly kind: string
  /** What the filter bar filters by, lowercased and trimmed at write time. */
  readonly query: string
  /** #capsTitle, or null while no draw has named it: the served literal stands. */
  readonly title: string | null
  /** #cq's placeholder, same rule. It carries no key, so no pass writes it. */
  readonly search: string | null
  readonly pillsHidden: boolean
  readonly skill: Installed | null
  readonly plugin: Installed | null
}

/** What a draw says about the chrome around the body. */
export interface Chrome {
  /** #capsTitle */
  readonly title: string
  /** #capsPage's aria-label: the tab's own name, installed view or not. */
  readonly label: string
  /** #cq's placeholder */
  readonly search: string
  /** #cKind -- both tabs hide it, which is why the pills are unreachable. */
  readonly pillsHidden: boolean
  /** #advAdd */
  readonly advHidden: boolean
  /** .cbar's display */
  readonly bar: string
}

/* The steps a draw is made of, each owned by the part that owns the island it
   draws. Named rather than a list, because the order they were visible in
   differs per tab (see `draw`). */
export interface DrawHooks {
  /** demo/152-skills.js: the skill tab, its own installed button included. */
  skill(): void
  /** Its button alone, which the plugin tab and the island's own changes sync. */
  skillButton(): void
  /** demo/153-plugins.js: the plugin tab. */
  plugin(): void
  pluginButton(): void
  /** The hero above the bar, last on both tabs. */
  hero(): void
}

const served = (): CapsState => ({
  tab: 'skill',
  kind: 'all',
  query: '',
  title: null,
  search: null,
  pillsHidden: false,
  skill: null,
  plugin: null,
})

let state: CapsState = served()

const listeners = new Set<() => void>()
const switched = new Set<() => void>()
const hooks: Partial<DrawHooks> = {}

/** The page's state, for <CapsPage/>. */
export function get(): CapsState {
  return state
}

/** For useSyncExternalStore: called whenever anything the component renders moves. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

function put(next: Partial<CapsState>): void {
  state = { ...state, ...next }
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

const field = (): HTMLInputElement | null => document.getElementById('cq') as HTMLInputElement | null

/* What the two tab flips do after the switch itself: each island drops the
   state it had. Registration order is the order the two decorators' effects
   were visible in -- the reduce applied them from the inside out, so the skill
   layer's ran before the plugin layer's (src/legacy/index.js installs 152 then
   153). */
export function onTab(fn: () => void): void {
  switched.add(fn)
}

/* Switching module resets the filters: a query typed while browsing skills is
   not a question about plugins. The field is cleared by hand because it is
   deliberately uncontrolled -- a component owning it would re-render a text
   field the reader is typing into -- and guarded because a harness may drive a
   flip against a page that has no filter bar. */
export function extSet(tab: Tab | null | undefined): void {
  if (!tab || tab === state.tab) return
  const el = field()
  if (el) el.value = ''
  put({ tab, kind: 'all', query: '' })
  /* caps and memory share the detail drawer, so the sheet a card left open
     would sit over the other tab's grid. */
  detail.close()
  for (const fn of [...switched]) fn()
}

/* A status pill: the filter it names, and the redraw the click asked for. The
   pills are hidden by every draw, so this is only reachable before the first
   one -- reproduced rather than fixed, like the rest of the bar. */
export function pick(kind: string): void {
  put({ kind })
  drawCaps()
}

/* The filter bar's own term. Both tabs take a typed query through their own
   island, so this is the innermost layer of the old #cq chain -- the one the
   two above it fall through to, which no reachable tab does. */
export function setQuery(query: string): void {
  put({ query })
  drawCaps()
}

/** The renderers, from the parts that own them. */
export function onDraw(part: Partial<DrawHooks>): void {
  Object.assign(hooks, part)
}

/* A draw of whichever tab is up, in the order the decorator chain made visible:
   the plugin layer wrapped the skill layer, so on the plugin tab the skill
   layer's button was synced first (it must not linger on this tab), and on the
   skill tab the plugin layer's button and the shared hero followed the skill
   draw. */
export function draw(): void {
  if (state.tab === 'plugin') {
    hooks.skillButton?.()
    hooks.plugin?.()
    hooks.hero?.()
    return
  }
  hooks.skill?.()
  hooks.pluginButton?.()
  hooks.hero?.()
}

/* The chrome a draw decides. The three elements page.html owns are written
   here, keeping the relative order the two renderers wrote them in; the three
   the component renders land in one commit after them. Unguarded, as the
   renderers were:
   all three are static markup, and a draw against a page that has none of it
   is the error its two callers already catch (redrawAll, state/install.ts). */
export function chrome(next: Chrome): void {
  document.getElementById('capsPage')!.setAttribute('aria-label', next.label)
  ;(document.getElementById('advAdd') as HTMLElement).hidden = next.advHidden
  bar(next.bar)
  put({ title: next.title, search: next.search, pillsHidden: next.pillsHidden })
}

/* .cbar's display. Its own attribute, not a rendered one: the bar is the
   container src/chrome/CapsPage.tsx portals into. The installed view hides it
   and every other path puts it back, which is why three call sites outside a
   draw write it too. */
export function bar(display: string): void {
  ;(document.querySelector('#capsPage .cbar') as HTMLElement).style.display = display
}

/* One tab's "installed" button. The first call is what puts it in the bar --
   created once, by the part that owns the count it shows -- and every later one
   is its sync. */
export function installedButton(tab: Tab, next: Installed): void {
  put(tab === 'skill' ? { skill: next } : { plugin: next })
}

/* The page hero, above the bar and so outside #capsBody: one node, repopulated
   on every draw for whichever view is up. It cannot be a rendered child before
   stage C14: .wrap's other children are still page.html's, and a portal appends
   after them rather than landing first. */
export function hero(title: string): void {
  let el = document.getElementById('pageHero')
  if (!el) {
    const anchor = document.querySelector('#capsPage .cbar') as HTMLElement
    el = document.createElement('div')
    el.className = 'pmhero'
    el.id = 'pageHero'
    anchor.parentNode!.insertBefore(el, anchor)
  }
  el.innerHTML = ''
  el.hidden = !title
  if (title) {
    const heading = document.createElement('h3')
    heading.textContent = title
    el.appendChild(heading)
  }
}

/* Tests only: back to the state the page is served in, the two tabs' renderers
   included. What a case flips here is module state, and the component renders
   it, so the next case must not see it. */
export function wipe(): void {
  state = served()
  switched.clear()
  for (const key of Object.keys(hooks)) delete hooks[key as keyof DrawHooks]
}

/* The manual add: a name and an address the reader types, handed to the source
   as a server to register. It fails against a resident gateway today -- neither
   raven.mcp.list nor raven.mcp.set is a declared method, so both go through the
   transport's unchecked path and the second is answered -32601 -- and the card
   shows that refusal. Kept exactly so: the fix is a declared method, not a
   quieter failure.
   The two fields are read and cleared by hand for the same reason #cq is. */
export async function manualAdd(): Promise<void> {
  const name = document.getElementById('mName') as HTMLInputElement
  const address = document.getElementById('mAddr') as HTMLInputElement
  const n = name.value.trim()
  const a = address.value.trim()
  if (!n || !a) {
    toast(T('gui.adv.need_fields'))
    return
  }
  try {
    await sources.plugins!.manual(n, a)
  } catch (e) {
    toast(T('gui.plug.op_failed', { err: (e as Error).message || String(e) }))
    return
  }
  name.value = ''
  address.value = ''
  drawCaps()
  drawCapsBadge()
  toast(T('gui.adv.added_x', { name: n }))
}
