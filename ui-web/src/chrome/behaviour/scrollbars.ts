/* Overlay scrollbars.
 *
 * One implementation for every scroller in the app, installed once by
 * listening for scroll in the capture phase -- a panel drawn later, or a list
 * that only exists while a dialog is open, is covered without registering
 * anything. Thumbs are created on demand and parked in a fixed layer, so they
 * float over the content instead of taking a column out of it.
 *
 * Behaviour, not rendering: nothing here belongs to a component tree. The
 * thumbs are positioned from measurements taken at the moment of the scroll,
 * over elements owned by whatever layer drew them, so this stays a plain
 * module that state/globalListeners.ts installs once.
 *
 * SB_HIDE is how long a bar stays after the last scroll event. Long enough to
 * read where you are in a long transcript, short enough that it is gone before
 * you look at the layout again.
 */

import * as portals from '../../state/portals'

export const SB_HIDE = 900
export const SB_MIN = 26
export const SB_PAD = 2

type Axis = 'v' | 'h'

interface Bars {
  v: HTMLElement | null
  h: HTMLElement | null
  timer: number
  drag: boolean
}

const AXES: readonly Axis[] = ['v', 'h']

const sbMap = new WeakMap<HTMLElement, Bars>()
/* Which thumbs already carry their pointerdown listener. A WeakSet rather than
   a flag on the node, so a thumb that goes away is forgotten with it. */
const dragWired = new WeakSet<HTMLElement>()

/* Whoever is showing a bar right now. A scroller that moved without being
   scrolled -- because an ancestor scrolled, or a panel next to it was dragged
   wider -- still has to have its thumb put back where the box now is. */
const live = new Set<HTMLElement>()

/* Every scroller that has a thumb in the layer, showing or faded. */
const owners = new Set<HTMLElement>()

/* A scroller can leave the page with its thumb still parked in a layer of its
   own -- the model picker closing, a dialog torn down. A showing thumb then
   hung over the page until its fade ran out, and a faded one stayed in the
   layer for good, one per picker opened. So while any thumb exists, removals
   from the document take the thumbs of the scrollers they took. */
let departures: MutationObserver | null = null

function watchDepartures(): void {
  if (departures || typeof MutationObserver === 'undefined') return
  departures = new MutationObserver(() => {
    owners.forEach((el) => {
      if (!el.isConnected) hide(el)
    })
  })
  departures.observe(document.documentElement, { childList: true, subtree: true })
}

function settle(): void {
  if (owners.size || !departures) return
  departures.disconnect()
  departures = null
}

/* The fixed layer the thumbs are parked in. Handed out by state/portals.ts,
   which is where the order of everything standing at the body is declared:
   this layer shares its `--z` step with nothing, but the two below it do, and
   one module appending its own layer whenever it liked is how such an order
   goes wrong. Still made on the first ask, which is what keeps it in the boot
   goldens where it is. */
const layer = (): HTMLElement => portals.host('sbars')

/* The scroll event from the document, the documentElement and the window all
   mean the same scroller. */
function root(target: EventTarget | null): HTMLElement | null {
  const el =
    target === document || target === document.documentElement || target === window
      ? document.scrollingElement
      : (target as Node | null)
  return el && el.nodeType === 1 ? (el as HTMLElement) : null
}

function bars(el: HTMLElement): Bars {
  let b = sbMap.get(el)
  if (!b) {
    b = { v: null, h: null, timer: 0, drag: false }
    sbMap.set(el, b)
  }
  return b
}

function thumb(el: HTMLElement, b: Bars, axis: Axis): HTMLElement {
  const had = b[axis]
  if (had) return had
  const t = document.createElement('div')
  t.className = 'sbar'
  t.dataset.axis = axis
  /* The lightbox is the one overlay above this layer that scrolls; page.css
     lifts the layer over it while it is open and shows only these there. */
  if (el.closest('.lightbox')) t.dataset.over = 'lightbox'
  layer().appendChild(t)
  b[axis] = t
  owners.add(el)
  watchDepartures()
  return t
}

/* Takes one axis's thumb out of the layer, and the scroller off the watch once
   it has none left. */
function drop(el: HTMLElement, b: Bars, axis: Axis): void {
  const t = b[axis]
  if (!t) return
  t.remove()
  b[axis] = null
  if (!b.v && !b.h) {
    owners.delete(el)
    settle()
  }
}

/* Geometry is read from the element every time rather than cached: a scroller
   can be resized, moved by a panel drag, or re-rendered under the same node. */
interface Box {
  top: number
  bottom: number
  left: number
  right: number
}

