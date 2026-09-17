/* The session search row (#findBox in the markup): the term, and whether the
 * row is showing.
 *
 * Two facts and no DOM, now that <FindRow/> renders the row from them
 * (src/chrome/Rail.tsx): the row's `hidden`, the clear button's `hidden` and
 * the search button's `aria-expanded` are the same state read three ways, and
 * they used to be three writes by id here -- which also meant "is the row
 * open" was answerable only by asking the box what it was hiding.
 *
 * The field itself stays out of React's hands, and that is the reason this is a
 * store rather than an island: a component owning #sfind would re-render a text
 * field the reader is typing into. So `install()` puts the field's three
 * listeners on it natively -- input, keydown, blur -- and the two buttons beside
 * it call in from their own onClick. The listeners have to be native rather than
 * React's for a second reason as well: `input` and `blur` do not bubble, and the
 * Escape key is stopped here so that the document's chain does not also take a
 * panel down (see below).
 *
 * The term used to be a page global the chrome wrote and the rail's snapshot
 * carried back in. It is neither now: it lives here, and the rail asks for it
 * through term() below -- a direct read inside one bundle, not a seam. A seam
 * would mean the demo and live layers each having to answer for a value only
 * this module can produce, which is exactly the coupling the DataSource seam
 * exists to retire.
 *
 * The rail installs the redraw callback from the bundle entry point. Its own
 * live-boot hold decides whether that means rows or skeletons.
 */

import { flushSync } from 'react-dom'

import { composing } from '../features/composer/store'

export interface FindState {
  /** Whether the row is showing. Served closed: page.html hides the box. */
  readonly open: boolean
  /** What the rail filters by, lowercased and trimmed at write time. */
  readonly query: string
}

let state: FindState = { open: false, query: '' }
const listeners = new Set<() => void>()
let redraw = (): void => {}

export function onChange(fn: () => void): void {
  redraw = fn
}

/** The row's state, for <FindRow/>. */
export function get(): FindState {
  return state
}

/** For useSyncExternalStore: called whenever the row or the term changes. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* What the rail filters by. Lowercased and trimmed at write time, so every
   reader compares against the same shape. */
export function term(): string {
  return state.query
}

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null
const field = (): HTMLInputElement | null => el<HTMLInputElement>('sfind')

/* Committed synchronously, the way the three writes by id were: the row is
   shown and only then focused -- a hidden field takes no focus -- and the clear
   button has to appear on the keystroke that gave it something to clear. */
function put(next: Partial<FindState>): void {
  const merged = { ...state, ...next }
  if (merged.open === state.open && merged.query === state.query) return
  state = merged
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

/* Clearing is three facts, not one: the field, the term, and the clear button
   that only exists while there is something to clear. The field is the only one
   written by hand, because it is the only one React does not own. */
function wipe(): void {
  const f = field()
  if (f) f.value = ''
}

/* Search is a chore, so it hides until asked for; leaving it empty and
   clicking away puts the row back. */
export function toggle(force?: boolean): void {
  const open = force != null ? force : !state.open
  if (open) {
    put({ open: true })
    field()?.focus()
    return
  }
  /* Closing on an empty field changes nothing, so it must not cost a redraw:
     the blur handler below closes the row on every click away. */
  const had = state.query
  if (had) wipe()
  put({ open: false, query: '' })
  if (had) redraw()
}

/** The clear button's click: the term goes, the caret stays in the field. */
export function clear(): void {
  wipe()
  put({ query: '' })
  redraw()
  field()?.focus()
}

export function install(): void {
  const f = field()
  if (!f) return
  f.oninput = () => {
    put({ query: f.value.trim().toLowerCase() })
    redraw()
  }
  f.onkeydown = (e) => {
    /* Escape ends an open composition first; taking the row down on that
       keystroke would discard the candidate the reader was still typing. */
    if (composing(e)) return
    if (e.key === 'Escape') {
      e.stopPropagation()
      toggle(false)
    }
  }
  f.onblur = () => {
    if (!state.query) toggle(false)
  }
}
