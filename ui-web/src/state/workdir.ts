/* The working directory a conversation runs in: the popover it is chosen from
 * (#wdPop), raised by the "@" menu's Folder row, and the tag beside a
 * conversation's title (#wdTag).
 *
 * A store rather than a writer, the way perm.ts and tier.ts next door are:
 * <WorkdirPopover/> and <WorkdirTag/> render from it
 * (src/chrome/WorkdirPopover.tsx,
 * src/chrome/WorkdirTag.tsx). Nothing here reads the document; what stays here
 * is what decides.
 *
 * Two states. On a DRAFT the row is live: the reader picks a folder here, or
 * leaves the default, and the pick is held in this module until the first
 * message mints a session -- the runtime's promotion reads it off `staged()`
 * and hands it to `session.create`, which is the only moment a working
 * directory can be set (raven/rpc/methods/session.py). Held here rather than
 * on the draft runtime because state/session/registry.ts imports the rail's
 * store and the rail's draw repaints this row: a pick that lived on the
 * runtime would close that ring. IN a conversation the row is gone and the
 * tag beside the title reports instead: the row's `workdir` names the folder,
 * with the path on its title, because the engine cannot move a conversation
 * once it has started -- and the default is nothing to announce.
 *
 * "Default" is the word for no pick, on the row and in the menu: the engine's
 * policy default is a real place to work, not an absence of one.
 *
 * The menu's recent folders come from the conversations on the rail, deduped
 * in the rail's own order (latest activity first). Its last row is one of two
 * things: on the reader's own desktop, where that desktop has one, it opens the
 * host's folder dialog (`fs.pick_dir`), because a person choosing a folder
 * already has Finder for that; anywhere else it opens the in-page browser,
 * which walks the gateway's `fs.dirs` through the workspace domain's source,
 * and a folder the engine would refuse (the agent's own data, or an ancestor
 * of it) is shown greyed and explained rather than hidden, because the folders
 * under an ancestor may be perfectly good working directories.
 */

import { t } from '../i18n/t'
import { current as sessionCurrent } from '../lib/session'
import { servesFolderDialog } from '../rpc/capabilities'
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

/* The row's own values, as the last draw read the page: the word on it, the
   sentence on its title, whether a folder (rather than the default) is named,
   and whether the reader may still change it. Null until the first draw. */
export interface WdPaint {
  readonly label: string
  readonly title: string
  readonly set: boolean
  readonly locked: boolean
}

export interface WdState {
  /** Up or down: the popover's data-open. */
  readonly open: boolean
  /** The host's folder dialog is up, and the menu is waiting on it. */
  readonly picking: boolean
  /** Bumped by every open, so a popover opened twice is measured twice. */
  readonly opened: number
  /** The menu, the folder browser the menu's last row opens, or the list of
   *  drives that a browser standing on a root cannot reach by walking. */
  readonly view: 'menu' | 'browse' | 'drives'
  /** The menu's rows, built on open; null while it has never been opened. */
  readonly listed: readonly WdRow[] | null
  /** The directory the browser stands in, or null before the first answer. */
  readonly listing: DirListing | null
  /** A listing is on its way. */
  readonly loading: boolean
  /** Bumped by every read the browser asks for, refused or not: the path
   *  bar holds what was typed into it, and a refusal leaves the browser where
   *  it stood, so the field is keyed on this to come back with that path. */
  readonly asked: number
  /** Why the browser could not open, said on the menu; null when it could. */
  readonly err: string | null
  /** What the "@" menu's row shows, or null while nothing has drawn it yet. */
  readonly paint: WdPaint | null
}

const shut: WdState = {
  open: false, picking: false, opened: 0, view: 'menu', listed: null, listing: null, loading: false, asked: 0,
  err: null, paint: null,
}
const store = makeStore<WdState>(shut)

/** The store, for <WorkdirPopover/>, <WorkdirTag/> and the "@" menu's row. */
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
  if (next.open === now.open && next.picking === now.picking && next.opened === now.opened
    && next.view === now.view && next.listed === now.listed && next.listing === now.listing
    && next.loading === now.loading && next.asked === now.asked && next.err === now.err
    && samePaint(next.paint, now.paint)) return
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

