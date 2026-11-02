/* The activity strip above the ruler: how many calls the session made, over
 * the whole of it, and the window the rows below show.
 *
 * It always draws the whole session on the folded axis, whatever the window
 * (a Waterfall Replay hands it only what its playhead has passed):
 * a bar per bucket of real time, the model's calls under the tools', a mark
 * over a bucket that holds a failure, and each folded idle stretch with how
 * long it was, where it is wide enough to say. The window is a brush over it:
 * dragging across the strip takes a new window, dragging the brush moves it,
 * and dragging either edge moves that edge alone. A click keeps the window's
 * length and centres it there.
 *
 * The strip folds the same idle stretches the rows' axis does, but on an axis
 * of its own (`stripAxis`) that gives each fold room for its length while the
 * folds keep to a third of the strip: the rows' axis keeps a fold to a sliver
 * of the working time, too narrow to read. The two meet in real time -- the
 * window is kept in milliseconds -- so the brush frames on this axis exactly
 * the stretch the rows show on theirs.
 */

import { memo, useEffect, useMemo, useRef, useState } from 'react'

import { t } from '../../../i18n/t'
import { activity, bucketWidth, stripAxis } from './activity'
import { clockOf, dayOf, lengthOf } from './clock'

import type { TrajectoryEntry } from '../types'
import type { Interval, TimeAxis } from './axis'
import type { JSX, PointerEvent as ReactPointerEvent } from 'react'

const percent = (fraction: number): string => `${(fraction * 100).toFixed(4)}%`

/* How narrow a folded idle stretch may get (a session folded many times over)
   before its length is left unsaid. */
const FOLD_LABEL_PX = 22

/** An idle stretch's length in its largest unit, rounded: "10h", "25m", "40s". */
function idleLength(ms: number): string {
  if (ms >= 3_600_000) return `${Math.round(ms / 3_600_000)}h`
  if (ms >= 60_000) return `${Math.round(ms / 60_000)}m`
  return `${Math.round(ms / 1000)}s`
}

/* Memoised: the rows below re-render on every scroll frame, and nothing the
   strip shows moves with them. */
