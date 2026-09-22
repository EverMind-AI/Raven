/* Everything that sits at the body rather than inside a page, and the order it
 * sits in.
 *
 * Two steps of the `--z` ladder in src/styles/page.css are deliberate ties --
 * `--z-shade` with `--z-tip` at 90, and `--z-picker` with the inline 46 the two
 * composer popovers set -- so for those four elements the DOM order at the body
 * IS the whole of the stacking decision. Left to the appending code, that order
 * would be an accident of which module ran first, and nothing would say the
 * four had to land in the order the stylesheet assumes.
 *
 * The order is declared here instead, once, and `host` is what hands out the
 * four boot-time layers: made on first ask, appended in the order of this
 * table rather than the order of the asking. The tie at 90 reads backwards from
 * what page.css's comment intends -- the shade covers the tooltip, because
 * .tipp is appended at boot and .upshade only when an upgrade starts -- and
 * this table reproduces the measurement, not the intent.
 *
 * Three kinds are distinguished, because each breaks differently:
 *   static   -- at the body from the page root's first commit, and the writer
 *               only fills it (src/App.tsx renders it with no children).
 *   reparent -- born inside a page, moved to the body on first open, and never
 *               moved back.
 *   append   -- created at runtime and appended to the body.
 */

import { PAGES } from './pages'

export interface Portal {
  /** What the element is called, and the table's key: unique across the
   *  thirteen. */
  readonly id: string
  /** The selector that finds it, for the eleven that have one to themselves.
   *  The other two are named only: the model picker's wrapper carries neither
   *  id nor class (`make` below says why), and `bootErrorBar` is a row of the
   *  design's table that nothing in src/ builds. Anything looking these up in
   *  the document has to skip the two without a selector rather than take two
   *  nulls it cannot tell from a missing element. */
  readonly selector?: string
  readonly kind: 'static' | 'reparent' | 'append'
  /** The `--z` token it takes, or the literal an inline style sets. */
  readonly z: string
  /** Its 1-based place among the body's children at boot, or `last` for the
   *  ones that only reach the body when something opens them. */
  readonly at: number | 'last'
  /** The line a boot golden shows for it, for the four that are there at boot
   *  and the two static hosts. */
  readonly bootKey?: string
}

/* The body's standing order after boot, as the goldens record it: seventeen
   static regions (#splash and #noJs are removed before the snapshot is taken)
   then the four appended while the page installs itself. Keyed by tag plus id,
   or tag plus classes when there is no id -- the model picker's wrapper has
   neither, which is why one entry is a bare `div`. The seven module pages are
   the table in state/pages.ts, in its order, because that IS the order they
   are rendered in (src/App.tsx). */
export const BOOT_BODY_ORDER = [
  'div#onb',
  'div.app',
  'button#railShow',
  ...PAGES.map((page) => `section#${page.id}`),
  'aside#detail',
  'div#setVeil',
  'div#veil',
  'div#menu',
  'div#toasts',
  'div.sbars',
  'div',
  'div#deskHost',
  'div.tipp',
]

/* Where a boot-time layer stands among the body's children, read off the order
   above rather than written down: one insertion into a page list used to
   invalidate six absolute indices in this file and three more in its tests. */
const at = (bootKey: string): number => BOOT_BODY_ORDER.indexOf(bootKey) + 1

/* The thirteen, in the order the design's portal table lists them. */
export const PORTALS: readonly Portal[] = [
  { id: '.sbars', selector: '.sbars', kind: 'append', z: '--z-scrollbars', at: at('div.sbars'), bootKey: 'div.sbars' },
  { id: 'pickHost', kind: 'append', z: '--z-picker', at: at('div'), bootKey: 'div' },
  { id: '#deskHost', selector: '#deskHost', kind: 'append', z: '--z-desk', at: at('div#deskHost'), bootKey: 'div#deskHost' },
  { id: '.tipp', selector: '.tipp', kind: 'append', z: '--z-tip', at: at('div.tipp'), bootKey: 'div.tipp' },
  { id: '#permPop', selector: '#permPop', kind: 'reparent', z: '46', at: 'last' },
  { id: '#tierPop', selector: '#tierPop', kind: 'reparent', z: '46', at: 'last' },
  { id: 'button.lightbox', selector: 'button.lightbox', kind: 'append', z: '--z-lightbox', at: 'last' },
  { id: '.upshade', selector: '.upshade', kind: 'append', z: '--z-shade', at: 'last' },
  { id: '.topfail', selector: '.topfail', kind: 'append', z: '--z-failbar', at: 'last' },
  { id: 'bootErrorBar', kind: 'append', z: '99', at: 'last' },
  { id: 'input[type=file]', selector: 'input[type=file]', kind: 'append', z: '', at: 'last' },
  { id: '#menu', selector: '#menu', kind: 'static', z: '--z-menu', at: at('div#menu'), bootKey: 'div#menu' },
  { id: '#toasts', selector: '#toasts', kind: 'static', z: '--z-toast', at: at('div#toasts'), bootKey: 'div#toasts' },
]

/** The four layers this module hands out, in the order they belong at the body. */
export const LAYERS = ['sbars', 'picker', 'desk', 'tip'] as const

export type Layer = (typeof LAYERS)[number]

const made = new Map<Layer, HTMLElement>()

/* Each layer as its own module makes it: the scrollbar layer is `.sbars`, the
   model picker's wrapper carries neither id nor class (it is inert for layout;
   `.mpick` inside it is position: fixed), #deskHost is addressed by three
   `body:has(...) #deskHost` rules in page.css, and the tooltip layer is
   `.tipp`. */
function make(name: Layer): HTMLElement {
  const el = document.createElement('div')
  if (name === 'sbars') el.className = 'sbars'
  else if (name === 'desk') el.id = 'deskHost'
  else if (name === 'tip') el.className = 'tipp'
  return el
}

/* The layer, made and appended on the first ask and handed back on every one
 * after -- lazily, because WHEN it is asked for is also what the boot goldens
 * record, and a layer made eagerly here would move them.
 *
 * `isConnected` rather than a plain cache hit: a test that replaced the body
 * under us has to get a layer that is in the document, which is what the
 * scrollbar module's own accessor did.
 */
export function host(name: Layer): HTMLElement {
  const had = made.get(name)
  if (had?.isConnected) return had
  const el = make(name)
  made.set(name, el)
  /* Ahead of the first later layer already standing, so the table decides the
     order even when the asking does not. */
  const after = LAYERS.slice(LAYERS.indexOf(name) + 1)
    .map((later) => made.get(later))
    .find((later) => later?.isConnected)
  if (after) document.body.insertBefore(el, after)
  else document.body.appendChild(el)
  return el
}

/** Forgets the layers, so a test starting from a fresh body gets fresh ones. */
export function _resetForTests(): void {
  made.clear()
}
