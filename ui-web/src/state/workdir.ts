/* The working directory a conversation runs in: the chip on the composer
 * (#wdChip) and the popover it opens (#wdPop).
 *
 * A store rather than a writer, the way perm.ts and tier.ts next door are:
 * <WorkdirChip/> and <WorkdirPopover/> render both nodes from it
 * (src/chrome/WorkdirChip.tsx, src/chrome/WorkdirPopover.tsx). Nothing here
 * reads the document; what stays here is what decides.
 *
 * Two states, and the reader meets them as one control. On a DRAFT the chip is
 * live: the reader picks a folder here, or leaves the default, and the pick is
 * held in this module until the first message mints a session -- the runtime's
 * promotion reads it off `staged()` and hands it to `session.create`, which is
 * the only moment a working directory can be set (raven/rpc/methods/session.py).
 * Held here rather than on the draft runtime because state/session/registry.ts
 * imports the rail's store and the rail's draw repaints this chip: a pick that
 * lived on the runtime would close that ring. IN a conversation the chip only
 * reports: the row's `workdir` names the folder, or the default, and the button
 * is disabled with the path on its title, because the engine cannot move a
 * conversation once it has started.
 *
 * "Default" is the word for no pick, on the chip and in the menu: the engine's
 * policy default is a real place to work, not an absence of one.
 *
 * The menu's recent folders come from the conversations on the rail, deduped
 * in the rail's own order (latest activity first); the browser walks the
 * gateway's `fs.dirs` through the workspace domain's source, and a folder the
 * engine would refuse (the agent's own data, or an ancestor of it) is shown
 * greyed and explained rather than hidden, because the folders under an
 * ancestor may be perfectly good workspaces.
 */

import { t } from '../i18n/t'
import { current as sessionCurrent } from '../lib/session'
import { rows as sessionRows, sess } from './session/rows'
import { ds } from './sources'
import { makeStore } from './store'

import type { DirListing } from '../features/workspace/types'

/** How many recent folders the menu offers. */
const RECENT_MAX = 5

/** One row of the menu, as the open that built it read the catalogue. */
export interface WdRow {
  readonly kind: 'none' | 'recent'
  /** The folder, or null for the default. */
  readonly path: string | null
  readonly name: string
  readonly sub: string
  readonly ticked: boolean
}

/* The chip's own values, as the last draw read the page: the word on it, the
   sentence on its title, whether a folder (rather than the default) is named,
   and whether the reader may still change it. Null until the first draw. */
export interface WdPaint {
  readonly label: string
  readonly title: string
  readonly set: boolean
  readonly locked: boolean
}

export interface WdState {
  /** Up or down: the popover's data-open and the chip's aria-expanded. */
  readonly open: boolean
  /** Bumped by every open, so a popover opened twice is measured twice. */
  readonly opened: number
  /** The menu, or the folder browser the menu's last row opens. */
  readonly view: 'menu' | 'browse'
  /** The menu's rows, built on open; null while it has never been opened. */
  readonly listed: readonly WdRow[] | null
  /** The directory the browser stands in, or null before the first answer. */
  readonly listing: DirListing | null
  /** A listing is on its way. */
  readonly loading: boolean
  /** Why the browser could not open, said on the menu; null when it could. */
  readonly err: string | null
  /** What the chip shows, or null while nothing has drawn it yet. */
  readonly paint: WdPaint | null
}

const shut: WdState = {
  open: false, opened: 0, view: 'menu', listed: null, listing: null, loading: false, err: null, paint: null,
}
const store = makeStore<WdState>(shut)

/** The store, for <WorkdirChip/> and <WorkdirPopover/>. */
export const { get, subscribe } = store

const samePaint = (a: WdPaint | null, b: WdPaint | null): boolean => {
  if (a === b) return true
  if (!a || !b) return false
  return a.label === b.label && a.title === b.title && a.set === b.set && a.locked === b.locked
}

/* Committed synchronously, for the reason perm.ts's set gives: `open` is
   measured by the popover it has just filled. Field by field on the paint,
   because `draw` builds a fresh one on every rail draw. */
export function set(next: WdState): void {
  const now = get()
  if (next.open === now.open && next.opened === now.opened && next.view === now.view
    && next.listed === now.listed && next.listing === now.listing && next.loading === now.loading
    && next.err === now.err && samePaint(next.paint, now.paint)) return
  store.set(next)
}

/* ---- the draft's pick ---------------------------------------------------- */

/* The folder the draft chose, or null for the default. Consumed by the
   promotion (state/session/runtime.ts) and dropped with the draft
   (state/session/registry.ts's switchToDraft), so a pick never crosses from
   one conversation to the next. */
let picked: string | null = null

/** The draft's pick, for the promotion to hand to `session.create`. */
export const staged = (): string | null => picked

/** The pick is spent or the draft is gone. */
export function clearStaged(): void {
  picked = null
  draw()
}

