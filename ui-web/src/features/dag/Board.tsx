/* The viewport a graph is read in: one canvas, panned and zoomed by hand.
 *
 * Extracted from the playbook page, which is where it was written, because the
 * task view asks the same thing of the same picture -- a graph wider than any
 * pane it is shown in, and a reader who moves rather than a page that shrinks.
 * Nothing here knows what it is framing: it is given a content size and the
 * content, and it decides only where that content sits and at what scale.
 *
 * The opening view frames the whole graph. After that the view is the reader's,
 * and nothing but a change of subject reframes it.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { t } from '../../i18n/t'

import type { JSX, ReactNode } from 'react'

const ZOOM_MIN = 0.3
const ZOOM_MAX = 2
const ZOOM_STEP = 1.15
/* Two devices, one event. A trackpad sends a stream of small deltas per flick,
   so those zoom in proportion to how far it actually moved and glide. A mouse
   sends one large notch, which is a discrete press and gets a discrete step --
   the same one the button gives, so the two controls agree. Treating a notch as
   a proportional delta is what makes a mouse wheel either crawl or bolt. */
const WHEEL_GAIN = 0.008
const MOUSE_NOTCH = 50
/* A pointer that moved less than this between down and up was a click on
   whatever is under it, not a drag of the canvas. */
const DRAG_SLOP = 4
/* Breathing room between the graph and the viewport edge when the graph is too
   big to centre. */
const EDGE = 14

interface View {
  x: number
  y: number
  z: number
}

const clampZoom = (z: number): number => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))

interface BoardProps {
  /* The content's own size, in its own coordinates -- what `layout` returns. */
  width: number
  height: number
  /* What the view is framed for. When it changes the board reframes; while it
     holds, a reader who panned somewhere keeps their view. */
  fitKey: string
  label: string
  /* A press that starts inside one of these belongs to the content, not to the
     canvas: capturing the pointer here would take the pointerup away from the
     box under it, and a click on a step would go nowhere. */
  owns: string
  /* Whether the arrow keys are already spoken for. They walk the steps while a
     step is picked, and pan the canvas only when nothing is. */
  arrowsTaken?: boolean
  /* Fired on a tap that lands on the canvas itself rather than on one of
     `owns`'s boxes, and did not turn into a drag -- the prototype's own
     board clears whatever is picked on a tap of open background. Optional:
     a caller that has no notion of "picked" (there is none today) simply
     omits it. */
  onBlank?: () => void
  children: ReactNode
}

