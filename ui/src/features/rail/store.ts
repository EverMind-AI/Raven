import { ds, shell } from '../../shell/bridge'
import { mark as navMark } from '../../shell/navfly'
import { setCurrent } from '../../shell/session'
import { plainTitle } from './title'

import type { RailSnapshot, RailSource, SessRow } from './types'

/* Rail state, outside React on purpose: the legacy shell drives the list
 * imperatively (every layer calls drawList() after touching SESS, the live
 * boot holds it on skeletons, a language flip redraws it), so the state
 * lives in a plain store the shims can call, and the component subscribes.
 *
 * Unlike the page islands this store owns no rows: the session list stays
 * in the shared SESS array and session pointer, which the transcript, turn,
 * schedule and settings layers all write in place. Each draw pulls a fresh
 * snapshot through DS.sessions instead of keeping a copy that could go stale.
 */

export interface RailState {
  /* Null until the first draw: the legacy #list started empty too. */
  snap: RailSnapshot | null
  /* True while the live boot holds the rail on skeleton rows. */
  skel: boolean
}

let state: RailState = { snap: null, skel: false }
const listeners = new Set<() => void>()

export const getState = (): RailState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<RailState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): RailSource => ds<RailSource>('sessions')

export function reconcileRows(
  previous: SessRow[],
  incoming: SessRow[],
  currentId: string | null
): { currentMissing: boolean; rows: SessRow[] } {
  const oldById = new Map(previous.map(row => [row.id, row]))
  for (const row of incoming) {
    const old = oldById.get(row.id)
    if (old?.status) row.status = old.status
  }
  const current = currentId ? oldById.get(currentId) : undefined
  const currentListed = !!currentId && incoming.some(row => row.id === currentId)
  if (current && !current.persisted && !currentListed) incoming.unshift(current)
  return {
    currentMissing: !!current?.persisted && !currentListed,
    rows: incoming
  }
}

export function removeSessionRow(
  rows: SessRow[],
  currentId: string | null,
  deletedId: string
): { kind: 'draft' | 'open' | 'unchanged'; next?: SessRow; rows: SessRow[] } {
  const remaining = rows.filter(row => row.id !== deletedId)
  if (currentId !== deletedId) return { kind: 'unchanged', rows: remaining }
  const next = remaining[0]
  return next ? { kind: 'open', next, rows: remaining } : { kind: 'draft', rows: remaining }
}

const curId = (): string | null => {
  try {
    return source().snapshot().cur
  } catch {
    return null
  }
}

/* Collapsed rail groups survive reloads; expanded is the default. */
const FOLD_KEY = 'raven.gui.grpfold'
const grpFold = new Set<string>(
  (() => {
    try {
      return JSON.parse(localStorage.getItem(FOLD_KEY) || '[]') as string[]
    } catch {
      return []
    }
  })()
)
const saveGrpFold = (): void => {
  try {
    localStorage.setItem(FOLD_KEY, JSON.stringify([...grpFold]))
  } catch {
    /* private mode */
  }
}
/* Which capped groups stand fully expanded; page-lifetime only, like the
   legacy listOpen set. */
const listOpen = new Set<string>()

export const isFolded = (gid: string): boolean => grpFold.has(gid)
export const isOpen = (gid: string): boolean => listOpen.has(gid)

/* Both flips go back out through the shell's drawList, not a local render:
   during the live boot that name is wrapped to keep the skeletons up, and
   the flip must lose to that hold the way the legacy redraw did. */
export function flipFold(gid: string): void {
  if (grpFold.has(gid)) grpFold.delete(gid)
  else grpFold.add(gid)
  saveGrpFold()
  shell().drawList?.()
}

export function flipOpen(gid: string): void {
  if (listOpen.has(gid)) listOpen.delete(gid)
  else listOpen.add(gid)
  shell().drawList?.()
}

/* One draw = one snapshot. A source that cannot answer keeps the last rows
   on screen rather than blanking the rail. */
export function draw(): void {
  let snap: RailSnapshot
  try {
    snap = source().snapshot()
  } catch {
    return
  }
  markNew()
  set({ snap, skel: false })
}

/* The live boot's hold: skeleton rows until the real list lands. The demo
   never calls this. */
export function skeleton(): void {
  set({ skel: true })
}

/* One writer for the rail's nav marks, because the surfaces stack: the
   settings dialog layers over a module page, which layers over the chat,
   and every one of them has a row that would claim to be current on its
   own. Deciding it in one place from the topmost surface is what makes two
   selected rows impossible.

   The new-task row stands for a draft -- a draft has no session id, so an
   empty `cur` is its state -- but only while nothing covers it. Imperative
   on purpose: every element it marks lives outside the island's root. */
export function markNew(): void {
  const sh = shell()
  const nav = sh.navState?.()
  if (!nav) return
  const el = (id: string): HTMLElement | null => document.getElementById(id)
  const app = document.querySelector<HTMLElement>('.app')
  const pageUp =
    app && app.dataset.page === 'on'
      ? nav.pages.find(p => {
          const n = el(p)
          return !!n && n.dataset.open === 'true'
        }) || null
      : null
  let top: string | null | undefined = pageUp ? nav.btnOf(pageUp) : !curId() ? 'newBtn' : null
  /* While the More group stands open its rows are rail rows, and the current
     one wears the mark itself; the parent lights up only when the group is
     folded and has to stand in for whichever of its pages is open. The rows
     are the flyout module's to write -- it is asked, not reached into, and it
     answers whether the group stood open. */
  if (navMark() && top === 'moreBtn') top = null
  for (const id of ['newBtn', 'skillBtn', 'plugBtn', 'memBtn', 'moreBtn']) {
    const b = el(id)
    if (b) b.setAttribute('aria-current', String(id === top))
  }
}

