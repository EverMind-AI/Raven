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
  BAR_H, BLOCK_H, BLOCK_TOP, DBL_MS, DOT_ABOVE_Y, DOT_BELOW_Y, DRAG_PX, GAP, MIN_W, anchorOf, bandAt, bandsFor, barEntries, expand,
  hitTestExact, layoutFor, pan, restoreAnchor, summarize, toSegments, zoomAt,
} from './geometry'
import { BandHover, BarHover, BarSummary, summaryText } from './Hover'
import { kindClass, kindLabel, kindSlug } from './palette'
import * as store from './store'

import type { Band, Block, Layout, Segment, Viewport } from './geometry'
import type { HoverAt } from './Hover'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

/* A wheel notch's zoom, kept within a halving and a doubling per event. */
const wheelFactor = (deltaY: number): number => Math.max(0.5, Math.min(2, Math.exp(-deltaY * 0.0015)))

/** The dense block's pick list: its width, and the most height it takes. */
export const BUCKET_W = 320
export const BUCKET_MAX_H = 280

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
function paintColors(): { line: string; band: string; selected: string; failure: string; faint: string; kind: (kind: string) => string } {
  const style = typeof getComputedStyle === 'function' ? getComputedStyle(document.documentElement) : null
  const read = (name: string, fallback: string): string => {
    const v = style?.getPropertyValue(name).trim()
    return v ? v : fallback
  }
  return {
    line: read('--line', '#e7e7e7'),
    band: read('--line-soft', read('--raised', '#f0f0f0')),
    selected: read('--moss', '#3f7d2c'),
    failure: read('--chat-danger', read('--clay', '#b4402f')),
    faint: read('--faint', '#9d9d9d'),
    kind: (kind) => read(`--trajectory-c-${kindSlug(kind)}-fg`, '#5c5c5c'),
  }
}

/* Draws the visible part of a layout: the turn bands first, then every block
   at half the row's height, a green dot over the block holding the selected
   entry and a red dot under a failed one. Exported for the test that drives
   it against a recording context. */