/** The last segment of a path, on either separator; the root is itself. */
export function base(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/* ---- the chip -------------------------------------------------------------- */

/* The chip, as the values <WorkdirChip/> renders. Called by the rail's own
   draw (features/rail/store.ts), which runs on every change of the list and of
   the conversation on screen -- the two things this reads -- and by the picks
   below. Nothing here reads the document. */
export function draw(): void {
  const cur = sessionCurrent()
  let paint: WdPaint
  if (cur === null) {
    paint = picked
      ? { label: base(picked), title: picked, set: true, locked: false }
      : { label: t('gui.wd.none'), title: t('gui.wd.none_h'), set: false, locked: false }
  } else {
    /* Guarded like `recents` below: the seam the rows come through is installed
       by the boot, and a draw that runs ahead of it (or in a case that never
       installs one) has the default to report rather than a throw to make. */
    let dir: string | null = null
    try {
      dir = sess(cur)?.workdir || null
    } catch {
      dir = null
    }
    paint = {
      label: dir ? base(dir) : t('gui.wd.none'),
      title: `${dir || t('gui.wd.none_h')}\n${t('gui.wd.locked')}`,
      set: !!dir,
      locked: true,
    }
  }
  set({ ...get(), paint })
}

/* ---- the menu ---------------------------------------------------------------- */

/** The folders the rail's conversations were pinned to, latest first, each once. */
export function recents(): string[] {
  const out: string[] = []
  let rows: ReadonlyArray<{ workdir?: string | null }> = []
  try {
    rows = sessionRows()
  } catch {
    rows = []
  }
  for (const row of rows) {
    const dir = row.workdir
    if (dir && !out.includes(dir)) out.push(dir)
    if (out.length >= RECENT_MAX) break
  }
  return out
}

/* The default, then the folders: the one picked through the browser first when
   no conversation has run there yet -- a pick with no row to show it on would
   otherwise leave the menu with nothing ticked -- then the recent ones. */
const menuRows = (): readonly WdRow[] => {
  const dirs = recents()
  if (picked && !dirs.includes(picked)) dirs.unshift(picked)
  return [
    { kind: 'none', path: null, name: t('gui.wd.none'), sub: t('gui.wd.none_h'), ticked: picked === null },
    ...dirs.map((dir): WdRow => ({ kind: 'recent', path: dir, name: base(dir), sub: dir, ticked: dir === picked })),
  ]
}

/* Open only while the pick can still change: a conversation's chip is disabled,
   and a click that reached here anyway must not raise a menu over it. */
export function open(): void {
  if (get().paint?.locked) return
  set({ ...get(), open: true, view: 'menu', listed: menuRows(), err: null, opened: get().opened + 1 })
}

export function close(): void {
  set({ ...get(), open: false })
}

export const isOpen = (): boolean => get().open

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/** A row's click: the folder, or null for the default. */
export function pick(path: string | null): void {
  picked = path
  close()
  draw()
}

/* ---- the folder browser ---------------------------------------------------- */

type Dirs = (path?: string) => Promise<DirListing>

/* The workspace domain's `dirs`, or null on a page with nothing to browse: the
   seam is not installed, or the source installed has no gateway behind it. */
function dirs(): Dirs | null {
  try {
    const fn = ds('workspace').dirs
    return fn ? (path?: string) => fn(path) : null
  } catch {
    return null
  }
}

const detailOf = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string }
  return (o && o.data && o.data.detail) || (o && o.message) || String(e)
}

/* Read one directory and stand the browser in it. A refusal is said on the
   view the reader is looking at -- the menu before the first listing, the
   browser once it stands somewhere -- and replaces nothing. Answers that land
   after the popover closed, or after a later ask, are dropped. */
let asking = 0

export async function browse(path?: string): Promise<void> {
  const fn = dirs()
  if (!fn) {
    set({ ...get(), err: t('gui.wd.not_live') })
    return
  }
  const ticket = ++asking
  set({ ...get(), loading: true, err: null })
  try {
    const listing = await fn(path)
    if (ticket !== asking || !get().open) return
    set({ ...get(), view: 'browse', listing, loading: false, err: null })
  } catch (e) {
    if (ticket !== asking || !get().open) return
    set({ ...get(), loading: false, err: t('gui.wd.failed', { detail: detailOf(e) }) })
  }
}

/** Back from the browser to the menu, rebuilt: a pick may have been made. */
export function back(): void {
  asking += 1
  set({ ...get(), view: 'menu', listed: menuRows(), loading: false, err: null })
}

/** The browser's "use this folder", offered only where the engine would take it. */
export function useHere(): void {
  const here = get().listing
  if (!here || !here.ok) return
  pick(here.path)
}

/* Test seam only: the pick, the ticket and the popover are the module's. */
export function _resetForTests(): void {
  picked = null
  asking = 0
  set(shut)
}
