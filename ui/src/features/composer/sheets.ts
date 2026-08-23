/* The sheet rack: session-scoped bookkeeping for everything that docks above
 * the composer.
 *
 * Part of the composer island rather than a writer of its own, for three
 * reasons that all point the same way. The rack it fills (`#sheetRack`) sits
 * inside `.dock`, one node above the composer card in the same markup. Every
 * mutation here ends in `dockLift()`, which is already the composer's. And by
 * the rule `shell/lightbox.ts` states -- one node appended to a host is a
 * writer, a container plus a list is an island -- a static container plus a Map
 * of element sets is the second thing.
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
 * sheet -- with the reader's half-typed answer in it -- not a fresh one.
 *
 * A detached sheet's document-level key handler is still live, which is the one
 * thing this scoping does not fix on its own. Each handler checks
 * `sheet.isConnected` for that reason: otherwise "1" typed in one conversation
 * would answer a question waiting in another. Unregistering it when the sheet
 * leaves for good is the rack's job, through the teardown below.
 */

import { verb } from '../../shell/bridge'
import { dockLift } from './store'

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

/* A draft is not a session yet -- the page's `cur` is null until the first
   message lands -- but a question can be asked during its first turn, so it
   needs a key of its own rather than sharing one with every other draft-less
   state.
   `sessionKey` rather than a reach for `cur`: the page owns which conversation
   is open, and it is asked through the verb it already publishes for this.
   Insisted on rather than defaulted, because a rack that silently filed every
   sheet under one key would put another conversation's question back over the
   composer -- which is the bug this whole module exists to prevent. */
export const session = (): string => verb('sessionKey')() || '(draft)'

const rack = (): HTMLElement => document.querySelector<HTMLElement>('#sheetRack') || document.body

export function add(el: HTMLElement, key?: string, teardown?: () => void): void {
  const k = key || session()
  el.dataset.sess = k
  if (teardown) TEARDOWN.set(el, teardown)
  let bucket = SHEETS.get(k)
  if (!bucket) SHEETS.set(k, (bucket = new Set()))
  bucket.add(el)
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
  /* Dropped from the map before it is run, because a teardown typically ends in
     the tenant's own close, which calls back in here. Clearing the entry first
     makes that second pass find nothing rather than recurse. */
  const down = TEARDOWN.get(el)
  if (down) {
    TEARDOWN.delete(el)
    down()
  }
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
}
