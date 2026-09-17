/* The hover pill: which control is being pointed at, and where its label goes.
 *
 * One element for the whole window, because a pill drawn inside the control's
 * own parent was clipped by every panel edge it reached and covered by the
 * composer's glass -- a single layer outranks all of that, and the placer
 * clamps it to the viewport instead of relying on overflow.
 *
 * Placement prefers above (below for `.tipdn`), flips when the preferred side
 * would leave the window, and clamps either way. It is measured, so the text
 * has to be on screen before the box is: the label is committed, then the pill
 * is raised, then it is measured and moved -- the order the writer had, and the
 * reason the commit below is synchronous.
 *
 * The layer itself comes from state/portals.ts, which is where the order of
 * everything standing at the body is declared: `.tipp` shares its `--z` step
 * with the update shade, so its place among the body's children is the whole
 * of which one covers the other.
 */
import { flushSync } from 'react-dom'

import * as portals from './portals'

export interface Tip {
  /** The layer the pill is drawn in, once it stands at the body. */
  readonly host: HTMLElement | null
  /** The label of the control last pointed at, or null before the first one. */
  readonly text: string | null
}

let state: Tip = { host: null, text: null }

/* The control the pill is currently about. Held rather than derived because
   `pointerover` fires for every descendant of it as well, and a pill that
   re-placed itself on each of those would jump while the pointer moved inside
   one button. */
let hovered: HTMLElement | null = null

const listeners = new Set<() => void>()

export const get = (): Tip => state

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/* Synchronous, because the caller measures what it just wrote: the pill's own
   height decides whether the label goes above or below. */
function commit(next: Tip): void {
  state = next
  flushSync(() => { for (const fn of [...listeners]) fn() })
}

/** Raises the layer, where the body's declared order files it. */
export function mount(): void {
  commit({ host: portals.host('tip'), text: state.text })
}

function place(): void {
  if (!hovered || !hovered.isConnected || !hovered.dataset.tip) { hide(); return }
  commit({ host: state.host, text: hovered.dataset.tip })
  const pill = state.host
  if (!pill) return
  pill.dataset.on = 'true'
  const vw = document.documentElement.clientWidth
  const vh = document.documentElement.clientHeight
  const r = hovered.getBoundingClientRect()
  const tr = pill.getBoundingClientRect()
  const below = hovered.classList.contains('tipdn')
  let top = below ? r.bottom + 5 : r.top - tr.height - 5
  if (top < 4) top = r.bottom + 5
  if (top + tr.height > vh - 4) top = r.top - tr.height - 5
  pill.style.left = `${Math.max(4, Math.min(vw - tr.width - 4, r.left + r.width / 2 - tr.width / 2))}px`
  pill.style.top = `${Math.max(4, top)}px`
}

/* The label is left where it was: nothing reads it while the pill is down, and
   clearing it would make the next hover of the same control rewrite text that
   had not changed. */
export function hide(): void {
  hovered = null
  if (state.host) state.host.dataset.on = 'false'
}

/** The pointer entering anything: the pill follows whatever declares a label. */
export function follow(event: Event): void {
  const target = event.target as Element | null
  const next = target?.closest ? target.closest<HTMLElement>('[data-tip]') : null
  if (next === hovered) return
  if (next) { hovered = next; place() } else hide()
}

/* A control that rewrites its own label while the pointer is on it -- the copy
   button's "copied" flash -- keeps the visible pill in step. */
export function watch(): void {
  new MutationObserver(() => { if (hovered) place() })
    .observe(document.body, { subtree: true, attributes: true, attributeFilter: ['data-tip'] })
}

/** Test seam only: the layer and the pointed-at control outlive a test. */
export function _resetForTests(): void {
  hovered = null
  state = { host: null, text: null }
}