/* The pick is spent or the draft is gone -- and so is any menu that was open
   over it: a fresh draft starts with the menu closed. */
export function clearStaged(): void {
  picked = null
  set({ ...get(), open: false })
  draw()
}

/** The last segment of a path, on either separator; the root is itself. */
export function base(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/* ---- what the row and the tag say ----------------------------------------- */

/* The name and the tag, as the "@" menu's row and <WorkdirTag/> render them.
   Called by the rail's own draw (features/rail/store.ts), which runs on every
   change of the list and of the conversation on screen -- the two things this
   reads -- and by the picks below. Nothing here reads the document. */
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
      title: dir || t('gui.wd.none_h'),
      set: !!dir,
      locked: true,
    }
  }
  /* A conversation has no popover: the reader who opened the menu on a draft
     and then picked a conversation from the rail would otherwise be left with
     live rows over a folder the conversation cannot change, and a click on one
     would stage a pick for some later draft. The rail's draw runs on every
     switch, which is what makes this the moment. */
  set({ ...get(), paint, open: paint.locked ? false : get().open })
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

/* Open only while the pick can still change: a conversation's row is not
   offered, and a click that reached here anyway must not raise a menu over it.
   The way back out is a pointer landing outside (state/globalListeners.ts's
   click-away arbitration), Escape (state/escapeOrder.ts), a pick, or leaving
   the draft. */
export function open(): void {
  if (get().paint?.locked) return
  using = null
  set({ ...get(), open: true, view: 'menu', listed: menuRows(), err: null, opened: get().opened + 1 })
}

export function close(): void {
  using = null
  set({ ...get(), open: false })
}

/* The handshake can change the menu's last row without changing this store. */
export function redraw(): void {
  if (get().open) store.set({ ...get() })
}

/* Whether the popover's last row may be pressed: not while the dialog is up. */
export const isPicking = (): boolean => get().picking

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

/* ---- the host's folder dialog ------------------------------------------- */

const detailOf = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string }
  return (o && o.data && o.data.detail) || (o && o.message) || String(e)
}

/* One ticket for the dialog and the walk alike: an answer from either that
   lands after a later ask, a close or a switch is dropped. */
let asking = 0

/* One press of Use at a time: the read remounts the path bar with the folder
   the browser is leaving, so the second click of a double press would name
   that stale folder and stage it before the typed read lands. The ticket says
   whose press holds it, so a press that lost the browser cannot release the
   one that owns it. */
let using: number | null = null
let presses = 0

/* Whether the menu's last row opens the host's own folder dialog (Finder, or
   zenity/kdialog on a Linux desktop) rather than the in-page walk below: the
   gateway's host has one AND runs on this desktop, so the dialog opens where
   the reader is. A remote gateway would open it on somebody else's screen.
   Whether the host has one is the handshake's to say: a gateway in a container,
   or on a headless box reached through a forwarded port, is served at a
   loopback address too. A Windows gateway never announces one -- its dialog
   opens behind the browser (raven/rpc/folder_dialog.py) -- so the same few
   lines put the walk in front of a reader on their own Windows desktop. */
export function nativePick(): boolean {
  try {
    const src = ds('workspace')
    return !!src.pickDir && servesFolderDialog() && !!src.hostIsLocal?.()
  } catch {
    return false
  }
}

/* The menu's last row. The dialog where it is the reader's own desktop; the
   in-page walk where the gateway is elsewhere or offers no dialog. */
export function chooseFolder(): Promise<void> {
  return nativePick() ? pickNative() : browse()
}

/* Bring the menu back up to say something: the dialog is gone, the popover
   went down with the click that opened it, and a refusal with nowhere to be
   said would leave the reader with a folder they did not get and no word
   about it. The rows are rebuilt as an open builds them, so what comes back
   is the menu as it stands now, carrying the sentence. */
