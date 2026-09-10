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
 * module the way it was a plain block of the legacy shell.
 *
 * SB_HIDE is how long a bar stays after the last scroll event. Long enough to
 * read where you are in a long transcript, short enough that it is gone before
 * you look at the layout again.
 */

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
/* Which thumbs already carry their pointerdown listener. An expando on the
   node in the legacy shell; a WeakSet says the same thing in types. */
const dragWired = new WeakSet<HTMLElement>()

/* Whoever is showing a bar right now. A scroller that moved without being
   scrolled -- because an ancestor scrolled, or a panel next to it was dragged
   wider -- still has to have its thumb put back where the box now is. */
const live = new Set<HTMLElement>()

let sbLayer: HTMLElement | null = null

function layer(): HTMLElement {
  /* The isConnected half is for a test that replaced the body under us; in
     the page the layer is appended once and never removed. */
  if (!sbLayer || !sbLayer.isConnected) {
    sbLayer = document.createElement('div')
    sbLayer.className = 'sbars'
    document.body.appendChild(sbLayer)
  }
  return sbLayer
}

/* The scroll event from the document, the documentElement and the window all
   mean the same scroller. */
export function root(target: EventTarget | null): HTMLElement | null {
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

function thumb(b: Bars, axis: Axis): HTMLElement {
  const had = b[axis]
  if (had) return had
  const t = document.createElement('div')
  t.className = 'sbar'
  t.dataset.axis = axis
  layer().appendChild(t)
  b[axis] = t
  return t
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

function place(el: HTMLElement, b: Bars, axis: Axis): HTMLElement | undefined {
  const vert = axis === 'v'
  const size = vert ? el.clientHeight : el.clientWidth
  const full = vert ? el.scrollHeight : el.scrollWidth
  if (!el.isConnected || full <= size + 1 || size < 40) {
    const gone = b[axis]
    if (gone) {
      gone.remove()
      b[axis] = null
    }
    return
  }
  const r = el.getBoundingClientRect()
  if (!r.width || !r.height) return
  /* The track is the part of the box on screen, not the whole box: a scroller
     inside another scroller can be scrolled halfway out of it, and a bar laid
     out over the whole box is then drawn past the edge of the outer one -- over
     the settings dialog's header, in the case that found this. Measured inside
     the visible slice, the bar stays in the slice and still says where in the
     content the reader is. */
  const box = visible(el)
  if (!box) {
    const gone = b[axis]
    if (gone) {
      gone.remove()
      b[axis] = null
    }
    return
  }
  const t = thumb(b, axis)
  const from = (vert ? Math.max(r.top, box.top) : Math.max(r.left, box.left)) + SB_PAD
  const to = (vert ? Math.min(r.bottom, box.bottom) : Math.min(r.right, box.right)) - SB_PAD
  const track = to - from
  if (track < SB_PAD * 2) return
  const len = Math.min(track, Math.max(SB_MIN, Math.round(track * (size / full))))
  const pos = (vert ? el.scrollTop : el.scrollLeft) / (full - size)
  const off = Math.round((track - len) * Math.min(1, Math.max(0, pos)))
  if (vert) {
    t.style.cssText = `top:${from + off}px;left:${Math.min(r.right, box.right) - 8}px;width:6px;height:${len}px`
  } else {
    t.style.cssText = `left:${from + off}px;top:${Math.min(r.bottom, box.bottom) - 8}px;height:6px;width:${len}px`
  }
  wireDrag(t, el, axis)
  return t
}

export function show(el: HTMLElement): void {
  const b = bars(el)
  for (const a of AXES) {
    const t = place(el, b, a)
    if (t) t.dataset.on = 'true'
  }
  live.add(el)
  clearTimeout(b.timer)
  b.timer = setTimeout(() => {
    /* A bar being dragged must not time out from under the pointer. */
    if (b.drag) return
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
  for (const a of AXES) {
    const t = b[a]
    if (t) {
      t.remove()
      b[a] = null
    }
  }
}

/* The native bar is gone, so the thumb has to be draggable itself or scrolling
   by grabbing the bar -- the one gesture a trackpad cannot do -- would be lost. */
function wireDrag(t: HTMLElement, el: HTMLElement, axis: Axis): void {
  if (dragWired.has(t)) return
  dragWired.add(t)
  t.addEventListener('pointerdown', (e) => {
    e.preventDefault()
    e.stopPropagation()
    const vert = axis === 'v'
    const b = bars(el)
    const start = vert ? e.clientY : e.clientX
    const from = vert ? el.scrollTop : el.scrollLeft
    const size = vert ? el.clientHeight : el.clientWidth
    const full = vert ? el.scrollHeight : el.scrollWidth
    const track = (vert ? el.clientHeight : el.clientWidth) - SB_PAD * 2
    const len = Math.max(SB_MIN, track * (size / full))
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
      const d = (vert ? ev.clientY : ev.clientX) - start
      const ratio = (full - size) / Math.max(1, track - len)
      if (vert) el.scrollTop = from + d * ratio
      else el.scrollLeft = from + d * ratio
    }
    const up = (): void => {
      t.removeEventListener('pointermove', move)
      t.removeEventListener('pointerup', up)
      t.removeEventListener('pointercancel', up)
      delete t.dataset.drag
      b.drag = false
      document.body.style.userSelect = ''
      show(el)
    }
    t.addEventListener('pointermove', move)
    t.addEventListener('pointerup', up)
    t.addEventListener('pointercancel', up)
  })
}

function onScroll(e: Event): void {
  const el = root(e.target)
  if (!el) return
  show(el)
  sync(el)
}

/* Resizing moves every box at once, and a bar mid-fade would be left hanging
   over whatever landed under it. */
function onResize(): void {
  live.forEach(hide)
}

export function install(): void {
  layer()
  document.addEventListener('scroll', onScroll, true)
  window.addEventListener('resize', onResize)
}