/* Which of a scroller's box is actually on screen.
 *
 * A scroller inside another scroller can be scrolled halfway out of it, and a
 * thumb placed from the inner box alone is then drawn over whatever sits above
 * the outer one -- the settings dialog's header, in the case that found this.
 * What is visible is decided by the ancestors that clip, so the thumb is
 * clamped to their intersection.
 *
 * Ancestors that do not clip are skipped rather than intersected: a positioned
 * box narrower than its overflowing child would otherwise cut a bar that the
 * page is perfectly happy to show. */
function visible(el: HTMLElement): Box | null {
  const r = el.getBoundingClientRect()
  const box: Box = { top: r.top, bottom: r.bottom, left: r.left, right: r.right }
  for (let node = el.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node)
    const clips = (v: string): boolean => v === 'auto' || v === 'scroll' || v === 'hidden' || v === 'clip'
    if (!clips(style.overflowY) && !clips(style.overflowX)) continue
    const a = node.getBoundingClientRect()
    if (!a.width && !a.height) continue
    if (clips(style.overflowY)) {
      box.top = Math.max(box.top, a.top)
      box.bottom = Math.min(box.bottom, a.bottom)
    }
    if (clips(style.overflowX)) {
      box.left = Math.max(box.left, a.left)
      box.right = Math.min(box.right, a.right)
    }
  }
  return box.bottom > box.top && box.right > box.left ? box : null
}

/* Where the thumb goes, read from the element rather than cached: a scroller
   can be resized, moved by a panel drag, re-rendered under the same node, or
   have its content grow while the reader holds its bar. Draws and drags both
   come through here, so the bar the reader sees and the bar they drag cannot
   disagree about the track. */
interface Geo {
  vert: boolean
  /** The track's start on screen: the visible slice's near edge, inside the pad. */
  from: number
  track: number
  len: number
  full: number
  size: number
  /** The fixed edge the thumb is drawn against: 8px in from the scroller's. */
  edge: number
}

function geometry(el: HTMLElement, axis: Axis): Geo | null {
  const vert = axis === 'v'
  const size = vert ? el.clientHeight : el.clientWidth
  const full = vert ? el.scrollHeight : el.scrollWidth
  /* A hidden scroller keeps its box -- the chat column parks its transcript
     that way, under the trajectory view -- but a thumb over it would float
     over whatever is shown there, and drag what nobody can see. */
  if (!el.isConnected || full <= size + 1 || size < 40 || getComputedStyle(el).visibility === 'hidden') return null
  const r = el.getBoundingClientRect()
  if (!r.width || !r.height) return null
  /* The track is the part of the box on screen, not the whole box: a scroller
     inside another scroller can be scrolled halfway out of it, and a bar laid
     out over the whole box is then drawn past the edge of the outer one -- over
     the settings dialog's header, in the case that found this. Measured inside
     the visible slice, the bar stays in the slice and still says where in the
     content the reader is. */
  const box = visible(el)
  if (!box) return null
  const from = (vert ? Math.max(r.top, box.top) : Math.max(r.left, box.left)) + SB_PAD
  const to = (vert ? Math.min(r.bottom, box.bottom) : Math.min(r.right, box.right)) - SB_PAD
  const track = to - from
  if (track < SB_PAD * 2) return null
  const len = Math.min(track, Math.max(SB_MIN, Math.round(track * (size / full))))
  const edge = (vert ? Math.min(r.right, box.right) : Math.min(r.bottom, box.bottom)) - 8
  return { vert, from, track, len, full, size, edge }
}

function place(el: HTMLElement, b: Bars, axis: Axis): HTMLElement | undefined {
  const geo = geometry(el, axis)
  if (!geo) {
    drop(el, b, axis)
    return
  }
  const t = thumb(el, b, axis)
  const pos = (geo.vert ? el.scrollTop : el.scrollLeft) / (geo.full - geo.size)
  const off = Math.round((geo.track - geo.len) * Math.min(1, Math.max(0, pos)))
  if (geo.vert) {
    t.style.cssText = `top:${geo.from + off}px;left:${geo.edge}px;width:6px;height:${geo.len}px`
  } else {
    t.style.cssText = `left:${geo.from + off}px;top:${geo.edge}px;height:6px;width:${geo.len}px`
  }
  wireDrag(t, el, axis)
  return t
}

/* A scroller can also declare that its bar stays: `data-thumb="keep"`. The
   trajectory view asks for it -- its bars are how the reader says where in a
   session they are and how they get somewhere else, and a bar that fades out
   after the last scroll is not there when it is wanted. */
const keeps = (el: HTMLElement): boolean => el.dataset.thumb === 'keep'

