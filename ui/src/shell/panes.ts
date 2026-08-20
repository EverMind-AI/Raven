/* Resizable panels.
 *
 * Both panels drag the same way, so the differences are data: which variable
 * the grip writes, which direction widens it, and the bounds. The floor and
 * ceiling are not decoration -- a 90px rail cannot show a session title and a
 * 900px panel leaves no transcript -- and the ceiling is additionally clamped
 * against the window so a drag can never squeeze the chat below its floor.
 *
 * Behaviour, not rendering, like the scrollbars: a grip drag writes one CSS
 * variable on the document element and the grid does the rest, so there is no
 * component here to own.
 */

import { shell } from './bridge'
import { sync } from './scrollbars'

export type PaneName = 'rail' | 'ws'

interface Pane {
  v: string
  min: number
  max: number
  key: string
  edge: 'left' | 'right'
}

export const PANE: Record<PaneName, Pane> = {
  rail: { v: '--rail', min: 190, max: 420, key: 'raven.gui.railw', edge: 'left' },
  ws: { v: '--wsw', min: 320, max: 760, key: 'raven.gui.wsw', edge: 'right' },
}

const names = Object.keys(PANE) as PaneName[]

/* The chat's floor is a CSS token because the grid enforces it too -- read it
   back instead of keeping a second copy of the number here. */
export const cssPx = (name: string): number =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0

export function paneMax(p: Pane): number {
  const app = document.querySelector<HTMLElement>('.app')
  const railOff = app?.dataset.rail === 'off'
  const rail = document.querySelector<HTMLElement>('.rail')
  const taken = p.edge === 'right' && !railOff ? (rail ? rail.offsetWidth : 0) : 0
  return Math.max(p.min, Math.min(p.max, window.innerWidth - taken - cssPx('--chat-min')))
}

export function set(name: PaneName, px: number, persist: boolean): number {
  const p = PANE[name]
  const w = Math.round(Math.max(p.min, Math.min(paneMax(p), px)))
  document.documentElement.style.setProperty(p.v, w + 'px')
  if (persist) {
    try {
      localStorage.setItem(p.key, String(w))
    } catch {
      /* private mode */
    }
  }
  sync()
  return w
}

/* A width stored on a wide screen must not survive onto a narrow one unusably,
   so the stored value is re-clamped rather than trusted. */
export function load(): void {
  for (const name of names) {
    let v: string | null = null
    try {
      v = localStorage.getItem(PANE[name].key)
    } catch {
      /* private mode */
    }
    if (v) set(name, parseFloat(v), false)
  }
}

export function gripDrag(el: HTMLElement, name: PaneName): void {
  const p = PANE[name]
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault()
    const startX = e.clientX
    const start = cssPx(p.v) || p.min
    el.dataset.drag = 'true'
    el.setPointerCapture(e.pointerId)
    /* While dragging, the pointer is over the transcript half the time; without
       this the drag keeps selecting text under it. */
    document.body.style.userSelect = 'none'
    const move = (ev: PointerEvent): void => {
      const d = ev.clientX - startX
      set(name, start + (p.edge === 'right' ? -d : d), false)
      shell().dockLift?.()
    }
    const up = (): void => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      delete el.dataset.drag
      document.body.style.userSelect = ''
      set(name, cssPx(p.v), true)
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  })
  /* Keyboard: the grip is a real separator, so arrows move it. */
  el.tabIndex = 0
  el.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 40 : 10
    const at = cssPx(p.v) || p.min
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      set(name, at + (p.edge === 'right' ? step : -step), true)
    }
    if (e.key === 'ArrowRight') {
      e.preventDefault()
      set(name, at + (p.edge === 'right' ? -step : step), true)
    }
  })
}

/* Shrinking the window must re-clamp both, or the chat loses its floor. */
function onResize(): void {
  for (const n of names) set(n, cssPx(PANE[n].v) || PANE[n].min, false)
}

export function install(): void {
  const railGrip = document.getElementById('railGrip')
  const wsGrip = document.getElementById('wsGrip')
  if (railGrip) gripDrag(railGrip, 'rail')
  if (wsGrip) gripDrag(wsGrip, 'ws')
  window.addEventListener('resize', onResize)
}
