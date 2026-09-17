/* The sheet rack: session-scoped bookkeeping for everything that docks above
 * the composer.
 *
 * State rather than a writer, for the reason `state/lightbox.ts` states -- one
 * node appended to a host is a writer, a container plus a list is not -- and
 * the list is what is here: which conversation each sheet belongs to, what it
 * renders, and who is waiting on the reader. The rack it fills (`#sheetRack`)
 * sits inside `.dock`, one node above the composer card in the same markup, and
 * every mutation here still ends in `dockLift()`, which is the composer's.
 *
 * A sheet's own element is the host its tenant hands over, not something React
 * renders: `.dock .sheets > *` styles that element as the flex item and
 * `data-sess` is written on it, so a wrapper around it would take both, and the
 * rack's child list is mixed -- the dag sheet is a host with a root of its own.
 * The interior is a component (src/chrome/SheetRack.tsx portals each view into
 * the host it belongs to), so this module keeps the DOM order it always had:
 * newest first, the same insertBefore, and the caret undisturbed by a sync.
 *
 * What docks here -- a clarify question, an approval request, a dag graph --
 * belongs to the conversation it was raised in. Each used to be appended
 * straight into `.dock-in`, which is one element for the whole window, so
 * switching sessions left another conversation's question sitting over the
 * composer: still answerable, and answering it replied on behalf of a turn the
 * reader was no longer looking at.
 *
 * A sheet is filed under a session key on arrival and mounted only while that
 * session is open. Detached rather than destroyed, and the element is kept: the
 * question is still pending on the server, so coming back has to show the same
 * sheet -- not a fresh one that replays its entrance. Its interior is unmounted
 * while it is parked, which is why what the reader typed into it lives in
 * state/sheetDrafts.ts rather than in the input.
 *
 * A detached sheet's document-level key handler is still live, which is the one
 * thing this scoping does not fix on its own. Each handler checks
 * `sheet.isConnected` for that reason: otherwise "1" typed in one conversation
 * would answer a question waiting in another. Unregistering it when the sheet
 * leaves for good is the rack's job, through the teardown below.
 */

import { flushSync } from 'react-dom'

import { current } from '../lib/session'
import { dockLift } from '../features/composer/store'

import type { ReactNode } from 'react'

const SHEETS = new Map<string, Set<HTMLElement>>()

/* What to run when a sheet leaves the rack for good.

   A sheet's element is not the whole sheet: a tenant may hold a document-level
   key handler, and detaching the element does not unregister it. The rack is
   the only place that knows every way a sheet leaves -- answered by the reader,
   replaced by `dropClass`, or dropped with its conversation by `forget` -- so
   the takedown is registered here rather than kept as a second copy of
   who-owns-what beside this one. A tenant that tracked its own could only cover
   the exits it is told about, and `forget` is not one of them.

   A WeakMap, because a sheet the rack was never told to remove must still be
   collectable. */
const TEARDOWN = new WeakMap<HTMLElement, () => void>()

/* Who is waiting on the reader, per conversation, and who wants to know.
 *
 * A sheet that ASKS something -- an approval, a clarification -- interrupts:
 * the reader cannot get on until they answer it. A sheet that only SHOWS
 * something -- a graph running its nodes -- does not, and when the two dock
 * together the rack runs out of room and the question ends up below the fold.
 *
 * The rack is the one place every tenant passes through, so it is the only one
 * that can say "something is being asked here" without a tenant having to know
 * about the others. It says exactly that and no more: what to do about it
 * belongs to whoever is in the way, which is why this hands out a count rather
 * than an instruction.
 *
 * A tenant marks itself by setting `dataset.asks` before it docks. */
const WATCHERS = new Set<(key: string, asking: number) => void>()

/* The count as it stands, for a tenant that arrives after the fact: a watcher
   only hears about changes, and one docking under a question that is already up
   was never told about it. */
export const askingIn = (key: string): number =>
  [...(SHEETS.get(key) || [])].filter((el) => el.dataset.asks === '1').length

function told(key: string): void {
  if (!key || !WATCHERS.size) return
  const n = askingIn(key)
  WATCHERS.forEach((fn) => {
    try {
      fn(key, n)
    } catch {
      /* A watcher is a courtesy; one that throws must not take the rack down. */
    }
  })
}

/* Hear when a conversation starts or stops being asked something. Returns the
   unsubscribe -- nothing needs it yet, the one watcher lives as long as the
   page does, and it is what keeps this testable. */
export function watchAsking(fn: (key: string, asking: number) => void): () => void {
  WATCHERS.add(fn)
  return () => WATCHERS.delete(fn)
}

