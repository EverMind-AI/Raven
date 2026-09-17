/* The permission mode: the chip on the composer (#permChip) and the panel it
 * opens (#permPop).
 *
 * A store rather than a writer, now that <PermChip/> and <PermPop/> render both
 * from it (src/chrome/PermChip.tsx, src/chrome/PermPop.tsx). What stays here is
 * the state and everything that decides: the three tiers, the mode in force,
 * where a pick is written, and the rows a panel shows when it opens. The chip
 * itself is still painted by hand -- see `draw`.
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

import { flushSync } from 'react-dom'

import { t } from './bridge'

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

/* Shields, one per tier, differing only in what is inside them: a question, a
   check, an exclamation. Same outline so the three read as one control's three
   states rather than three unrelated icons. */
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

/** One row of the panel, as the open that built it read the catalogue. */
export interface PermRow {
  readonly id: string
  readonly name: string
  readonly sub: string
  readonly risk: boolean
  /** The shield's paths, as markup, because that is how ICO spells them. */
  readonly ico: string
  readonly ticked: boolean
}

export interface PermPanel {
  /** Up or down: the panel's data-open and the chip's aria-expanded. */
  readonly open: boolean
  /* The rows the last open built, or null while the panel has never been
     opened. Not cleared when it closes, because closing only hid the panel
     before and may not start emptying it -- so what is ticked here is the mode
     as of the last open rather than the mode in force, and the words are the
     catalogue as of the last open rather than the language in force. */
  readonly listed: readonly PermRow[] | null
  /** Bumped by every open, so a panel opened twice is measured twice. */
  readonly opened: number
}

const shut: PermPanel = { open: false, listed: null, opened: 0 }
let panel: PermPanel = shut
const listeners = new Set<() => void>()

/** The panel's state, for <PermPop/> and <PermChip/>. */
export function get(): PermPanel {
  return panel
}

/** For useSyncExternalStore: called whenever the panel changes. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* Committed synchronously, the way the writes by id were: `open` measures the
   panel it has just filled, and a caller that opens and then reads the DOM --
   the chip's own toggle, the pointerdown that closes it, every case in
   perm.test.ts -- has to see it. */
function put(next: PermPanel): void {
  if (next.open === panel.open && next.listed === panel.listed && next.opened === panel.opened) return
  panel = next
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
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
  return TIERS.some((p) => p.id === stored) ? stored : 'ask'
}

function commit(value: string): void {
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

/* The mode the engine actually holds, pushed in by the live layer once the
   config has loaded (and again whenever another surface changes it). */
/* How a pick reaches the config, when anything can write one. Registered by
   ui-web/src/legacy/live/120-settings.js, which owns the settings transport;
   null on the offline shell, where the pick commits locally. */
let persist: ((mode: string) => Promise<boolean> | boolean) | null = null

export function setPermPersister(fn: (mode: string) => Promise<boolean> | boolean): void {
  persist = fn
}

export function setFromConfig(value: string): void {
  if (!TIERS.some((p) => p.id === value) || value === mode) return
  commit(value)
}

export const current = (): string => mode

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* The chip, painted by hand over what <PermChip/> renders: the icon is markup
   ICO carries as a string, and the label and the accessible name are one write
   each that React would only ever render the served literal of. Safe because
   the component renders no value for any of the three -- React diffs against
   the props it rendered last, so a prop it never changes is never written
   again.

   The chip reads its own state, which is why it carries no hover label: the
   detail of each tier belongs in the panel the click opens. Nothing to remove
   for that -- the chip is rendered with no data-tip and no data-i18n-tip for
   the lang store to fill, and the legacy drawPerm's `delete chip.dataset.tip`
   was dead there too. It did not come across. */
export function draw(): void {
  const cur = TIERS.find((p) => p.id === mode) || TIERS[0]!
  const name = el('permName')
  if (name) name.textContent = t(cur.label)
  const chip = el('permChip')
  if (!chip) return
  chip.classList.toggle('risk', !!cur.risk)
  const pic = chip.querySelector('.pico')
  if (pic) pic.innerHTML = ICO[cur.id] || ICO.full!
  chip.setAttribute('aria-label', `${t('gui.perm.title')}: ${t(cur.label)}`)
}

const rows = (): readonly PermRow[] =>
  TIERS.map((p) => ({
    id: p.id,
    name: t(p.label),
    sub: t(p.sub),
    risk: !!p.risk,
    ico: ICO[p.id] ?? '',
    ticked: p.id === mode,
  }))

export function open(): void {
  const pop = el('permPop')
  const chip = el('permChip')
  if (!pop || !chip) return
  /* Out of the card first, and once only: the card's entrance animation makes
     it a containing block, which quietly re-bases the panel's position: fixed
     against the card instead of the viewport. Moving a node React rendered is
     safe because none of the card's children is conditional, so React never
     reconciles that child list and never puts it back (src/chrome/Dock.tsx).
     Before the rows and the flag, because the placement <PermPop/> makes in
     its layout effect measures the panel where it now stands. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  put({ open: true, listed: rows(), opened: panel.opened + 1 })
}

export function close(): void {
  put({ ...panel, open: false })
}

export const isOpen = (): boolean => panel.open

/* The chip toggles rather than opens: it is the only way back out of the panel
   with the pointer, since the panel has no close button of its own. */
export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* A row's click. The panel goes first, whatever the write does next.

   Registered by the live layer, which persists to config (the gate reads it
   live); the offline shell registers nothing and the pick commits locally. The
   chip commits only on acknowledgement -- painting the new mode while the write
   failed would show `ask` over a gate still running `full`, a false security
   state, so a rejected write leaves the chip on the mode the engine actually
   holds. */
export function pick(id: string): void {
  close()
  if (!persist) {
    commit(id)
    return
  }
  Promise.resolve(persist(id)).then(
    (ok) => { if (ok) commit(id) },
    () => {},
  )
}

/* Test seam only: the mode, the panel and the registered writer are the
   module's now, so they outlive a case's DOM. The subscribers are left alone --
   a mounted root owns its own, and React takes them back when it unmounts. */
export function _resetForTests(): void {
  mode = read()
  persist = null
  put(shut)
}
