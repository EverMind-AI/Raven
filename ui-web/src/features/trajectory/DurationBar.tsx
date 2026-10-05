/* The duration bar: every entry of the conversation as a block in one row,
 * as wide as what it is charged, with the zoom, the drag, the hover and the
 * click that make it a way of moving through the list.
 *
 * The arithmetic is geometry.ts's and the state is the list store's
 * (`timeline`); what is here is the canvas that draws a layout, the events
 * that turn into moves on the viewport, and the lifecycle that keeps a
 * delayed action from landing in a conversation the reader has left.
 *
 * Three rules on timing:
 *   - a click waits a quarter of a second for a second click, and the wait
 *     carries what it is about -- the entry or the dense block's members,
 *     and the list's generation, conversation and view at the time. When it
 *     fires it checks all of that and the target's presence, and does
 *     nothing if any changed. A new click replaces the wait; a double click
 *     cancels it; a drag never starts one.
 *   - a pointer that moves more than a few pixels is a drag, which pans the
 *     view and ends without a click; a cancelled or lost pointer ends it the
 *     same way.
 *   - the conversation, the view, the switch or the epoch changing under the
 *     bar clears the wait, the hover, the drag and the dense block's pick
 *     list, and so does unmounting.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import * as details from './detailStore'
import {
  BAR_H, DBL_MS, DRAG_PX, GAP, MIN_W, expand, hitTest, layoutFor, pan, restoreAnchor, summarize, toSegments, zoomAt,
} from './geometry'
import { BarHover } from './Hover'
import { kindClass, kindLabel, kindSlug } from './palette'
import * as store from './store'

import type { Block, Layout, Segment, Viewport } from './geometry'
import type { HoverAt } from './Hover'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

/* A wheel notch's zoom, kept within a halving and a doubling per event. */
const wheelFactor = (deltaY: number): number => Math.max(0.5, Math.min(2, Math.exp(-deltaY * 0.0015)))

/* The world the bar was in when a gesture began. A delayed action compares
   this against the world when it fires. */
interface Captured {
  gen: number
  sessionKey: string | null
}

const captured = (): Captured => ({ gen: store.gen(), sessionKey: store.get().sessionKey })

const stillValid = (c: Captured): boolean => {
  const s = store.get()
  return c.gen === store.gen() && c.sessionKey === s.sessionKey && s.view === 'trajectory' && store.available(s) && s.snapshotReady
}

interface Pending {
  timer: ReturnType<typeof setTimeout>
  block: Block
  x: number
  world: Captured
}

interface Drag {
  pointerId: number
  startX: number
  lastX: number
  moved: boolean
  world: Captured
}

/* The CSS variables the canvas paints with, read once per frame; happy-dom
   answers nothing, and the fallbacks keep the bar legible there too. */
function paintColors(): { line: string; selected: string; failure: string; faint: string; kind: (kind: string) => string } {
  const style = typeof getComputedStyle === 'function' ? getComputedStyle(document.documentElement) : null
  const read = (name: string, fallback: string): string => {
    const v = style?.getPropertyValue(name).trim()
    return v ? v : fallback
  }
  return {
    line: read('--line', '#e7e7e7'),
    selected: read('--amber', '#a8801c'),
    failure: read('--chat-danger', read('--clay', '#b4402f')),
    faint: read('--faint', '#9d9d9d'),
    kind: (kind) => read(`--trajectory-c-${kindSlug(kind)}-fg`, '#5c5c5c'),
  }
}

/* Draws the visible part of a layout. Exported for the test that drives it
   against a recording context. */