function sayInMenu(err: string): void {
  set({ ...get(), picking: false, open: true, view: 'menu', listed: menuRows(), err, opened: get().opened + 1 })
}

/* The menu goes down with the click and the desktop's own dialog comes up in
   its place: the dialog is modal and takes the reader with it, so a menu left
   standing behind it had nothing to say and only sat in the way. The page
   waits: a dismissal leaves the folder where it was, and a folder the create
   would refuse brings the menu back with the reason on it. Answers that land
   after the draft became a conversation are dropped -- the pick would be for
   some later draft nobody asked for. */
async function pickNative(): Promise<void> {
  const fn = ds('workspace').pickDir
  if (!fn || get().picking) return
  const ticket = ++asking
  set({ ...get(), picking: true, err: null, open: false })
  try {
    const r = await fn()
    if (ticket !== asking) return
    set({ ...get(), picking: false })
    if (get().paint?.locked) return
    if (!r.path) return
    if (!r.ok) {
      sayInMenu(t('gui.wd.blocked'))
      return
    }
    pick(r.path)
  } catch (e) {
    if (ticket !== asking) return
    set({ ...get(), picking: false })
    if (get().paint?.locked) return
    sayInMenu(t('gui.wd.failed', { detail: detailOf(e) }))
  }
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

/* Read one directory and stand the browser in it. A refusal is said on the
   view the reader is looking at -- the menu before the first listing, the
   browser once it stands somewhere -- and replaces nothing. Answers that land
   after the popover closed, or after a later ask, are dropped. */

async function read(path?: string): Promise<boolean> {
  const fn = dirs()
  if (!fn) {
    set({ ...get(), err: t('gui.wd.not_live') })
    return false
  }
  const ticket = ++asking
  set({ ...get(), loading: true, err: null, asked: get().asked + 1 })
  try {
    const listing = await fn(path)
    if (ticket !== asking || !get().open) return false
    set({ ...get(), view: 'browse', listing, loading: false, err: null })
    return true
  } catch (e) {
    if (ticket !== asking || !get().open) return false
    set({ ...get(), loading: false, err: t('gui.wd.failed', { detail: detailOf(e) }) })
    return false
  }
}

export function browse(path?: string): Promise<void> {
  return read(path).then(() => undefined)
}

/** Back from the browser to the menu, rebuilt: a pick may have been made. */
export function back(): void {
  asking += 1
  using = null
  set({ ...get(), view: 'menu', listed: menuRows(), loading: false, err: null })
}

/** The view a root needs: the other roots the gateway's machine exposes.
 *  A root has no parent to go up to, so without this a browser that started
 *  in the user's home could never reach a second drive. */
export function drives(): void {
  const here = get().listing
  const roots = here?.roots
  if (!roots || !roots.length) return
  asking += 1
  set({ ...get(), view: 'drives', loading: false, err: null })
}

/** A path typed into the browser's bar: read as any other folder, and refused
 *  the same way, on the view the reader is looking at. */
export function go(path: string): Promise<void> {
  const at = path.trim()
  if (!at) return Promise.resolve()
  asking += 1
  return browse(at)
}

/** The browser's "use this folder", offered only where the engine would take
 *  it. A path typed into the bar is read first, like Enter; only a successful
 *  read is staged. */
export async function useHere(path?: string): Promise<void> {
  const here = get().listing
  if (!here || !here.ok) return
  if (using !== null) return
  const at = path?.trim() ?? ''
  if (!at || at === here.path) {
    pick(here.path)
    return
  }
  const press = ++presses
  using = press
  try {
    if (!(await read(at))) return
    /* A typed path is judged as a row is: walking into a folder the engine would
       refuse stays allowed, the pick alone is withheld. */
    const landed = get().listing
    if (!landed || !landed.ok) {
      set({ ...get(), err: t('gui.wd.blocked') })
      return
    }
    pick(landed.path)
  } finally {
    if (using === press) using = null
  }
}

/* Test seam only: the pick, the ticket and the popover are the module's. */
export function _resetForTests(): void {
  picked = null
  using = null
  asking = 0
  set(shut)
}