export const ActivityStrip = memo(function ActivityStrip({ axis: rowsAxis, entries, start, end, zoomed, onWindow }: {
  /** The rows' axis: its folds are the strip's, drawn wider. */
  axis: TimeAxis
  entries: readonly TrajectoryEntry[]
  /** The window the rows show, in real time. */
  start: number
  end: number
  zoomed: boolean
  onWindow: (next: Interval) => void
}): JSX.Element {
  const plot = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = plot.current
    if (!el) return
    const measure = (): void => setWidth(el.clientWidth)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const room = width > 0 ? width : 1200
  const axis = useMemo(() => stripAxis(rowsAxis, room), [rowsAxis, room])
  const bucketMs = bucketWidth(axis, room)
  const buckets = useMemo(() => activity(entries, axis, bucketMs), [entries, axis, bucketMs])
  const peak = Math.max(1, ...buckets.map((bucket) => bucket.llm + bucket.tool))
  const full = axis.length
  const at = (time: number): number => axis.project(time) / full

  /* Where the session crosses into another day, read at each busy stretch's start. */
  const days = useMemo(() => {
    const marks: Array<{ left: number; label: string }> = [{ left: 0, label: dayOf(axis.start) }]
    let day = new Date(axis.start).toDateString()
    for (const gap of axis.gaps) {
      const next = new Date(gap.end).toDateString()
      if (next !== day) marks.push({ left: gap.to / full, label: dayOf(gap.end) })
      day = next
    }
    return marks
  }, [axis, full])

  const lo = axis.project(start) / full
  const hi = axis.project(end) / full
  /* A stretch of the strip, as fractions of it, in real time. */
  const take = (from: number, to: number): void => {
    onWindow({ start: axis.unproject(Math.max(0, from) * full), end: axis.unproject(Math.min(1, to) * full) })
  }
  type Drag =
    | { mode: 'new'; at: number; width: number }
    | { mode: 'pan'; at: number; width: number; from: number; to: number }
    | { mode: 'edge'; edge: 'lo' | 'hi'; width: number; fixed: number }
  const drag = useRef<Drag | null>(null)
  const fraction = (clientX: number, rect: DOMRect): number =>
    Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)))
  const down = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const where = fraction(event.clientX, rect)
    const target = event.target as HTMLElement
    const grip = target.closest?.('[data-edge]') as HTMLElement | null
    if (zoomed && grip !== null) {
      const edge = grip.dataset.edge === 'lo' ? 'lo' : 'hi'
      drag.current = { mode: 'edge', edge, width: rect.width, fixed: edge === 'lo' ? hi : lo }
    } else if (zoomed && (target.closest?.('.trajectory-act-brush') ?? null) !== null) {
      drag.current = { mode: 'pan', at: where, width: rect.width, from: lo, to: hi }
    } else {
      drag.current = { mode: 'new', at: where, width: rect.width }
    }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      /* not capturable */
    }
  }
  const apply = (start: Drag, where: number, final: boolean): void => {
    if (start.mode === 'pan') {
      /* Held inside the strip, so a pan past an end keeps the window's width. */
      const shift = Math.min(1 - start.to, Math.max(-start.from, where - start.at))
      take(start.from + shift, start.to + shift)
      return
    }
    if (start.mode === 'edge') {
      take(Math.min(where, start.fixed), Math.max(where, start.fixed))
      return
    }
    if (Math.abs(where - start.at) * start.width >= 3) {
      take(Math.min(where, start.at), Math.max(where, start.at))
      return
    }
    /* A click, not a drag: the window keeps its width and moves to it. */
    if (final && zoomed) {
      const half = (hi - lo) / 2
      const middle = Math.min(1 - half, Math.max(half, where))
      take(middle - half, middle + half)
    }
  }
  const move = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const start = drag.current
    if (start === null) return
    apply(start, fraction(event.clientX, event.currentTarget.getBoundingClientRect()), false)
  }
  const up = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const start = drag.current
    drag.current = null
    if (start === null) return
    apply(start, fraction(event.clientX, event.currentTarget.getBoundingClientRect()), true)
  }

  return (
    <div className="trajectory-act">
      <div className="trajectory-act-key">
        <b>{t('gui.trajectory.activity.title')}</b>
        <i>{t('gui.trajectory.activity.per', { length: lengthOf(bucketMs) })}</i>
        <span className="trajectory-act-legend">
          <em data-kind="llm" />{t('gui.trajectory.activity.llm')}
          <em data-kind="tool" />{t('gui.trajectory.activity.tool')}
          <em data-kind="error" />{t('gui.trajectory.activity.error')}
        </span>
      </div>
      <div className="trajectory-act-plot" ref={plot} aria-hidden="true"
        onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => { drag.current = null }}>
        {buckets.map((bucket) => {
          const left = at(bucket.start)
          const right = at(bucket.end)
          return (
            <span key={bucket.start} className="trajectory-act-bar" style={{ left: percent(left), width: percent(Math.max(0, right - left)) }}>
              <i data-kind="llm" style={{ height: percent(bucket.llm / peak) }} />
              <i data-kind="tool" style={{ height: percent(bucket.tool / peak) }} />
              {bucket.failed ? <b className="trajectory-act-err" style={{ bottom: percent((bucket.llm + bucket.tool) / peak) }} /> : null}
            </span>
          )
        })}
        {axis.gaps.map((gap) => (
          (gap.to - gap.from) / full * room < FOLD_LABEL_PX ? null : (
            <span key={gap.start} className="trajectory-act-fold" style={{ left: percent(gap.from / full), width: percent((gap.to - gap.from) / full) }}>
              {idleLength(gap.end - gap.start)}
            </span>
          )
        ))}
        {days.map((day) => <span key={day.left} className="trajectory-act-day" style={{ left: percent(day.left) }}>{day.label}</span>)}
        <span className="trajectory-act-end" data-side="start">{clockOf(axis.start)}</span>
        <span className="trajectory-act-end" data-side="end">{clockOf(axis.unproject(full))}</span>
        {zoomed ? (
          <>
            <span className="trajectory-act-dim" style={{ left: 0, width: percent(lo) }} />
            <span className="trajectory-act-dim" style={{ left: percent(hi), width: percent(1 - hi) }} />
            <span className="trajectory-act-brush" style={{ left: percent(lo), width: percent(hi - lo) }}>
              <span className="trajectory-act-grip" data-edge="lo" />
              <span className="trajectory-act-grip" data-edge="hi" />
            </span>
          </>
        ) : null}
      </div>
    </div>
  )
})
