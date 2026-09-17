/* The sub-agent effort tier: the chip on the composer (#tierChip) and the panel
 * it opens (#tierPop).
 *
 * A store rather than a writer, for the reasons `perm.ts` gives next door:
 * <TierChip/> and <TierPop/> render both nodes from it (src/chrome/TierChip.tsx,
 * src/chrome/TierPop.tsx), the panel is still moved to the body by hand to be
 * positioned at all, and what stays here decides. The two chips sit side by side
 * and are built the same way on purpose -- a reader meets them as one row of
 * session settings, not as two unrelated controls.
 *
 * What it is NOT is a model. What it IS depends on the catalogue, and the panel
 * says which rather than assuming.
 *
 * A **Session Tier** is `medium`/`high`/`max` and nothing else: `clamp_tier`
 * recognises those three names and declines every other vocabulary, so those are
 * the rungs that actually reach a sub-agent. A **Session Mode** is any profile a
 * deployment declares, and its two knobs -- the iteration cap and the overlay --
 * are the loop's own. CONTEXT.md keeps them as two entries and warns against
 * assuming a replacement catalogue carries a tier.
 *
 * So the heading and the footer are chosen from whether every rung on offer is
 * one the ladder ranks, which is the same test the clamp applies. Naming a
 * deployment's `quick`/`thorough` "sub-agent effort" was not merely loose: those
 * reach no sub-agent at all, and what they do move is raven's own cap.
 *
 * Neither wording claims raven's effort is unchanged, though the shipped
 * catalogue does leave it alone. The reply carries an id, a name and a
 * description -- never the knobs -- so a catalogue that names its rungs
 * `medium`/`high`/`max` AND sets an iteration cap is indistinguishable here from
 * the shipped one. The control says the part it can stand behind: where the tier
 * goes.
 *
 * The menu is the server's, not a constant here. `session.set_mode` answers
 * every call -- report, set and clear alike -- with the tier in force and the
 * whole catalogue, which is why one round trip is enough to draw the control. A
 * deployment can replace the catalogue or turn it off entirely (`"modes": {}`),
 * and an empty menu means this build has no tier to offer: the chip stays
 * hidden rather than drawing a control over nothing.
 *
 * The state lives on the server, so nothing is mirrored to localStorage. A click
 * paints from the REPLY, never from the click: the handler is what decides
 * whether the tier was accepted, and painting first would show a tier that is
 * not in force whenever it refuses.
 */

import { flushSync } from 'react-dom'

import { ds, t } from './bridge'
import { show as toast } from './toast'

export interface TierOption {
  id: string
  name?: string
  description?: string
}

export interface TierReply {
  mode?: string | null
  availableModes?: TierOption[]
}

/* The seam, published by the live layer over `session.set_mode` and by the demo
   shell over a fixture. Both calls take the same shape the wire does: no `mode`
   reports, a `mode` switches. */
export interface TierSource {
  read(): Promise<TierReply>
  set(mode: string): Promise<TierReply>
}

let mode = ''
let menu: TierOption[] = []
let loaded = false

/** One row of the panel, as the open that built it read the catalogue. */
export interface TierRow {
  readonly id: string
  readonly name: string
  /** The rung's own sentence, on the line and on the title. Absent when the
   *  catalogue sent none, and then neither is rendered. */
  readonly sub?: string
  /** The bars, as markup, because that is how ICO spells them. */
  readonly ico: string
  readonly ticked: boolean
}

export interface TierPanel {
  /** Up or down: the panel's data-open and the chip's aria-expanded. */
  readonly open: boolean
  /* The rows the last open built, or null while the panel has never been
     opened. Not cleared when it closes, because closing only hid the panel
     before and may not start emptying it -- so the catalogue here is the one
     that answered before the last open, not the one in force. */
  readonly listed: readonly TierRow[] | null
  /** Bumped by every open, so a panel opened twice is measured twice. */
  readonly opened: number
}

const shut: TierPanel = { open: false, listed: null, opened: 0 }
let panel: TierPanel = shut
const listeners = new Set<() => void>()

/** The panel's state, for <TierPop/> and <TierChip/>. */
export function get(): TierPanel {
  return panel
}

/** For useSyncExternalStore: called whenever the panel changes. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* Committed synchronously, for the reason perm.ts's put gives: `open` measures
   the panel it has just filled. */
