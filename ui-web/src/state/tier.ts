/* The sub-agent effort tier: the row of the model picker that offers it
 * (features/model/ModelPicker.tsx, its TierRow), and everything that decides
 * behind that row.
 *
 * A store rather than a writer, for the reasons `perm.ts` gives next door:
 * the picker's tier row renders from it -- `offered`, `options`, `label`,
 * `isRanked`, `scopeNote` -- and the chip paint and the popover's two headings
 * included. The popover is still moved to the body by hand, because that is the
 * only way it can be positioned at all; what stays here is what decides. The
 * two chips sit side by side and are built the same way on purpose -- a reader
 * meets them as one row of session settings, not as two unrelated controls.
 *
 * What it is NOT is a model. What it IS depends on the catalogue, and the popover
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

import { t } from '../i18n/t'
import { ds } from './sources'
import { makeStore } from './store'
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

/** One row of the popover, as the open that built it read the catalogue. */
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

/* The chip's paint, kept for the two callers held to `draw` by literal. `off` is the whole of it for a chip with
   nothing honest to show -- no reply yet, or a build with no catalogue -- and
   the three words are not carried at all there, because the hidden chip goes on
   showing the shape the page was served with rather than a tier this side
   invented. */
export type TierPaint =
  | { readonly off: true }
  | {
    readonly off: false
    readonly label: string
    /** The bars, as markup, because that is how ICO spells them. */
    readonly ico: string
    readonly aria: string
  }

/** The popover's heading and footer, as the open that built them read them. */
export interface TierHeadings {
  readonly lab: string
  readonly note: string
}

export interface TierPopoverState {
  /** Up or down: the popover's data-open and the chip's aria-expanded. */
  readonly open: boolean
  /* The rows the last open built, or null while the popover has never been
     opened. Not cleared when it closes, because closing only hid the popover
     before and may not start emptying it -- so the catalogue here is the one
     that answered before the last open, not the one in force. */
  readonly listed: readonly TierRow[] | null
  /** Bumped by every open, so a popover opened twice is measured twice. */
  readonly opened: number
  /** What the chip shows, or null while nothing has drawn it yet. */
  readonly paint: TierPaint | null
  /* The words the last open chose, or null while the popover has never been
     opened -- and then the two nodes stand empty, which is how the page is
     served with them. */
  readonly head: TierHeadings | null
}

const shut: TierPopoverState = { open: false, listed: null, opened: 0, paint: null, head: null }
const store = makeStore<TierPopoverState>(shut)

/** The store's state: the paint, and the popover fields the tier row no longer draws. */
export const { get, subscribe } = store

/* Field by field, for the reason perm.ts's samePaint gives: a fresh object per
   draw would otherwise be a change every time, and a comparison by reference
   alone would let a reply that moved nothing re-render the chip. */
const samePaint = (a: TierPaint | null, b: TierPaint | null): boolean => {
  if (a === b) return true
  if (!a || !b || a.off !== b.off) return false
  if (a.off || b.off) return true
  return a.label === b.label && a.ico === b.ico && a.aria === b.aria
}

const sameHead = (a: TierHeadings | null, b: TierHeadings | null): boolean => {
  if (a === b) return true
  if (!a || !b) return false
  return a.lab === b.lab && a.note === b.note
}

/* Committed synchronously, for the reason perm.ts's put gives: `open` measures
   the popover it has just filled. */
export function set(next: TierPopoverState): void {
  const now = get()
  if (next.open === now.open && next.listed === now.listed && next.opened === now.opened
    && samePaint(next.paint, now.paint) && sameHead(next.head, now.head)) return
  store.set(next)
}

/* Who else the tier moves. It is a session-wide setting, and a surface showing
   what one sub-agent will run at is showing this value clamped -- so it has to
   hear about a switch, or it goes on naming the rung from before.

   A notification rather than a call into whoever cares: this module has no
   business knowing that an instance pane exists. It says the value moved; what
   that is worth is the listener's to decide. */
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

/* The built-in rungs' names, in the reader's language. The server names them
   by capitalising the id (raven/config/schema.py, _builtin_modes), which is
   English whatever the page speaks; these are the same three words looked up. */
const NAME: Record<string, string> = { medium: 'gui.tier.medium', high: 'gui.tier.high', max: 'gui.tier.max' }

export function label(id: string): string {
  const key = NAME[id]
  if (key && LADDER.includes(id)) return t(key)
  const found = menu.find((m) => m.id === id)
  /* The catalogue's own name, because a deployment that replaces it names its
     own rungs and this control is not entitled to rename them. Capitalised id
     only when it sent none. */
  return found?.name || (id ? id.charAt(0).toUpperCase() + id.slice(1) : '')
}

/** Whether there is a catalogue to draw: a reply landed and it offered rungs. */
export const offered = (): boolean => loaded && menu.length > 0

/** Every rung on offer, as the picker's tier row draws them. */
export const options = (): readonly TierRow[] => rows()

/** Whether every rung on offer is one of the built-in ladder (a Session Tier). */
export const isRanked = (): boolean => ranked()

/** Where the rung in force goes, as one sentence for the row that draws it. */
export const scopeNote = (): string => t(inForceReaches() ? 'gui.tier.scope' : 'gui.tier.mode_scope')

/* The paint, as four values, for the reason perm.ts's
   draw gives. Nothing here reads the document.

   Hidden until the first reply, and hidden again for a build with no catalogue.
   A chip that draws before the answer arrives has to invent a tier to show, and
   the one it would invent -- the ladder's default -- is exactly the value a
   deployment with its own catalogue does not use. That is why `off` carries no
   words: there is nothing honest to put in them, and a hidden chip left showing
   the page's own literal is what this did when it returned early. */
export function draw(): void {
  if (!loaded || !menu.length) {
    set({ ...get(), paint: { off: true } })
    return
  }
  set({
    ...get(),
    paint: {
      off: false,
      label: label(mode),
      ico: ICO[mode] || FALLBACK,
      aria: `${t(ranked() ? 'gui.tier.title' : 'gui.tier.mode')}: ${label(mode)}`,
    },
  })
}

/* The popover's heading and footer, chosen on open because both depend on the
   catalogue that answered, and then held in the store until the next one.
   Neither can be rendered from a key: the row is built before anything is
   asked, and a key there would have the lang store paint the tier wording back
   over a mode catalogue's. */
const headings = (): TierHeadings => ({
  lab: t(ranked() ? 'gui.tier.title' : 'gui.tier.mode'),
  /* Keyed to the rung in force, not to the catalogue: see `inForceReaches`. */
  note: t(inForceReaches() ? 'gui.tier.scope' : 'gui.tier.mode_scope'),
})

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
    absorb(await ds('tier').read())
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
  set({ ...get(), open: true, listed: rows(), opened: get().opened + 1, head: headings() })
}

export function close(): void {
  set({ ...get(), open: false })
}

export const isOpen = (): boolean => get().open

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* A row's click. The popover goes first, and a row that is already in force asks
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
    absorb(await ds('tier').set(id))
  } catch (err) {
    toast(t('gui.tier.failed', { name: label(id) }))
    /* Nothing moved -- `mode` still holds what the server last told us -- but
       the popover may be redrawn from it, so put the chip back in step. */
    draw()
    void err
  }
}

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
  set(shut)
}