export function show(el: HTMLElement): void {
  /* Not on a one-line field. A text input scrolls itself to the caret as you
     type -- there is no thumb to reach for -- and the bar landed across the
     bottom of a box one line tall, 6px of it inside 27px, which is what a
     long conversation name being renamed looked like. A textarea is a real
     scroller and keeps its bar. */
  if (el.tagName === 'INPUT') return
  /* Nor on a scroller that declares it wants none: one that is its own
     indicator of position (the transcript's turn nav keeps the mark being
     read in view) would carry a second indicator saying the same thing. */
  if (el.dataset.thumb === 'none') return
  const b = bars(el)
  for (const a of AXES) {
    const t = place(el, b, a)
    if (t) t.dataset.on = 'true'
  }
  live.add(el)
  clearTimeout(b.timer)
  if (keeps(el)) return
  b.timer = setTimeout(() => {
    /* A bar being dragged must not time out from under the pointer. */
    if (b.drag) return
    if (!el.isConnected) {
      hide(el)
      return
    }
    for (const a of AXES) {
      const t = b[a]
      if (t) t.dataset.on = 'false'
    }
    live.delete(el)
  }, SB_HIDE)
}

export function sync(skip?: HTMLElement): void {
  live.forEach((el) => {
    if (el === skip) return
    if (!el.isConnected) {
      hide(el)
      return
    }
    const b = bars(el)
    for (const a of AXES) if (b[a]) place(el, b, a)
  })
}

export function hide(el: HTMLElement): void {
  const b = sbMap.get(el)
  live.delete(el)
  if (!b) return
  clearTimeout(b.timer)
  for (const a of AXES) drop(el, b, a)
}

/* The native bar is gone, so the thumb has to be draggable itself or scrolling
   by grabbing the bar -- the one gesture a trackpad cannot do -- would be lost.

   The grabbed point of the bar stays under the pointer, and the geometry is
   read again on every move: the list behind a message pane measures its rows
   while the reader scrolls, so the content's height can change under the bar,
   and a ratio taken at pointerdown would then send the content to a place the
   pointer never pointed at. The moves are heard on the window rather than on
   the thumb: a 6px bar is easy to leave, and pointer capture -- which a drag
   may not depend on -- is then only an optimisation. */
function wireDrag(t: HTMLElement, el: HTMLElement, axis: Axis): void {
  if (dragWired.has(t)) return
  dragWired.add(t)
  t.addEventListener('pointerdown', (e) => {
    e.preventDefault()
    e.stopPropagation()
    const start = geometry(el, axis)
    if (!start) return
    const pointer = start.vert ? e.clientY : e.clientX
    /* Where the bar stands, by the same arithmetic place() drew it with: the
       rect of a thumb parked in the fixed layer is one frame behind at best. */
    const pos = (start.vert ? el.scrollTop : el.scrollLeft) / (start.full - start.size)
    const shown = Math.round((start.track - start.len) * Math.min(1, Math.max(0, pos)))
    const grab = Math.max(0, Math.min(start.len, pointer - (start.from + shown)))
    const b = bars(el)
    b.drag = true
    t.dataset.drag = 'true'
    /* A pointer can be gone by the time this runs (released mid-dispatch, or a
       synthetic event); capture is an optimisation, not the drag itself. */
    try {
      t.setPointerCapture(e.pointerId)
    } catch {
      /* not capturable */
    }
    document.body.style.userSelect = 'none'
    const move = (ev: PointerEvent): void => {
      const geo = geometry(el, axis) ?? start
      const at = geo.vert ? ev.clientY : ev.clientX
      const free = Math.max(1, geo.track - geo.len)
      const off = Math.min(free, Math.max(0, at - grab - geo.from))
      const to = (off / free) * (geo.full - geo.size)
      if (geo.vert) el.scrollTop = to
      else el.scrollLeft = to
    }
    const up = (): void => {
      window.removeEventListener('pointermove', move, true)
      window.removeEventListener('pointerup', up, true)
      window.removeEventListener('pointercancel', up, true)
      delete t.dataset.drag
      b.drag = false
      document.body.style.userSelect = ''
      show(el)
    }
    window.addEventListener('pointermove', move, true)
    window.addEventListener('pointerup', up, true)
    window.addEventListener('pointercancel', up, true)
  })
}

/* The capture-phase scroll, which is how one handler covers every scroller in
   the app. Registered with the page's other document listeners
   (state/globalListeners.ts). */
export function onScroll(e: Event): void {
  const el = root(e.target)
  if (!el) return
  show(el)
  sync(el)
}

/* Resizing moves every box at once, and a bar mid-fade would be left hanging
   over whatever landed under it. */
export function onResize(): void {
  live.forEach((el) => {
    /* A bar that stays only has to be placed again from its new box; one that
       fades is taken down and comes back with the next scroll. */
    if (keeps(el)) {
      const b = bars(el)
      for (const a of AXES) if (b[a]) place(el, b, a)
      return
    }
    hide(el)
  })
}

/** Raises the layer, so the thumbs have somewhere to be parked. */
export function install(): void {
  layer()
}
