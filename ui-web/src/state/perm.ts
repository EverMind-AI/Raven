/* The permission mode: the chip on the composer (#permChip) and the popover it
 * opens (#permPop).
 *
 * A store rather than a writer: <PermChip/> and <PermPopover/> render both from it
 * (src/chrome/PermChip.tsx, src/chrome/PermPopover.tsx). What stays here is the
 * state and everything that decides: the three tiers, the mode in force, where
 * a pick is written, the rows a popover shows when it opens, and the chip's own
 * four values -- `draw` fills those rather than writing them by id.
 *
 * Three tiers, ordered from the strictest to the one with no brakes, because
 * that is the order a reader should meet them in. The engine's permission gate
 * reads the choice live from config, so a pick here holds from the next tool
 * call. Full access is the one tier drawn in the warning colour, since that is
 * a fact about it, not decoration.
 *
 * The config file is the choice's home; localStorage only remembers the last
 * known value so the chip paints right before the live layer has loaded the
 * config. The live layer pushes the loaded value in through setFromConfig and
 * registers how a pick is written through setPermPersister -- registered
 * rather than imported so the demo layer, which has no engine, simply leaves
 * nobody listening.
 */

import { t } from '../i18n/t'
import { makeStore } from './store'

export interface Tier {
  id: string
  label: string
  sub: string
  risk?: boolean
}

export const TIERS: readonly Tier[] = [
  { id: 'ask', label: 'gui.perm.ask', sub: 'gui.perm.ask_h' },
  { id: 'smart', label: 'gui.perm.smart', sub: 'gui.perm.smart_h' },
  { id: 'full', label: 'gui.perm.full', sub: 'gui.perm.full_h', risk: true },
]

const KEY = 'raven.perm'

/* What the chip shows before the config has loaded, and the only tier the page
   can name on its own. It is the engine's default (raven/config/schema.py,
   PermissionsConfig.mode) written twice: the page paints before the config
   arrives, and painting a tier the engine is not in reads as a mode flipping
   under the reader. */
const DEFAULT_TIER = 'smart'

/* Shields, one per tier, differing only in what is inside them: a question, a
   check, an exclamation. Same outline so the three read as one control's three
   states rather than three unrelated icons.

   The chip wears one of these; the popover's rows do not. The visual reference
   draws the tier list as name-and-sentence alone, and a shield repeated down a
   list of three says nothing the words beside it do not. */
const ICO: Record<string, string> = {
  ask:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M9.6 10.2a2.4 2.4 0 1 1 3.3 2.2v1.1"/><path d="M12 16h.01"/>',
  smart:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M9 12.2l2 2 4-4.4"/>',
  full:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M12 8.6v3.6M12 15.2h.01"/>',
}

/** The tick, which wears its stroke on the element rather than on the sheet. */
export const CHECK = 'M5 12.5l4.5 4.5L19 7'

/** One row of the popover, as the open that built it read the catalogue. */
export interface PermRow {
  readonly id: string
  readonly name: string
  readonly sub: string
  readonly risk: boolean
  readonly ticked: boolean
}

/* The chip's own four values, as the last draw read the catalogue: the tier's
   name, whether it is the one in the warning colour, its shield and the
   accessible name. Null until the first draw, and then <PermChip/> shows the
   word the page was served with. */
export interface PermPaint {
  readonly label: string
  readonly risk: boolean
  /** The shield's paths, as markup, because that is how ICO spells them. */
  readonly ico: string
  readonly aria: string
}

export interface PermPopoverState {
  /** Up or down: the popover's data-open and the chip's aria-expanded. */
  readonly open: boolean
  /* The rows the last open built, or null while the popover has never been
     opened. Not cleared when it closes, because closing only hid the popover
     before and may not start emptying it -- so what is ticked here is the mode
     as of the last open rather than the mode in force, and the words are the
     catalogue as of the last open rather than the language in force. */
  readonly listed: readonly PermRow[] | null
  /** Bumped by every open, so a popover opened twice is measured twice. */
  readonly opened: number
  /** What the chip shows, or null while nothing has drawn it yet. */
  readonly paint: PermPaint | null
}

const shut: PermPopoverState = { open: false, listed: null, opened: 0, paint: null }
const store = makeStore<PermPopoverState>(shut)

/** The popover's state, for <PermPopover/> and <PermChip/>. */
export const { get, subscribe } = store

/* Field by field, not by reference: `draw` builds a fresh paint on every call,
   and comparing the two objects would make each call a change -- while a paint
   compared by reference alone would also let a language flip re-render the chip
   into the same words for nothing. */
const samePaint = (a: PermPaint | null, b: PermPaint | null): boolean => {
  if (a === b) return true
  if (!a || !b) return false
  return a.label === b.label && a.risk === b.risk && a.ico === b.ico && a.aria === b.aria
}