function put(next: TierPanel): void {
  if (next.open === panel.open && next.listed === panel.listed && next.opened === panel.opened) return
  panel = next
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

/* Who else the tier moves. It is a session-wide setting, and a surface showing
   what one sub-agent will run at is showing this value clamped -- so it has to
   hear about a switch, or it goes on naming the rung from before.

   A notification rather than a call into whoever cares, for the reason the
   composer rack gives for `watchAsking`: this module has no business knowing
   that an instance pane exists. It says the value moved; what that is worth is
   the listener's to decide. */
const WATCHERS = new Set<(next: string) => void>()

export function watch(fn: (next: string) => void): () => void {
  WATCHERS.add(fn)
  return () => WATCHERS.delete(fn)
}

function told(): void {
  WATCHERS.forEach((fn) => {
    try {
      fn(mode)
    } catch {
      /* A listener is a courtesy; one that throws must not take the chip down. */
    }
  })
}

export const current = (): string => mode
export const options = (): readonly TierOption[] => menu

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* `TIER_LADDER` (`raven/config/schema.py`), cheapest first. Held here because
   the reply cannot say whether a rung is one: it carries ids and names, and
   which of them the clamp ranks is a fact about the ladder, not about the
   catalogue. The one place this side spells the three names. */
const LADDER: readonly string[] = ['medium', 'high', 'max']

/* Two questions, and they are answered at different scopes -- which is the whole
   of what this got wrong the first time.

   What to CALL the catalogue is a fact about the catalogue: every rung on offer
   is one the ladder ranks, or a deployment declared its own vocabulary and this
   is a Session Mode catalogue. Every rather than any, because a mixed one is
   read rung by rung and a heading cannot be half true.

   Where the choice GOES is a fact about the rung in force. `clamp_tier` honours
   an exact hit in any vocabulary that spells the rung the same way, so in a
   `{high, thorough}` catalogue `high` reaches sub-agents and `thorough` does not
   -- one catalogue, two answers. A footer keyed to the catalogue said "not
   offered to sub-agents" over a rung that is. */
const ranked = (): boolean => menu.length > 0 && menu.every((m) => LADDER.includes(m.id))
const inForceReaches = (): boolean => LADDER.includes(mode)

/* Rising bars, one more per rung, so the three read as one control's three
   levels rather than three unrelated pictures -- the same reasoning `perm.ts`
   applies to its shields. A ladder, because that is what a tier is: `medium`,
   `high` and `max` differ in degree, and a metaphor with three unrelated
   symbols would hide the one thing worth seeing at a glance.

   Keyed by the built-in ids. A deployment that replaces the catalogue names its
   own rungs, which this cannot rank, so those draw the neutral glyph rather than
   borrowing a level they were never assigned. */
const ICO: Record<string, string> = {
  medium: '<path d="M6 18.5v-4"/>',
  high: '<path d="M6 18.5v-4"/><path d="M12 18.5v-9"/>',
  max: '<path d="M6 18.5v-4"/><path d="M12 18.5v-9"/><path d="M18 18.5v-14"/>',
}
const FALLBACK = '<circle cx="12" cy="12" r="7.4"/>'

/** The tick, which wears its stroke on the element rather than on the sheet. */
export const CHECK = 'M5 12.5 10 17.5 19 7'

function label(id: string): string {
  const found = menu.find((m) => m.id === id)
  /* The catalogue's own name, because a deployment that replaces it names its
     own rungs and this control is not entitled to rename them. Capitalised id
     only when it sent none. */
  return found?.name || (id ? id.charAt(0).toUpperCase() + id.slice(1) : '')
}

/* The chip, painted by hand over what <TierChip/> renders, for the reason
   perm.ts's draw gives: four writes the component renders no value for, so
   React never writes any of them again.

   Hidden until the first reply, and hidden again for a build with no catalogue.
   A chip that draws before the answer arrives has to invent a tier to show, and
   the one it would invent -- the ladder's default -- is exactly the value a
   deployment with its own catalogue does not use. */
export function draw(): void {
  const chip = el('tierChip')
  if (!chip) return
  const off = !loaded || !menu.length
  chip.hidden = off
  if (off) return
  const name = el('tierName')
  if (name) name.textContent = label(mode)
  const pic = chip.querySelector('.pico')
  if (pic) pic.innerHTML = ICO[mode] || FALLBACK
  chip.setAttribute('aria-label', `${t(ranked() ? 'gui.tier.title' : 'gui.tier.mode')}: ${label(mode)}`)
}

/* The panel's heading and footer, written on open because both depend on the
   catalogue that answered. Neither can be rendered from a key: <TierPop/> is
   built before anything is asked, and a key there would have the lang store
   paint the tier wording back over a mode catalogue's. Written by hand for the
   same reason the chip is -- the component renders no value for either, so a
   re-render leaves both standing. */
function headings(): void {
  const lab = document.querySelector<HTMLElement>('#tierPop .hd .lab')
  const note = document.querySelector<HTMLElement>('#tierPop .note')
  const tier = ranked()
  if (lab) lab.textContent = t(tier ? 'gui.tier.title' : 'gui.tier.mode')
  /* Keyed to the rung in force, not to `tier`: see `inForceReaches`. */
  if (note) note.textContent = t(inForceReaches() ? 'gui.tier.scope' : 'gui.tier.mode_scope')
}

function absorb(reply: TierReply | null | undefined): void {
  if (!reply) return
  const offered = Array.isArray(reply.availableModes) ? reply.availableModes : []
  const was = mode
  menu = offered.filter((m) => m && typeof m.id === 'string' && m.id)
  mode = typeof reply.mode === 'string' ? reply.mode : ''
  loaded = true
  draw()
  /* Only on a real move. Every read passes through here, including the one on
     each conversation change, and telling listeners the value they already hold
     has changed would have them re-fetch for nothing. */
  if (mode !== was) told()
}

/* Asked on every conversation change, including the change to none: a draft is
   a conversation being written, and the tier its first turn will dispatch at is
   worth showing before that turn is sent. What a draft has no answer for is
   WHERE a switch is written, and that is the seam's problem rather than this
   module's -- see the live source, which stages one the way it already stages a
   model picked before the session exists.

   A failure hides the chip rather than drawing a control whose state is unknown.
   There is nothing useful to show, and a chip that names a tier the next turn
   will not run at is worse than no chip. */
export async function load(): Promise<void> {
  try {
    absorb(await ds<TierSource>('tier').read())
  } catch {
    loaded = false
    draw()
  }
}

/* A second line under the name, and on the title too for a description too
   long for the row.

   This was hover-only until the base changed under it. The built-in
   descriptions used to read "Sub-agents run at their <tier> tier. Raven's own
   effort is the same in every mode." -- the first half restating the name and
   the second identical on every row, which the footer already makes once, and
   neither translated. They now say only what differs between the rungs
   (`_TIER_TEXTS`), and raven translates the ones it owns, so the reason for
   hiding them is gone and what is left is worth reading. A deployment's own
   descriptions are its author's and come through untouched, in whatever
   language they wrote. */
const rows = (): readonly TierRow[] =>
  menu.map((m) => ({
    id: m.id,
    name: label(m.id),
    ...(m.description ? { sub: m.description } : {}),
    ico: ICO[m.id] || FALLBACK,
    ticked: m.id === mode,
  }))

export function open(): void {
  const pop = el('tierPop')
  const chip = el('tierChip')
  if (!pop || !chip) return
  headings()
  /* Positioned off the chip, raised clear of the card, and moved to the body --
     all three for the reasons `perm.ts` records next door, its open included. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  put({ open: true, listed: rows(), opened: panel.opened + 1 })
}

export function close(): void {
  put({ ...panel, open: false })
}

export const isOpen = (): boolean => panel.open

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* A row's click. The panel goes first, and a row that is already in force asks
   for nothing. */
export function pick(id: string): void {
  close()
  if (id === mode) return
  void choose(id)
}

/* The reply decides, not the click. The handler refuses a tier this build does
   not offer, and painting the row first would leave the chip showing a tier the
   next turn will not run at. */
async function choose(id: string): Promise<void> {
  try {
    absorb(await ds<TierSource>('tier').set(id))
  } catch (err) {
    toast(t('gui.tier.failed', { name: label(id) }))
    /* Nothing moved -- `mode` still holds what the server last told us -- but
       the panel may be redrawn from it, so put the chip back in step. */
    draw()
    void err
  }
}

/* Test seam: the module's state outlives a test file's DOM. */
/* Test seam: the module's state outlives a test file's DOM. Watchers are NOT
   cleared, for the reason `state/sheetRack` gives -- one is registered when its
   own module loads, which happens once per test file, so clearing them would
   unwire the first reset and leave every case after it listening to nothing. */
/* Test seam for a listener's side of the contract: the tier moving without a
   reply to absorb, which is what a caller in another module has to react to. */
export function _notifyForTests(next: string): void {
  mode = next
  told()
}

export function _resetForTests(): void {
  mode = ''
  menu = []
  loaded = false
  put(shut)
}