/* Every session at once, from the settings page's data section. Same guard as
   the pin: no source installed means there is nothing to delete from. */
export function deleteAll(): void {
  try {
    source().deleteAll?.()
  } catch {
    /* no source, nothing to delete */
  }
}

/* Persisting a pin, when there is anywhere to persist it. Wrapped rather than
   read at the call site because source() throws with nothing installed, and an
   optimistic move must not be undone by the attempt to record it. */
export function pin(id: string, pinned: boolean): void {
  try {
    source().pin?.(id, pinned)
  } catch {
    /* no source, nowhere to put it */
  }
}

export function renameRow(s: SessRow, title: string): void {
  if (title === s.title) return
  s.title = title
  if (curId() === s.id) {
    const heading = document.getElementById('title')
    if (heading) heading.textContent = plainTitle(title)
  }
  try {
    source().renamed?.(s.id, title)
  } catch {
    /* no source, nowhere to put it */
  }
  shell().drawList?.()
}

export function archive(s: SessRow): void {
  let via: RailSource['archive']
  try {
    via = source().archive
  } catch {
    /* nothing installed: hiding the local row is the whole behaviour */
  }
  if (via) {
    via(s)
    return
  }
  const rows = source().snapshot().rows
  const at = rows.indexOf(s)
  const index = rows.findIndex(row => row.id === s.id)
  if (index >= 0) rows.splice(index, 1)
  const sh = shell()
  sh.drawList?.()
  sh.toast(sh.T('gui.sess.archived', { title: s.title }), {
    label: sh.T('gui.undo'),
    fn: () => {
      const current = source().snapshot().rows
      if (!current.some(row => row.id === s.id)) current.splice(Math.max(0, Math.min(at, current.length)), 0, s)
      sh.drawList?.()
    }
  })
}

/* Deleting a session. A source that can delete one does it -- the live page
   has a confirmation to ask and a pile of per-session state to forget, none of
   which belongs to the rail. Without one, this is the whole behaviour: splice
   the shared rows in place rather than rebinding SESS (an island cannot
   reassign a page-script binding), and offer it back. */
let undoBin: { s: SessRow; at: number } | null = null

export function remove(s: SessRow): void {
  let via: RailSource['remove']
  try {
    via = source().remove
  } catch {
    /* nothing installed: the local behaviour below is the whole of it */
  }
  if (via) {
    via(s)
    return
  }
  const sh = shell()
  const rows = source().snapshot().rows
  const at = rows.indexOf(s)
  sh.dropDraft?.(s.id)
  const i = rows.findIndex(x => x.id === s.id)
  if (i >= 0) rows.splice(i, 1)
  undoBin = { s, at }
  const st = source().snapshot()
  if (st.cur === s.id) {
    const nx = st.rows[0]
    if (nx) {
      setCurrent(nx.id)
      sh.openSession?.(nx)
    }
  }
  sh.drawList?.()
  sh.toast(shell().T('gui.sess.deleted_x', { title: s.title }), {
    label: shell().T('gui.undo'),
    fn: () => {
      const bin = undoBin as { s: SessRow; at: number }
      source().snapshot().rows.splice(bin.at, 0, bin.s)
      undoBin = null
      sh.drawList?.()
    }
  })
}

/* Inline rename in the top bar; the list follows. The DOM dance -- swap
   #title for an input, put an h1#title back -- is the legacy one, kept
   byte-for-byte. What changed is who persists it: the source is TOLD the new
   title (see renamed in types.ts), where the live layer used to wrap this
   function and hang its own blur listener off the input created here. */
export function rename(): void {
  const h = document.getElementById('title')
  if (!h) return
  let snap: RailSnapshot
  try {
    snap = source().snapshot()
  } catch {
    return
  }
  const s = snap.rows.find(x => x.id === snap.cur)
  if (!s) return
  const inp = document.createElement('input')
  inp.className = 'titin'
  inp.value = s.title
  h.replaceWith(inp)
  const rb = document.getElementById('renameBtn') as HTMLButtonElement | null
  if (rb) rb.hidden = true
  inp.focus()
  inp.select()
  const was = s.title
  const finish = (commit: boolean): void => {
    const v = inp.value.trim()
    const next = commit && v ? v : s.title
    s.title = next
    /* Only on a real change, and from inside finish rather than off a blur:
       committing with Enter replaces the input while it still has focus, and
       whether that fires a blur at all is the browser's business -- which is
       why the wrapper this replaces could miss an Enter entirely. */
    if (next !== was) source().renamed?.(s.id, next)
    const nh = document.createElement('h1')
    nh.textContent = plainTitle(next)
    nh.id = 'title'
    inp.replaceWith(nh)
    if (rb) rb.hidden = false
    shell().drawList?.()
  }
  inp.onblur = () => finish(true)
  inp.onkeydown = e => {
    if (e.isComposing || e.keyCode === 229) return
    if (e.key === 'Enter') {
      e.preventDefault()
      finish(true)
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      inp.onblur = null
      finish(false)
    }
  }
}