/* Committed synchronously, the way the writes by id were: `open` measures the
   popover it has just filled, and a caller that opens and then reads the DOM --
   the chip's own toggle, the pointerdown that closes it, every case in
   perm.test.ts -- has to see it. */
export function set(next: PermPopoverState): void {
  const now = get()
  if (next.open === now.open && next.listed === now.listed && next.opened === now.opened
    && samePaint(next.paint, now.paint)) return
  store.set(next)
}

/* Read once, and validated: a stored id from an older build that no longer
   names a tier would leave the chip drawing nothing. Private mode throws on
   read, which is a reason to fall back rather than a reason to fail. */
let mode = read()

function read(): string {
  let stored = ''
  try {
    stored = localStorage.getItem(KEY) || ''
  } catch {
    stored = ''
  }
  return TIERS.some((p) => p.id === stored) ? stored : DEFAULT_TIER
}

function remember(value: string): void {
  mode = value
  /* Private mode throws on write. Losing the paint cache is the whole cost,
     and it is not worth taking the commit down with it. */
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    /* nothing to do about it */
  }
  draw()
}

/* The mode the engine actually holds, pushed in once the config has loaded
   (and again whenever another surface changes it). */
/* How a pick reaches the config, when anything can write one. Registered by
   src/features/settings/wire.ts, which owns the settings transport; null on
   the offline shell, where the pick commits locally. */
let persist: ((mode: string) => Promise<boolean> | boolean) | null = null

export function setPermPersister(fn: (mode: string) => Promise<boolean> | boolean): void {
  persist = fn
}

export function setFromConfig(value: string): void {
  if (!TIERS.some((p) => p.id === value) || value === mode) return
  remember(value)
}

export const current = (): string => mode

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* The chip, as four values <PermChip/> renders. The name is `draw` and so are
   its two call sites (the boot's ordered list and the language repaint), which
   is what the two ordered-list gates assert by literal; what changed is that
   the four land in the store instead of being written by id.

   Nothing here reads the document, so there is nothing to guard: a page whose
   markup has gone still has a React tree to commit into (measured), and the
   popover's own open is what still needs the two nodes to exist.

   The chip reads its own state, which is why it carries no hover label: the
   detail of each tier belongs in the popover the click opens. Nothing to remove
   for that -- the chip is rendered with no data-tip for the lang store to
   fill, and the `delete chip.dataset.tip` the draw before this one carried was
   dead there too. It did not come across. */
export function draw(): void {
  const cur = TIERS.find((p) => p.id === mode) || TIERS[0]!
  set({
    ...get(),
    paint: {
      label: t(cur.label),
      risk: !!cur.risk,
      ico: ICO[cur.id] || ICO.full!,
      aria: `${t('gui.perm.title')}: ${t(cur.label)}`,
    },
  })
}

const rows = (): readonly PermRow[] =>
  TIERS.map((p) => ({
    id: p.id,
    name: t(p.label),
    sub: t(p.sub),
    risk: !!p.risk,
    ticked: p.id === mode,
  }))

export function open(): void {
  const pop = el('permPop')
  const chip = el('permChip')
  if (!pop || !chip) return
  /* Out of the card first, and once only: the card's entrance animation makes
     it a containing block, which quietly re-bases the popover's position: fixed
     against the card instead of the viewport. Moving a node React rendered is
     safe because none of the card's children is conditional, so React never
     reconciles that child list and never puts it back (src/chrome/Dock.tsx).
     Before the rows and the flag, because the placement <PermPopover/> makes in
     its layout effect measures the popover where it now stands. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  set({ ...get(), open: true, listed: rows(), opened: get().opened + 1 })
}

export function close(): void {
  set({ ...get(), open: false })
}

export const isOpen = (): boolean => get().open

/* The chip toggles rather than opens: it is the only way back out of the popover
   with the pointer, since the popover has no close button of its own. */
export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* A row's click. The popover goes first, whatever the write does next.

   Registered by the live layer, which persists to config (the gate reads it
   live); the offline shell registers nothing and the pick commits locally. The
   chip commits only on acknowledgement -- painting the new mode while the write
   failed would show `ask` over a gate still running `full`, a false security
   state, so a rejected write leaves the chip on the mode the engine actually
   holds. */
export function pick(id: string): void {
  close()
  if (!persist) {
    remember(id)
    return
  }
  Promise.resolve(persist(id)).then(
    (ok) => { if (ok) remember(id) },
    () => {},
  )
}

/* Test seam only: the mode, the popover and the registered writer are the
   module's now, so they outlive a case's DOM. The subscribers are left alone --
   a mounted root owns its own, and React takes them back when it unmounts. */
export function _resetForTests(): void {
  mode = read()
  persist = null
  set(shut)
}
