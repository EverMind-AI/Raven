/* The capabilities page: which tab it shows, what its filter bar says, and the
 * chrome around #capsBody.
 *
 * One section serves two modules -- the skill market and the plugin market --
 * and each of them used to write the same handful of elements from whichever
 * tab was up, by id, on every draw.
 *
 * All of it is state here, and src/chrome/CapsPage.tsx renders all of it: the
 * title, the search field's placeholder, the status pills' pressed flag, the two
 * "installed" buttons, and the four things a draw used to write on the markup by
 * id -- the section's aria-label, the manual-add fold's `hidden`, the filter
 * bar's `display`, and the hero above the bar, which is a child of .wrap the
 * component renders in its place rather than a node inserted before it.
 *
 * The draw is dispatched from here rather than from a chain of decorators. The
 * two tabs' renderers live beside the islands they draw (features/skills/tab.ts,
 * features/plugins/tab.ts), and this declares the order their steps run in
 * (see `draw`), the way
 * state/detail.ts declares the drawer's close order.
 *
 * Committed synchronously (flushSync), like the four stores stage C4 moved: a
 * draw writes what it writes and then hands over to an island, and the page's
 * boot takes its snapshot in the same task.
 */

import { T } from '../i18n/t'
import * as detail from './detail'
import * as page from './page'
import { ds } from './sources'
import { makeStore } from './store'
import { show as toast } from './toast'

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
  /** #cq's placeholder, same rule. It carries no key, so it has no catalogue. */
  readonly search: string | null
  readonly pillsHidden: boolean
  readonly skill: Installed | null
  readonly plugin: Installed | null
  /** #capsPage's aria-label, or null while no draw has named one: the served
   *  section carries none at all. */
  readonly label: string | null
  /** #advAdd's `hidden`. Served showing, which no reachable draw leaves it. */
  readonly advHidden: boolean
  /** .cbar's `display`, or null while nothing has set one -- the served bar has
   *  no inline style, and the two values a draw uses are '' and 'none'. */
  readonly bar: string | null
  /** The hero's heading, or null while no draw has made one: the served .wrap
   *  has no #pageHero, and a draw with no title leaves it standing but hidden. */
  readonly hero: string | null
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
  label: null,
  advHidden: false,
  bar: null,
  hero: null,
})

const store = makeStore<CapsState>(served())

const switched = new Set<() => void>()
const hooks: Partial<DrawHooks> = {}

/** The page's state, for <CapsPage/>. */
export const { get, subscribe } = store

/** A patch, merged into the page's state. */
export function set(next: Partial<CapsState>): void {
  store.set((prev) => ({ ...prev, ...next }))
}

const field = (): HTMLInputElement | null => document.getElementById('cq') as HTMLInputElement | null

/* What the two tab flips do after the switch itself: each island drops the
   state it had. Registration order is the order the two tabs' effects are
   visible in -- the skill tab's before the plugin tab's, which is the order
   src/main.tsx installs them in. */
export function onTab(fn: () => void): void {
  switched.add(fn)
}

/* Switching module resets the filters: a query typed while browsing skills is
   not a question about plugins. The field is cleared by hand because it is
   deliberately uncontrolled -- a component owning it would re-render a text
   field the reader is typing into -- and guarded because a harness may drive a
   flip against a page that has no filter bar. */
export function extSet(tab: Tab | null | undefined): void {
  if (!tab || tab === get().tab) return
  const el = field()
  if (el) el.value = ''
  set({ tab, kind: 'all', query: '' })
  /* caps and memory share the detail drawer, so the sheet a card left open
     would sit over the other tab's grid. */
  detail.close()
  for (const fn of [...switched]) fn()
}

/* Leaving the section, and the order is load-bearing: the page flag first,
   then the shared drawer -- which is not on any page, so closing the section
   does not reach it on its own. */
export function close(): void {
  page.show(null)
  detail.close()
}

/* A status pill: the filter it names, and the redraw the click asked for. The
   pills are hidden by every draw, so this is only reachable before the first
   one -- reproduced rather than fixed, like the rest of the bar. */
export function pick(kind: string): void {
  set({ kind })
  draw()
}

/* The filter bar's own term. Both tabs take a typed query through their own
   island, so this is the innermost layer of the old #cq chain -- the one the
   two above it fall through to, which no reachable tab does. */
export function setQuery(query: string): void {
  set({ query })
  draw()
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
  if (get().tab === 'plugin') {
    hooks.skillButton?.()
    hooks.plugin?.()
    hooks.hero?.()
    return
  }
  hooks.skill?.()
  hooks.pluginButton?.()
  hooks.hero?.()
}

/* A draw, but only while the page is open on the plugin tab: a plugin write
   that lands with the page shut, or on the other tab, has nothing to repaint.
   The island asks for this rather than calling `draw` itself, because "is my
   page the one on screen" is the page's answer and not the island's. */
export function drawIfOpenOnPlugins(): void {
  if (page.get() === 'capsPage' && get().tab === 'plugin') draw()
}

/* The chrome a draw decides, in one commit: the six values are what the two
   renderers wrote on six elements, and the component draws all six. */
export function chrome(next: Chrome): void {
  set({
    label: next.label,
    advHidden: next.advHidden,
    bar: next.bar,
    title: next.title,
    search: next.search,
    pillsHidden: next.pillsHidden,
  })
}

/* .cbar's display on its own. The installed view hides the bar and every other
   path puts it back, which is why three call sites outside a draw write it. */
export function bar(display: string): void {
  set({ bar: display })
}

/* One tab's "installed" button. The first call is what puts it in the bar --
   created once, by the part that owns the count it shows -- and every later one
   is its sync. */
export function installedButton(tab: Tab, next: Installed): void {
  set(tab === 'skill' ? { skill: next } : { plugin: next })
}

/* The page hero, first child of .wrap and so above the bar and outside
   #capsBody: one heading, renamed on every draw for whichever view is up, and
   standing but empty and hidden on a view that has none. Null until the first
   draw asks for one, because the served page has no such element -- which is
   what makes "the element appears with the first draw" a state rather than an
   insertion. */
export function hero(title: string): void {
  set({ hero: title })
}

/* Tests only: back to the state the page is served in, the two tabs' renderers
   included. What a case flips here is module state, and the component renders
   it, so the next case must not see it. */
export function _resetForTests(): void {
  store._resetForTests()
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
    await ds('plugins').manual(n, a)
  } catch (e) {
    toast(T('gui.plug.op_failed', { err: (e as Error).message || String(e) }))
    return
  }
  name.value = ''
  address.value = ''
  draw()
  toast(T('gui.adv.added_x', { name: n }))
}
