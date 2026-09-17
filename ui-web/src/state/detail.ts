/* The shared detail drawer: whose card is in it, whether it is up, and the host
 * each island renders that card into.
 *
 * Four islands drew into the same #detail dialog by id -- memory, plugins,
 * skills and extAgents. Each cleared #dBody with innerHTML, blanked #dTitle,
 * wrote data-open for itself, and two of them watched the element with a
 * MutationObserver to learn that one of the others had closed it. The drawer's
 * state was therefore a reading of the DOM, with four writers and no order.
 *
 * It is one store now. An island calls open() and close(), renders into the
 * host handed to it here, and hears about a close through the handler it
 * registers. The two flags are still written imperatively, because aside#detail
 * is rendered with the flag the page is served with and so is not
 * React's to render; #dBody's child list is one host per owner, which React
 * never reconciles either. What IS React's is the interior App.tsx renders --
 * #dTitle's text and the close button.
 *
 * Close order is a contract rather than an accident. The legacy verb was
 * wrapped by two decorators and the reduce made their effects visible
 * outermost-first: plugins, then skills, then the base flag write, with the two
 * observers following in a microtask after all of it. CLOSE_ORDER is that
 * order, declared -- registration order is module evaluation order, which is
 * not something this contract should rest on.
 */

import { makeStore } from './store'

/** The four islands that share the drawer. */
export type DetailOwner = 'memory' | 'plugins' | 'skills' | 'extAgents'

export type DetailState = {
  readonly owner: DetailOwner | null
  readonly open: boolean
  /** A card whose body is fetched on open holds a settled box while it lands. */
  readonly fill: boolean
  /** Null while no card has claimed the header, so the served literal stands. */
  readonly title: string | null
  /** Opens, counted: which open a pending drop belongs to. */
  readonly gen: number
}

const CLOSE_ORDER: readonly DetailOwner[] = ['plugins', 'skills', 'memory', 'extAgents']

/* Kept in step with `.detail`'s opacity transition in page.css. A little
   longer than the transition, so the drop lands after the last painted frame
   rather than in the middle of it. */
const FADE_MS = 260

const store = makeStore<DetailState>({ owner: null, open: false, fill: false, title: null, gen: 0 })
const closers = new Map<DetailOwner, () => void>()
const hosts = new Map<DetailOwner, HTMLDivElement>()

/** Which card the drawer holds; `set` is every open, close and drop. */
export const { get, set, subscribe, _resetForTests } = store

/** What an owner runs when the drawer closes: drop its card, after the fade. */
export function onClose(owner: DetailOwner, fn: () => void): void {
  closers.set(owner, fn)
}

/* The card is dropped a fade after the flag, not in the tick it flipped, or
 * what fades is an empty strip where a card had been -- measured on the skill
 * card, 630px of content became a 43px bar one millisecond after the close, and
 * that bar is what the eye caught on the way out. `stale` is asked again at the
 * end because a reader may open another card inside that window: the second
 * open has already written the state, and this must not wipe theirs on the way
 * past.
 */
export function dropAfterFade(drop: () => void, stale: () => boolean): void {
  window.setTimeout(() => {
    if (stale()) return
    drop()
  }, FADE_MS)
}

/* One host per owner, made on first ask and never replaced: every open
   re-adopts it, so an island keeps rendering into the same node across the
   wipes below and React never meets one a wipe orphaned. `display: contents`
   for two of them, whose sections were #dBody's own grid items before there was
   a container between them -- the skill and plugin cards were always one box,
   and the gap between sections is the difference. */
export function host(owner: DetailOwner): HTMLDivElement {
  let el = hosts.get(owner)
  if (!el) {
    el = document.createElement('div')
    if (owner === 'memory' || owner === 'extAgents') el.style.display = 'contents'
    hosts.set(owner, el)
  }
  return el
}

/* One writer for the two flags, so they cannot disagree with the state.
   data-fill used to be removed by whoever had set it, a fade after the close,
   which left a memory card wearing the skill card's fixed height for as long as
   that took. */
function paint(): void {
  const el = document.getElementById('detail')
  if (!el) return
  el.dataset.open = String(get().open)
  if (get().fill) el.dataset.fill = 'true'
  else delete el.dataset.fill
}

/* #dBody holds exactly one child: the owner's host. Cleared wholesale rather
   than by removing the host that was there, which is what each opener did. */
function adopt(owner: DetailOwner): void {
  const body = document.getElementById('dBody')
  if (!body) return
  const el = host(owner)
  if (el.parentElement === body) return
  body.innerHTML = ''
  body.appendChild(el)
}

/* A different card in a drawer already open for this owner is not a new open:
   `gen` answers "has the reader opened something since", which is the question
   a pending drop asks, and reopening inside the fade is the case it exists
   for. */
export function open(owner: DetailOwner, card: { title?: string; fill?: boolean } = {}): void {
  const was = get()
  const reopened = !was.open || was.owner !== owner
  set({
    owner,
    open: true,
    fill: card.fill ?? false,
    title: card.title ?? '',
    gen: reopened ? was.gen + 1 : was.gen,
  })
  adopt(owner)
  paint()
}

/** Every close path: the close button, the scrim, Escape, a page switch, a
    language flip, an island's own button. */
export function close(): void {
  for (const owner of CLOSE_ORDER) closers.get(owner)?.()
  const had = get().owner
  set({ ...get(), open: false })
  paint()
  if (!had) return
  const gen = get().gen
  dropAfterFade(
    () => {
      set({ ...get(), owner: null, fill: false })
      paint()
    },
    () => get().gen !== gen,
  )
}