/* The interior of each sheet whose conversation is open, for <SheetRack/>.
 *
 * A tenant that renders its sheet from a component hands the view over with the
 * host; one that fills its host itself (the dag sheet, which roots a tree of its
 * own in it) hands over no view and is absent from here. What is rendered is
 * therefore the open conversation's views -- a parked sheet's interior is
 * unmounted, which is what "not rendered" means for the conversation the reader
 * is not looking at. */
export interface RackSheet {
  readonly id: string
  readonly el: HTMLElement
  readonly view: ReactNode
}

const VIEWS = new WeakMap<HTMLElement, RackSheet>()
let shown: readonly RackSheet[] = []
let made = 0
const listeners = new Set<() => void>()

/** The interiors to render, for useSyncExternalStore. */
export function sheets(): readonly RackSheet[] {
  return shown
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* Recomputed from the buckets rather than tracked alongside them, so the one
   record of who is where cannot disagree with itself. The same array comes back
   when nothing moved, which is what useSyncExternalStore requires, and the
   commit is synchronous because the callers below go on to read the DOM: the
   sheet's own focus call, `dockLift`'s measurement, and a tenant reaching for
   its first option are all one statement later. */
function paint(): void {
  const here = session()
  const next: RackSheet[] = []
  for (const [key, bucket] of SHEETS) {
    if (key !== here) continue
    for (const el of bucket) {
      const sheet = VIEWS.get(el)
      if (sheet) next.push(sheet)
    }
  }
  const same = next.length === shown.length && next.every((s, i) => shown[i] === s)
  if (same) return
  shown = next
  if (!listeners.size) return
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

/* A draft is not a session yet -- the session pointer is null until the first
   message lands -- but a question can be asked during its first turn, so it
   needs a key of its own rather than sharing one with every other draft-less
   state.
   The page session module owns which conversation is open; the draft gets a
   stable rack key rather than sharing one with every state lacking an id. */
export const session = (): string => current() || '(draft)'

const rack = (): HTMLElement => document.querySelector<HTMLElement>('#sheetRack') || document.body

export function add(el: HTMLElement, key?: string, teardown?: () => void, view?: ReactNode): void {
  const k = key || session()
  el.dataset.sess = k
  if (teardown) TEARDOWN.set(el, teardown)
  if (view !== undefined) VIEWS.set(el, { id: `sheet${++made}`, el, view })
  let bucket = SHEETS.get(k)
  if (!bucket) SHEETS.set(k, (bucket = new Set()))
  bucket.add(el)
  told(k)
  /* Filled before it is docked, the way a tenant that built its own sheet handed
     over a finished one. */
  paint()
  /* First child, not last: sheets are flow content, and the newest belongs on
     top of the stack, above the field it interrupts. */
  if (k === session()) {
    const dock = rack()
    dock.insertBefore(el, dock.firstChild)
  }
  dockLift()
}

export function remove(el: HTMLElement): void {
  const bucket = SHEETS.get(el.dataset.sess as string)
  if (bucket) {
    bucket.delete(el)
    if (!bucket.size) SHEETS.delete(el.dataset.sess as string)
  }
  el.remove()
  VIEWS.delete(el)
  paint()
  /* Dropped from the map before it is run, because a teardown typically ends in
     the tenant's own close, which calls back in here. Clearing the entry first
     makes that second pass find nothing rather than recurse. */
  const down = TEARDOWN.get(el)
  if (down) {
    TEARDOWN.delete(el)
    down()
  }
  told(el.dataset.sess as string)
  dockLift()
}

/* Retire the sheets of one class in one session's bucket, and only there: a new
   question replaces the pending one it belongs beside, never one another
   conversation is still waiting on. */
export function dropClass(cls: string, key?: string): void {
  const bucket = SHEETS.get(key || session())
  if (!bucket) return
  ;[...bucket].forEach((el) => { if (el.classList.contains(cls)) remove(el) })
}

/* Called wherever the open session changes. Mount what belongs here, detach
   everything else -- including sheets raised while the reader was away. */
export function sync(): void {
  const here = session()
  /* Before the elements move, so a sheet coming back is filled before it is
     docked and one being parked is emptied before it leaves. */
  paint()
  SHEETS.forEach((bucket, key) => bucket.forEach((el) => {
    if (key === here) {
      if (!el.isConnected) {
        const dock = rack()
        dock.insertBefore(el, dock.firstChild)
      }
    } else if (el.isConnected) el.remove()
  }))
  dockLift()
}

/* A deleted conversation's pending question has nothing left to answer. */
export function forget(key: string): void {
  const bucket = SHEETS.get(key)
  if (bucket) [...bucket].forEach(remove)
}

/* Test seam only: the Map outlives a test file's DOM. */
export function _resetForTests(): void {
  SHEETS.clear()
  paint()
  /* Not the watchers. One is registered when its island's module loads, which
     happens once for a whole test file, so clearing them here would unwire the
     first reset and leave every test after it watching nothing. A test that adds
     its own drops it through the unsubscribe. */
}