export function Board({ width, height, fitKey, label, owns, arrowsTaken, onBlank, children }: BoardProps): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  const [port, setPort] = useState({ w: 0, h: 0 })
  const [view, setView] = useState<View | null>(null)
  /* Set once the reader has panned or zoomed by hand, and cleared by the fit
     control -- the one thing that still overrides a held view is the reader
     asking for the whole graph back. While held, a size change (say, a
     docked pane going full screen) must not silently snap the view back to
     the opening frame under their hands. */
  const held = useRef(false)

  /* Measured on every render, plus a frame-by-frame retry while there is nothing
     to measure, and NOT on a notification alone.
     The island mounts while its page is still `display: none` -- so the first
     measurement is always zero-width, and a design that settled for 1:1 there
     would frame every graph wrongly until something happened to resize the box.
     A ResizeObserver rescues that in a browser; it is silent in the embedded
     pane this was verified in, and `window.resize` never fired there either.
     Both are kept as the cheap path, but correctness does not depend on
     either. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    let frame = 0
    let slow = 0
    let tries = 0
    const measure = (): void => {
      const w = el.clientWidth
      const h = el.clientHeight
      if (w <= 0 || h <= 0) {
        /* A burst of frames covers the usual case -- the page is being shown
           right now and the box has a size one frame from here. Past that it is
           hidden for as long as the reader is elsewhere, so the watch drops to a
           slow poll rather than stopping: stopping is what leaves the graph
           framed against a size it no longer has. */
        if (tries++ < 60) frame = requestAnimationFrame(measure)
        else if (!slow) slow = window.setInterval(measure, 500)
        return
      }
      tries = 0
      if (slow) {
        window.clearInterval(slow)
        slow = 0
      }
      setPort((prev) => (prev.w === w && prev.h === h ? prev : { w, h }))
    }
    measure()
    window.addEventListener('resize', measure)
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    if (ro) ro.observe(el)
    return () => {
      if (frame) cancelAnimationFrame(frame)
      if (slow) window.clearInterval(slow)
      window.removeEventListener('resize', measure)
      if (ro) ro.disconnect()
    }
  })

  /* Centred at some zoom, never enlarged past life size: a three-step graph
     blown up to fill the viewport would read as a bigger graph. */
  const centred = (z: number): View => ({
    x: (port.w - width * z) / 2,
    y: (port.h - height * z) / 2,
    z
  })
  const fitZoom = (): number => (port.w && port.h ? clampZoom(Math.min(1, port.w / width, port.h / height)) : 1)
  /* The whole graph is the opening view: a box carries a name and an agent,
     which stay readable much further out than a paragraph would.
     Below the zoom floor it still overflows, and then it opens at its start
     rather than centred -- a graph reads from its first step, and centring one
     too big to fit cuts off the first step and the last one at once, which
     looks like damage rather than like a big graph. */
  const opening = (): View => {
    if (!port.w) return { x: 0, y: 0, z: 1 }
    const z = fitZoom()
    const mid = centred(z)
    /* The margin is for the axis that overflows, and only that one: a graph
       that fits keeps its centred offset, including the one that fits exactly,
       where a floor of EDGE would push the last step off the edge it just fit
       inside. */
    return { x: mid.x < 0 ? EDGE : mid.x, y: mid.y < 0 ? EDGE : mid.y, z }
  }

  /* Reframes on any size change -- not only the first -- unless the reader
     has since panned or zoomed by hand: a docked pane's board going full
     screen, or back, is a size change the graph should still open framed
     for, the way the prototype's own `reframeIfNeeded` (its ResizeObserver)
     does. A subject change (`fitKey`) always reframes and drops the hold,
     since a held pan belongs to the graph that was on screen when the
     reader made it. */
  const key = `${fitKey}:${port.w}x${port.h}`
  const lastFit = useRef('')
  const lastSubject = useRef(fitKey)
  if (lastSubject.current !== fitKey) {
    lastSubject.current = fitKey
    held.current = false
  }
  if (port.w > 0 && lastFit.current !== key) {
    lastFit.current = key
    if (!held.current) setView(opening())
  }
  const at = view ?? { x: 0, y: 0, z: 1 }

  /* Composed off the previous view, not off this render's copy of it: a wheel
     gesture delivers several events before React re-renders, and reading the
     zoom from the closure makes all of them compute the same result -- the
     flick lands as one step and the canvas feels stuck. */
  const zoomBy = (factor: number, about?: { x: number; y: number }): void => {
    held.current = true
    setView((prev) => {
      const cur = prev ?? { x: 0, y: 0, z: 1 }
      const z = clampZoom(cur.z * factor)
      /* Zoom about a point: the graph coordinate under it has to stay under it,
         or the canvas swims away from wherever the reader was looking. */
      const cx = about ? about.x : port.w / 2
      const cy = about ? about.y : port.h / 2
      return { x: cx - ((cx - cur.x) / cur.z) * z, y: cy - ((cy - cur.y) / cur.z) * z, z }
    })
  }
  /* The fit control's own job: put the reader back at the opening view, and
     let a later resize reframe again rather than defending a view the
     reader just asked to leave. */
  const fit = (): void => { held.current = false; setView(opening()) }

  const drag = useRef<{ id: number; x: number; y: number; ox: number; oy: number; moved: boolean } | null>(null)
  const onDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    if ((e.target as HTMLElement).closest(owns)) return
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, ox: at.x, oy: at.y, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d || d.id !== e.pointerId) return
    const dx = e.clientX - d.x
    const dy = e.clientY - d.y
    if (!d.moved && Math.abs(dx) + Math.abs(dy) < DRAG_SLOP) return
    d.moved = true
    held.current = true
    setView((prev) => ({ x: d.ox + dx, y: d.oy + dy, z: prev?.z ?? 1 }))
  }
  const onUp = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (d && d.id === e.pointerId) {
      /* A tap that did not turn into a drag, on the canvas rather than on
         one of `owns`'s boxes (`onDown` already returned early for those):
         the prototype's own board clears whatever is picked on a tap of
         open background. */
      if (!d.moved) onBlank?.()
      drag.current = null
    }
  }
  /* Attached by hand, non-passive, because React registers `wheel` as a passive
     listener -- and in a passive listener `preventDefault` is a no-op. Through
     the `onWheel` prop the ctrl+wheel reached the browser as its own page-zoom
     gesture: the whole page grew while this canvas zoomed the other way. */
  useEffect(() => {
    const el = box.current
    if (!el) return
    const onWheel = (e: WheelEvent): void => {
      /* Plain wheel belongs to the page: this board sits in a scrolling column,
         and stealing it would trap the reader inside the canvas. */
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      const r = el.getBoundingClientRect()
      /* deltaY is in lines or pages on some mice; normalise before reading it. */
      const raw = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * (port.h || 1) : e.deltaY
      const factor =
        Math.abs(raw) >= MOUSE_NOTCH ? (raw < 0 ? ZOOM_STEP : 1 / ZOOM_STEP) : Math.exp(-raw * WHEEL_GAIN)
      zoomBy(factor, { x: e.clientX - r.left, y: e.clientY - r.top })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })

  const onKey = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    const pan = e.shiftKey ? 120 : 40
    const panBy = (dx: number, dy: number): void => {
      held.current = true
      setView((prev) => ({ x: (prev ?? at).x + dx, y: (prev ?? at).y + dy, z: (prev ?? at).z }))
    }
    const step: Record<string, () => void> = {
      '+': () => zoomBy(ZOOM_STEP),
      '=': () => zoomBy(ZOOM_STEP),
      '-': () => zoomBy(1 / ZOOM_STEP),
      '0': fit,
      ArrowUp: () => panBy(0, pan),
      ArrowDown: () => panBy(0, -pan),
      ArrowLeft: () => panBy(pan, 0),
      ArrowRight: () => panBy(-pan, 0)
    }
    if (/^Arrow/.test(e.key) && arrowsTaken) return
    const run = step[e.key]
    if (!run) return
    e.preventDefault()
    run()
  }

  return (
    <div className="gboard">
      <div
        className="gstage"
        ref={box}
        tabIndex={0}
        role="application"
        aria-label={label}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
      >
        <div className="gview" style={{ transform: `translate(${at.x}px, ${at.y}px) scale(${at.z})` }}>
          {children}
        </div>
      </div>
      <div className="gzoom">
        <button className="gzb" aria-label={t('gui.dag.zoom_out')} onClick={() => zoomBy(1 / ZOOM_STEP)}>
          &minus;
        </button>
        {/* The percentage is the control that puts the whole graph back in view,
            not a note about what the page decided to do. */}
        <button className="gzpct" onClick={fit} title={t('gui.dag.zoom_fit')}>
          {Math.round(at.z * 100)}%
        </button>
        <button className="gzb" aria-label={t('gui.dag.zoom_in')} onClick={() => zoomBy(ZOOM_STEP)}>
          +
        </button>
      </div>
    </div>
  )
}