export function paint(ctx: CanvasRenderingContext2D, layout: Layout, view: Viewport, selectedId: string | null, bySegment: ReadonlyMap<string, Segment>, dpr: number): void {
  const colors = paintColors()
  ctx.save()
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, view.width, BAR_H)
  const left = view.offset
  const right = view.offset + view.width
  const top = 6
  const h = BAR_H - 12
  for (const b of layout.blocks) {
    if (b.x + b.w < left || b.x > right) continue
    const x = b.x - left
    ctx.fillStyle = colors.kind(b.kind)
    if (b.ids) {
      /* A dense block: hatched, so it reads as several rather than one. */
      ctx.fillRect(x, top, b.w, h)
      ctx.fillStyle = 'rgba(255,255,255,0.35)'
      for (let s = x - h; s < x + b.w; s += 4) {
        ctx.beginPath()
        ctx.moveTo(Math.max(x, s), top)
        ctx.lineTo(Math.min(x + b.w, s + h), top + h)
        ctx.lineWidth = 1
        ctx.strokeStyle = 'rgba(255,255,255,0.35)'
        ctx.stroke()
      }
    } else {
      const segment = bySegment.get(b.id)
      const mark = segment !== undefined && !(segment.charged !== null && segment.charged > 0)
      if (mark) {
        /* A zero or unknown duration: the mark stays the minimum width,
           centred in a slot that may have grown with the zoom. */
        ctx.fillRect(x + (b.w - MIN_W) / 2, top, MIN_W, h)
      } else {
        ctx.fillRect(x, top, b.w, h)
      }
      if (segment?.failure) {
        ctx.fillStyle = colors.failure
        ctx.beginPath()
        ctx.arc(x + b.w / 2, top - 2, 2, 0, Math.PI * 2)
        ctx.fill()
      }
    }
    const selected = selectedId !== null && (b.id === selectedId || (b.ids?.includes(selectedId) ?? false))
    if (selected) {
      ctx.strokeStyle = colors.selected
      ctx.lineWidth = 2
      ctx.strokeRect(x + 1, top + 1, Math.max(1, b.w - 2), h - 2)
    }
  }
  ctx.strokeStyle = colors.line
  ctx.lineWidth = 1
  for (const d of layout.dividers) {
    if (d < left || d > right) continue
    const x = Math.round(d - left) + 0.5
    ctx.beginPath()
    ctx.moveTo(x, 2)
    ctx.lineTo(x, BAR_H - 2)
    ctx.stroke()
  }
  if (!view.fit && layout.contentWidth > view.width) {
    /* The scroll indicator: where the viewport sits in the content. */
    const frac = view.width / layout.contentWidth
    ctx.fillStyle = colors.faint
    ctx.fillRect((view.offset / layout.contentWidth) * view.width, 0, Math.max(8, frac * view.width), 1)
  }
  ctx.restore()
}

/* The dense block's pick list: its members as options, and the way to spread
   the block out instead. Escape reaches it through the trajectory layer in
   the page's Escape order (detailStore.ts), so a sheet or a popover above it
   is taken back first. */
