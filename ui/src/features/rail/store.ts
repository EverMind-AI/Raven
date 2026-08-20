import { ds, shell } from '../../shell/bridge'
import { mark as navMark } from '../../shell/navfly'
import { plainTitle } from './title'

import type { RailSnapshot, RailSource, SessRow } from './types'

/* Rail state, outside React on purpose: the legacy shell drives the list
 * imperatively (every layer calls drawList() after touching SESS, the live
 * boot holds it on skeletons, a language flip redraws it), so the state
 * lives in a plain store the shims can call, and the component subscribes.
 *
 * Unlike the page islands this store owns no rows: the session list stays
 * in the shared SESS/cur globals, which the transcript, turn, schedule and
 * settings layers all write in place. Each draw pulls a fresh snapshot
 * through DS.sessions instead of keeping a copy that could go stale.
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
  })(),
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
      ? nav.pages.find((p) => {
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

/* The demo-mode delete, reached through the legacy removeSession shim (live
   mode rebinds that name to its RPC version, so this never runs there).
   Splices the shared rows in place rather than rebinding SESS -- an island
   cannot reassign a page-script binding -- which every reader observes the
   same way. */
let undoBin: { s: SessRow; at: number } | null = null

export function remove(s: SessRow): void {
  const sh = shell()
  const rows = source().snapshot().rows
  const at = rows.indexOf(s)
  sh.dropDraft?.(s.id)
  const i = rows.findIndex((x) => x.id === s.id)
  if (i >= 0) rows.splice(i, 1)
  undoBin = { s, at }
  const st = source().snapshot()
  if (st.cur === s.id) {
    const nx = st.rows[0]
    if (nx) {
      sh.setCur?.(nx.id)
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
    },
  })
}

/* Inline rename in the top bar; the list follows. The live layer wraps the
   legacy renameTitle name around this to persist the result, so the DOM
   dance (swap #title for an input, put an h1#title back) stays exactly the
   legacy one. */
export function rename(): void {
  const h = document.getElementById('title')
  if (!h) return
  let snap: RailSnapshot
  try {
    snap = source().snapshot()
  } catch {
    return
  }
  const s = snap.rows.find((x) => x.id === snap.cur)
  if (!s) return
  const inp = document.createElement('input')
  inp.className = 'titin'
  inp.value = s.title
  h.replaceWith(inp)
  const rb = document.getElementById('renameBtn') as HTMLButtonElement | null
  if (rb) rb.hidden = true
  inp.focus()
  inp.select()
  const finish = (commit: boolean): void => {
    const v = inp.value.trim()
    const next = commit && v ? v : s.title
    s.title = next
    const nh = document.createElement('h1')
    nh.textContent = plainTitle(next)
    nh.id = 'title'
    inp.replaceWith(nh)
    if (rb) rb.hidden = false
    shell().drawList?.()
  }
  inp.onblur = () => finish(true)
  inp.onkeydown = (e) => {
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