export function paint(
  ctx: CanvasRenderingContext2D, layout: Layout, view: Viewport, selectedId: string | null,
  bySegment: ReadonlyMap<string, Segment>, dpr: number, bands: readonly Band[] = [],
): void {
  const colors = paintColors()
  ctx.save()
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, view.width, BAR_H)
  const left = view.offset
  const right = view.offset + view.width
  const top = BLOCK_TOP
  const h = BLOCK_H
  ctx.fillStyle = colors.band
  for (const band of bands) {
    if (band.x1 < left || band.x0 > right) continue
    ctx.fillRect(band.x0 - left, top - 2, band.x1 - band.x0, h + 4)
  }
  for (const b of layout.blocks) {
    if (b.x + b.w < left || b.x > right) continue
    const x = b.x - left
    ctx.fillStyle = colors.kind(b.kind)
    let failed = false
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
      failed = b.failure
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
      failed = segment?.failure ?? false
    }
    const cx = x + b.w / 2
    if (failed) {
      ctx.fillStyle = colors.failure
      ctx.beginPath()
      ctx.arc(cx, DOT_BELOW_Y, 2.5, 0, Math.PI * 2)
      ctx.fill()
    }
    const selected = selectedId !== null && (b.id === selectedId || (b.ids?.includes(selectedId) ?? false))
    if (selected) {
      ctx.fillStyle = colors.selected
      ctx.beginPath()
      ctx.arc(cx, DOT_ABOVE_Y, 2.5, 0, Math.PI * 2)
      ctx.fill()
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
function Bucket({ ids, left, top, maxHeight, onPick, onExpand }: {
  ids: string[]; left: number; top: number; maxHeight: number; onPick: (id: string) => void; onExpand: () => void
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
    <div className="trajectory-bucket" ref={box} role="listbox" aria-label={t('gui.trajectory.bar.pick_one')} style={{ left, top, maxHeight }} onKeyDown={onKeyDown}>
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

/* What the pointer is over besides a block: a turn's band, or the bare bar. */
type Over = { kind: 'band'; band: Band; clientX: number } | { kind: 'summary'; clientX: number }

export function DurationBar(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const { selectedId, timeline, prefs } = s
  /* The bar draws the visible rows, less the short ones while that switch is on. */
  const entries = useMemo(() => barEntries(s.visible, prefs.hideShort), [s.visible, prefs.hideShort])
  /* The turns whose reply the list leaves out keep their time as a band. */
  const hiddenTurns = useMemo(() => {
    const out = new Map<number, number>()
    for (const e of s.entries) {
      if (e.slot !== 'turn.output' || store.hiddenOf(e) === null || e.entry_id in s.visibleIndex) continue
      if (typeof e.turn_number === 'number' && typeof e.charged_ms === 'number') out.set(e.turn_number, e.charged_ms)
    }
    return out
  }, [s.entries, s.visibleIndex])
  const root = useRef<HTMLDivElement | null>(null)
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const [width, setWidth] = useState(0)
  const [hover, setHover] = useState<HoverAt | null>(null)
  const [over, setOver] = useState<Over | null>(null)
  const [theme, setTheme] = useState(0)
  const pending = useRef<Pending | null>(null)
  const drag = useRef<Drag | null>(null)

  const segments = useMemo(() => toSegments(entries), [entries])
  const bySegment = useMemo(() => new Map(segments.map((seg) => [seg.id, seg])), [segments])
  const view: Viewport = useMemo(() => ({ ...timeline, width }), [timeline, width])
  const layout = useMemo(() => layoutFor(segments, view), [segments, view])
  const sum = useMemo(() => summarize(segments), [segments])
  const bands = useMemo(() => bandsFor(layout, bySegment, hiddenTurns), [layout, bySegment, hiddenTurns])

  /* The canvas's own width -- not the bar's, which also holds the sum and
     the tools -- measured as it is and again whenever it changes, so the
     layout, the hit test and the zoom all speak in the pixels the canvas is
     drawn at. A longer sum narrows the canvas, and the observer sees that. */
  const attachCanvas = useCallback((el: HTMLCanvasElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    canvas.current = el
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
      if (ctx) paint(ctx, layout, view, selectedId, bySegment, dpr, bands)
    })
    return () => cancelAnimationFrame(frame)
  }, [layout, view, selectedId, bySegment, width, theme, bands])

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
      setOver(null)
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

  /* What is under the pointer: a block, else a band, else the bar itself. */
  const describe = (clientX: number): void => {
    const x = contentX(clientX)
    const block = hitTestExact(layout, x)
    if (block) { setHover({ block, clientX }); setOver(null); return }
    setHover(null)
    const band = bandAt(bands, x)
    setOver(band ? { kind: 'band', band, clientX } : { kind: 'summary', clientX })
  }

  /* A click on a turn's band lands on the turn's first visible entry. */
  const selectBand = (band: Band): void => {
    const first = entries.find((e) => e.turn_number === band.turn)
    if (first) store.select(first.entry_id, { source: 'bar' })
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
        setOver(null)
      }
      if (d.moved) {
        const next = pan(view, e.clientX - d.lastX, layout.contentWidth)
        d.lastX = e.clientX
        /* The anchor moves with the view: it is what rows arriving later
           are restored against, and a stale one would undo the drag. */
        if (next.offset !== view.offset) store.setTimeline({ offset: next.offset, anchor: anchorOf(layout, next) })
        return
      }
    }
    if (width < MIN_W) return
    describe(e.clientX)
  }

  const onPointerUp = (e: ReactPointerEvent<HTMLCanvasElement>): void => {
    const d = drag.current
    if (!d || d.pointerId !== e.pointerId) return
    endDrag()
    if (d.moved) return
    const at = contentX(e.clientX)
    const block = hitTestExact(layout, at)
    if (!block) {
      const band = bandAt(bands, at)
      if (band) selectBand(band)
      return
    }
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
  /* Below the bar when the window has room for the list there, above it
     otherwise -- the bar sits at the bottom of the column, so above is the
     usual case -- and never wider than the window. */
  const bucketPlace = (): { left: number; top: number; maxHeight: number } => {
    const rect = canvas.current?.getBoundingClientRect()
    const vw = document.documentElement.clientWidth || 1000
    const vh = document.documentElement.clientHeight || 800
    const left = (rect?.left ?? 0) + (bucket ? bucket.x - view.offset : 0)
    const below = (rect?.bottom ?? BAR_H) + 4
    const above = (rect?.top ?? 0) - 4
    const roomBelow = vh - 4 - below
    const roomAbove = above - 4
    const wantHeight = Math.min(BUCKET_MAX_H, Math.max(roomBelow, roomAbove))
    const top = roomBelow >= wantHeight ? below : Math.max(4, above - wantHeight)
    return { left: Math.max(4, Math.min(vw - BUCKET_W - 4, left)), top, maxHeight: wantHeight }
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
    <div className="trajectory-bar" ref={root} tabIndex={-1} data-fit={timeline.fit ? '' : undefined}>
      <canvas
        className="trajectory-canvas"
        ref={attachCanvas}
        role="img"
        aria-label={label}
        style={{ width: '100%', height: BAR_H }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onLostPointerCapture={onPointerCancel}
        onPointerLeave={() => { setHover(null); setOver(null) }}
        onDoubleClick={onDoubleClick}
      />
      <div className="trajectory-bar-tools" role="group" title={summaryText(sum)}>
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.zoom_in')} title={t('gui.trajectory.bar.zoom_in')} onClick={() => zoomBy(2)}>+</button>
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.zoom_out')} title={t('gui.trajectory.bar.zoom_out')} onClick={() => zoomBy(0.5)} disabled={timeline.fit}>{'−'}</button>
        <button className="trajectory-bar-tool" aria-label={t('gui.trajectory.bar.reset')} title={t('gui.trajectory.bar.reset')} onClick={onDoubleClick} disabled={timeline.fit}>{'⤢'}</button>
        <button
          className="trajectory-bar-tool trajectory-bar-switch"
          aria-label={t('gui.trajectory.bar.hide_short')}
          title={t('gui.trajectory.bar.hide_short')}
          aria-pressed={prefs.hideShort}
          onClick={() => store.setPrefs({ hideShort: !prefs.hideShort })}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M3 5h18l-7 8v6l-4 2v-8z" />
          </svg>
        </button>
        <button
          className="trajectory-bar-tool trajectory-bar-switch"
          aria-label={t('gui.trajectory.bar.show_internal')}
          title={t('gui.trajectory.bar.show_internal')}
          aria-pressed={prefs.showInternal}
          onClick={() => store.setPrefs({ showInternal: !prefs.showInternal })}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" />
          </svg>
        </button>
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
      {!hover && over && rect && !bucket ? (
        over.kind === 'band'
          ? (
            <BandHover
              band={over.band}
              sum={sum}
              bar={rect}
              clientX={over.clientX}
              size={{ width: 320, height: 44 }}
              viewport={{ width: document.documentElement.clientWidth || 1000, height: document.documentElement.clientHeight || 800 }}
            />
          )
          : (
            <BarSummary
              sum={sum}
              bar={rect}
              clientX={over.clientX}
              size={{ width: 320, height: 28 }}
              viewport={{ width: document.documentElement.clientWidth || 1000, height: document.documentElement.clientHeight || 800 }}
            />
          )
      ) : null}
      {bucket ? <Bucket ids={bucket.ids} {...bucketPlace()} onPick={onPick} onExpand={onExpand} /> : null}
    </div>
  )
}

/* Exposed for tests that drive the gap arithmetic directly. */
export { GAP }