function Bucket({ ids, left, top, onPick, onExpand }: {
  ids: string[]; left: number; top: number; onPick: (id: string) => void; onExpand: () => void
}): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  const s = useSyncExternalStore(store.subscribe, store.get)
  useLayoutEffect(() => {
    box.current?.querySelector<HTMLElement>('[role="option"]')?.focus()
  }, [])
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const options = [...(box.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])]
    const at = options.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'ArrowDown' ? Math.min(options.length - 1, at + 1) : Math.max(0, at - 1)
    options[next]?.focus()
  }
  return (
    <div className="trajectory-bucket" ref={box} role="listbox" aria-label={t('gui.trajectory.bar.pick_one')} style={{ left, top }} onKeyDown={onKeyDown}>
      <div className="trajectory-bucket-head">
        <span>{t('gui.trajectory.bar.pick_one')}</span>
        <button className="trajectory-link" onClick={onExpand}>{t('gui.trajectory.bar.expand')}</button>
      </div>
      <div className="trajectory-bucket-list">
        {ids.map((id) => {
          const entry = store.entry(id)
          if (!entry) return null
          return (
            <button key={id} className="trajectory-bucket-item" role="option" aria-selected={s.selectedId === id} onClick={() => onPick(id)}>
              <span className={`trajectory-tag ${kindClass(entry.kind)}`}>{kindLabel(entry.kind)}</span>
              <span className="trajectory-bucket-text">{entry.preview ?? t('gui.trajectory.no_preview')}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

export function DurationBar(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const { entries, selectedId, timeline } = s
  const root = useRef<HTMLDivElement | null>(null)
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const [width, setWidth] = useState(0)
  const [hover, setHover] = useState<HoverAt | null>(null)
  const [theme, setTheme] = useState(0)
  const pending = useRef<Pending | null>(null)
  const drag = useRef<Drag | null>(null)

  const segments = useMemo(() => toSegments(entries), [entries])
  const bySegment = useMemo(() => new Map(segments.map((seg) => [seg.id, seg])), [segments])
  const view: Viewport = useMemo(() => ({ ...timeline, width }), [timeline, width])
  const layout = useMemo(() => layoutFor(segments, view), [segments, view])
  const sum = useMemo(() => summarize(segments), [segments])

  /* The bar's width, from the box as it is and again whenever it changes. */
  const attach = useCallback((el: HTMLDivElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    root.current = el
    if (!el) return
    const measure = (): void => { setWidth(el.clientWidth) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])

  /* The theme: colours are read at paint time, so a change only needs a repaint. */
  useEffect(() => {
    if (typeof MutationObserver === 'undefined') return
    const mo = new MutationObserver(() => { setTheme((n) => n + 1) })
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => mo.disconnect()
  }, [])

  /* Rows changed under a zoomed view: the anchored place stays where it was,
     and a view that had no unit to freeze takes one from the first duration.
     On the rows and the width only -- a move of the view is the reader's,
     and re-anchoring it to itself would chase rounding forever. */
  const latest = useRef({ view, layout })
  latest.current = { view, layout }
  useLayoutEffect(() => {
    const { view: v, layout: l } = latest.current
    if (v.fit || v.width < MIN_W) return
    const restored = restoreAnchor(l, v)
    const moved = Math.abs(restored.offset - v.offset) > 1e-6 || restored.frozenUnit !== v.frozenUnit
    if (moved) store.setTimeline({ offset: restored.offset, frozenUnit: restored.frozenUnit })
  }, [segments, width])

  /* Paint, once per frame at most. */
  useLayoutEffect(() => {
    const el = canvas.current
    if (!el || width < MIN_W) return
    let frame = 0
    frame = requestAnimationFrame(() => {
      const dpr = typeof devicePixelRatio === 'number' && devicePixelRatio > 0 ? devicePixelRatio : 1
      if (el.width !== Math.round(width * dpr)) el.width = Math.round(width * dpr)
      if (el.height !== Math.round(BAR_H * dpr)) el.height = Math.round(BAR_H * dpr)
      const ctx = el.getContext('2d')
      if (ctx) paint(ctx, layout, view, selectedId, bySegment, dpr)
    })
    return () => cancelAnimationFrame(frame)
  }, [layout, view, selectedId, bySegment, width, theme])

  /* The wheel zooms, about the pointer, and only over the canvas: a native
     listener, because React's own is passive and could not keep the page
     from scrolling. */
  const viewRef = useRef(view)
  viewRef.current = view
  const segmentsRef = useRef(segments)
  segmentsRef.current = segments
  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const onWheel = (e: WheelEvent): void => {
      if (viewRef.current.width < MIN_W || !segmentsRef.current.length) return
      e.preventDefault()
      const x = e.clientX - el.getBoundingClientRect().left
      const next = zoomAt(viewRef.current, x, wheelFactor(e.deltaY), segmentsRef.current)
      store.setTimeline({ scale: next.scale, offset: next.offset, fit: next.fit, frozenUnit: next.frozenUnit, anchor: next.anchor })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const clearPending = useCallback((): void => {
    if (pending.current) clearTimeout(pending.current.timer)
    pending.current = null
  }, [])

  const endDrag = useCallback((): void => {
    const d = drag.current
    drag.current = null
    if (d && canvas.current?.hasPointerCapture?.(d.pointerId)) canvas.current.releasePointerCapture(d.pointerId)
    if (root.current) delete root.current.dataset.drag
  }, [])

  /* The world moving under the bar -- another conversation, the conversation
     view, the switch off, a rebuilt index -- takes every gesture in progress
     with it; so does unmounting. */
  useEffect(() => {
    let seen = { sessionKey: store.get().sessionKey, view: store.get().view, epoch: store.get().epoch, available: store.available() }
    const unsubscribe = store.subscribe(() => {
      const now = { sessionKey: store.get().sessionKey, view: store.get().view, epoch: store.get().epoch, available: store.available() }
      const changed = now.sessionKey !== seen.sessionKey || now.view !== seen.view || now.epoch !== seen.epoch || now.available !== seen.available
      seen = now
      if (!changed) return
      clearPending()
      endDrag()
      setHover(null)
      store.closeBucket()
    })
    return () => {
      unsubscribe()
      clearPending()
      endDrag()
      store.closeBucket()
    }
  }, [clearPending, endDrag])

  /* The hover follows the rows: a block whose entry left is no longer anything to describe. */
  useEffect(() => {
    if (!hover) return
    const alive = hover.block.ids ? hover.block.ids.some((id) => id in s.index) : hover.block.id in s.index
    if (!alive) setHover(null)
  }, [hover, s.index])

  const contentX = (clientX: number): number => {
    const rect = canvas.current?.getBoundingClientRect()
    return clientX - (rect?.left ?? 0) + view.offset
  }

  /* The delayed click, fired with the world it was made in. */
  const fire = (p: Pending): void => {
    pending.current = null
    if (!stillValid(p.world)) return
    const index = store.get().index
    if (p.block.ids) {
      const alive = p.block.ids.filter((id) => id in index)
      if (alive.length) store.openBucket(alive, p.x)
      return
    }
    if (!(p.block.id in index)) return
    store.select(p.block.id, { source: 'bar' })
    details.openDetails()
  }

  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>): void => {
    if (e.button !== 0 || width < MIN_W || !segments.length) return
    drag.current = { pointerId: e.pointerId, startX: e.clientX, lastX: e.clientX, moved: false, world: captured() }
  }

  const onPointerMove = (e: ReactPointerEvent<HTMLCanvasElement>): void => {
    const d = drag.current
    if (d && d.pointerId === e.pointerId) {
      if (!d.moved && Math.abs(e.clientX - d.startX) > DRAG_PX && !view.fit) {
        d.moved = true
        e.currentTarget.setPointerCapture(e.pointerId)
        if (root.current) root.current.dataset.drag = 'true'
        setHover(null)
      }
      if (d.moved) {
        const next = pan(view, e.clientX - d.lastX, layout.contentWidth)
        d.lastX = e.clientX
        if (next.offset !== view.offset) store.setTimeline({ offset: next.offset })
        return
      }
    }
    if (width < MIN_W) return
    const block = hitTest(layout, contentX(e.clientX))
    setHover(block ? { block, clientX: e.clientX } : null)
  }

  const onPointerUp = (e: ReactPointerEvent<HTMLCanvasElement>): void => {
    const d = drag.current
    if (!d || d.pointerId !== e.pointerId) return
    endDrag()
    if (d.moved) return
    const block = hitTest(layout, contentX(e.clientX))
    if (!block) return
    clearPending()
    const world = captured()
    const x = block.x
    const timer = setTimeout(() => { if (pending.current) fire(pending.current) }, DBL_MS)
    pending.current = { timer, block, x, world }
  }

  const onPointerCancel = (): void => { endDrag() }

  const onDoubleClick = (): void => {
    clearPending()
    store.setTimeline({ ...store.initialTimeline, bucket: null })
  }

  const zoomBy = (factor: number): void => {
    if (width < MIN_W || !segments.length) return
    const next = zoomAt(view, width / 2, factor, segments)
    store.setTimeline({ scale: next.scale, offset: next.offset, fit: next.fit, frozenUnit: next.frozenUnit, anchor: next.anchor })
  }

  const bucket = timeline.bucket
  const bucketPlace = (): { left: number; top: number } => {
    const rect = canvas.current?.getBoundingClientRect()
    const left = (rect?.left ?? 0) + (bucket ? bucket.x - view.offset : 0)
    const top = (rect?.bottom ?? BAR_H) + 4
    const vw = typeof document !== 'undefined' ? document.documentElement.clientWidth || 1000 : 1000
    return { left: Math.max(4, Math.min(vw - 324, left)), top }
  }

  const onPick = (id: string): void => {
    store.closeBucket()
    if (!(id in store.get().index)) return
    store.select(id, { source: 'bar' })
    details.openDetails()
    root.current?.focus()
  }

  const onExpand = (): void => {
    if (!bucket) return
    const block = layout.blocks.find((b) => b.ids && b.ids[0] === bucket.ids[0]) ?? layout.blocks.find((b) => b.ids?.includes(bucket.ids[0]!))
    store.closeBucket()
    if (!block) return
    const next = expand(view, block, segments)
    store.setTimeline({ scale: next.scale, offset: next.offset, fit: next.fit, frozenUnit: next.frozenUnit, anchor: next.anchor })
    root.current?.focus()
  }

  /* When the list closes for any other reason -- Escape through the page's
     order, the world moving -- the focus comes back here. */
  const hadBucket = useRef(bucket !== null)
  useEffect(() => {
    if (hadBucket.current && bucket === null) root.current?.focus()
    hadBucket.current = bucket !== null
  }, [bucket])

  const label = t('gui.trajectory.bar.canvas_label', { n: entries.length, known: formatDuration(sum.known) })
  const rect = canvas.current?.getBoundingClientRect()
  return (
    <div className="trajectory-bar" ref={attach} tabIndex={-1} data-fit={timeline.fit ? '' : undefined}>
      <canvas
        className="trajectory-canvas"
        ref={canvas}
        role="img"
        aria-label={label}
        style={{ width: '100%', height: BAR_H }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onLostPointerCapture={onPointerCancel}
        onPointerLeave={() => setHover(null)}
        onDoubleClick={onDoubleClick}
      />
      <div className="trajectory-bar-sum">
        <span>{t('gui.trajectory.bar.sum_known', { dur: formatDuration(sum.known) })}</span>
        {sum.unknownCount > 0 ? <span>{t('gui.trajectory.bar.sum_unknown', { n: sum.unknownCount })}</span> : null}
        {sum.overlap ? <span>{t('gui.trajectory.bar.sum_overlap')}</span> : null}
      </div>
      <div className="trajectory-bar-tools" role="group">
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.zoom_in')} title={t('gui.trajectory.bar.zoom_in')} onClick={() => zoomBy(2)}>+</button>
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.zoom_out')} title={t('gui.trajectory.bar.zoom_out')} onClick={() => zoomBy(0.5)} disabled={timeline.fit}>{'−'}</button>
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.reset')} title={t('gui.trajectory.bar.reset')} onClick={onDoubleClick} disabled={timeline.fit}>{'⤢'}</button>
      </div>
      {hover && rect && !bucket ? (
        <BarHover
          at={hover}
          bySegment={bySegment}
          bar={rect}
          size={{ width: 320, height: 64 }}
          viewport={{ width: document.documentElement.clientWidth || 1000, height: document.documentElement.clientHeight || 800 }}
        />
      ) : null}
      {bucket ? <Bucket ids={bucket.ids} {...bucketPlace()} onPick={onPick} onExpand={onExpand} /> : null}
    </div>
  )
}

/* Exposed for tests that drive the gap arithmetic directly. */
export { GAP }
