/* The page's time axis: a piecewise map from real milliseconds to a position
 * where stretches no agent was doing anything are collapsed to one fixed
 * width each.
 *
 * Everything a surface draws -- a bar, a tick, a vertical rule -- answers
 * "where is this instant" through `project`, and every pointer answers "what
 * instant is this" through `unproject`. Keeping both here means the ruler and
 * the rows cannot drift apart: they are the same function.
 *
 * The gap width follows the reference implementation: a small multiple of the
 * active time, floored so a break is still clickable and capped so a session
 * that is 99% idle is still mostly its work.
 */

export const IDLE_MS = 60_000

export interface Interval {
  start: number
  end: number
}

export interface Gap extends Interval {
  /** Where the gap begins and ends in projected positions. */
  from: number
  to: number
}

export interface TimeAxis {
  start: number
  end: number
  /** The projected length; a viewport's bounds are in this space. */
  length: number
  gaps: readonly Gap[]
  project(time: number): number
  unproject(position: number): number
}

export interface Viewport {
  from: number
  to: number
}

interface Piece extends Interval {
  from: number
  to: number
  gap: boolean
}

/** The occupied runs, in order, with touching intervals merged. */
function merge(intervals: readonly Interval[]): Interval[] {
  const sorted = intervals
    .filter((i) => Number.isFinite(i.start) && Number.isFinite(i.end) && i.end > i.start)
    .map((i) => ({ start: i.start, end: i.end }))
    .sort((a, b) => a.start - b.start)
  const merged: Interval[] = []
  for (const interval of sorted) {
    const last = merged.at(-1)
    if (last && interval.start <= last.end) last.end = Math.max(last.end, interval.end)
    else merged.push({ ...interval })
  }
  return merged
}

/** The empty scale left after the last bar, as a fraction of the projected
 *  length: the rows draw their duration just past the bar, and the longest
 *  bar has to end before the track's edge for that label to have a place. */
export const TRAIL_PAD = 0.15

/** The pieces and the closures over them, from runs that are already merged. */
function axisOf(runs: readonly Interval[], options: { idleMs?: number; gapWidth?: number; pad?: number }): TimeAxis {
  const idleMs = options.idleMs ?? IDLE_MS
  if (!runs.length) {
    return { start: 0, end: 0, length: 1, gaps: [], project: () => 0, unproject: () => 0 }
  }
  const active = runs.reduce((sum, run) => sum + (run.end - run.start), 0)
  const gapWidth = options.gapWidth ?? Math.max(1000, Math.min(15_000, active / 50))
  const start = runs[0]!.start
  const end = Math.max(runs.at(-1)!.end, start + 1)
  const pieces: Piece[] = []
  const gaps: Gap[] = []
  let position = 0
  const append = (from: number, to: number, gap: boolean): void => {
    if (to <= from) return
    const width = gap ? gapWidth : to - from
    pieces.push({ start: from, end: to, from: position, to: position + width, gap })
    if (gap) gaps.push({ start: from, end: to, from: position, to: position + width })
    position += width
  }
  let cursor = start
  for (const run of runs) {
    append(cursor, run.start, run.start - cursor > idleMs)
    append(Math.max(cursor, run.start), run.end, false)
    cursor = Math.max(cursor, run.end)
  }
  append(cursor, end, end - cursor > idleMs)
  const pad = options.pad ?? 0
  const padMs = pad > 0 ? Math.max(1, Math.round(position * pad)) : 0
  if (padMs > 0) append(end, end + padMs, false)

  /* Lower bound so a shared boundary keeps the earlier piece, which is what
     makes an instant at the end of a run project to the end of that run. */
  const find = (value: number, key: 'start' | 'end' | 'from' | 'to'): Piece => {
    let low = 0
    let high = pieces.length - 1
    while (low < high) {
      const middle = (low + high) >>> 1
      if (value <= pieces[middle]![key]) high = middle
      else low = middle + 1
    }
    return pieces[low]!
  }

  return {
    start,
    end: end + padMs,
    length: Math.max(1, position),
    gaps,
    project(time: number): number {
      if (!Number.isFinite(time)) return 0
      const piece = find(time, 'end')
      if (time < piece.start) return piece.from
      if (time > piece.end) return piece.to
      return piece.from + ((time - piece.start) / (piece.end - piece.start)) * (piece.to - piece.from)
    },
    unproject(target: number): number {
      if (!Number.isFinite(target)) return 0
      const piece = find(target, 'to')
      if (target < piece.from) return piece.start
      if (target > piece.to) return piece.end
      return piece.start + ((target - piece.from) / (piece.to - piece.from)) * (piece.end - piece.start)
    },
  }
}

export function createAxis(intervals: readonly Interval[], options: { idleMs?: number; gapWidth?: number; pad?: number } = {}): TimeAxis {
  return axisOf(merge(intervals), options)
}

/** The same axis, fed a page at a time: intervals are inserted into the merged
 *  runs instead of re-sorting the whole session on every page. */
export class AxisIndex {
  private runs: Interval[] = []
  private cached: TimeAxis | null = null
  private readonly options: { idleMs?: number; gapWidth?: number; pad?: number }

  constructor(options: { idleMs?: number; gapWidth?: number; pad?: number } = {}) {
    this.options = options
  }

  add(intervals: readonly Interval[]): void {
    for (const interval of intervals) {
      if (!Number.isFinite(interval.start) || !Number.isFinite(interval.end) || interval.end <= interval.start) continue
      this.insert({ start: interval.start, end: interval.end })
    }
  }

  axis(): TimeAxis {
    if (this.cached === null) this.cached = axisOf(this.runs, this.options)
    return this.cached
  }

  private insert(next: Interval): void {
    let low = 0
    let high = this.runs.length
    while (low < high) {
      const middle = (low + high) >>> 1
      if (this.runs[middle]!.start < next.start) low = middle + 1
      else high = middle
    }
    this.cached = null
    const before = this.runs[low - 1]
    if (before !== undefined && next.start <= before.end) {
      before.end = Math.max(before.end, next.end)
      this.absorb(low, before)
      return
    }
    const after = this.runs[low]
    if (after !== undefined && next.end >= after.start) {
      after.start = Math.min(after.start, next.start)
      after.end = Math.max(after.end, next.end)
      this.absorb(low + 1, after)
      return
    }
    this.runs.splice(low, 0, next)
  }

  /** Folds every run the given one now touches into it. */
  private absorb(at: number, into: Interval): void {
    while (this.runs[at] !== undefined && this.runs[at]!.start <= into.end) {
      into.end = Math.max(into.end, this.runs[at]!.end)
      this.runs.splice(at, 1)
    }
  }
}

export function clampViewport(viewport: Viewport, length: number): Viewport {
  const width = Math.min(length, Math.max(Math.min(1, length), viewport.to - viewport.from))
  const from = Math.min(Math.max(0, viewport.from), length - width)
  return { from, to: from + width }
}

/** Zooms about an anchor, holding the anchor's relative position in the view. */
export function zoomViewport(viewport: Viewport, factor: number, anchor: number, length: number): Viewport {
  const span = viewport.to - viewport.from
  if (!(span > 0) || !(factor > 0)) return clampViewport(viewport, length)
  const ratio = Math.min(1, Math.max(0, (anchor - viewport.from) / span))
  const width = span / factor
  return clampViewport({ from: anchor - width * ratio, to: anchor - width * ratio + width }, length)
}

/** A real interval as fractions of the visible viewport, or null when it is off screen. */
export function barOf(axis: TimeAxis, viewport: Viewport, start: number, end: number): { left: number; width: number } | null {
  const span = viewport.to - viewport.from
  if (!(span > 0)) return null
  const a = axis.project(start)
  const b = Math.max(a, axis.project(end))
  if (b < viewport.from || a > viewport.to) return null
  const left = (Math.max(a, viewport.from) - viewport.from) / span
  const right = (Math.min(b, viewport.to) - viewport.from) / span
  return { left, width: Math.max(0, right - left) }
}

/** The ruler's spelling of an instant: one unit, the largest the value fills,
 *  so a label reads "2 s", "15 min" or "3 h" -- the network timeline's own
 *  habit -- and never "1085m21s". `step` picks the unit for the zero mark,
 *  which has no magnitude of its own to go by. */
export function formatTick(ms: number, step = 1): string {
  const at = Math.max(0, Math.round(ms))
  if (at === 0) {
    if (step >= 3_600_000) return '0 h'
    if (step >= 60_000) return '0 min'
    if (step >= 1000) return '0 s'
    return '0 ms'
  }
  if (at < 1000) return `${at} ms`
  /* Each unit is chosen on the value as it will print, rounded to its one
     decimal: chosen on the raw value, 59.96 s printed as "60.0 s" and 3 h
     59 min 40 s as "3 h 60 min". */
  const tenthsOfSecond = Math.round(at / 100)
  if (tenthsOfSecond < 600) return `${trimmed(tenthsOfSecond / 10)} s`
  const tenthsOfMinute = Math.round(at / 6000)
  if (tenthsOfMinute < 600) return `${trimmed(tenthsOfMinute / 10)} min`
  const minutes = Math.round(at / 60_000)
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`
}

const trimmed = (value: number): string => (Number.isInteger(value) ? String(value) : value.toFixed(1))

/** The ruler's marks: a handful of positions across the viewport, labelled with
 *  the real elapsed time at that point. Positions come from the viewport (so a
 *  compressed gap never multiplies them) and labels from `unproject` (so a tick
 *  over a gap still says how far into the session it is). */
export function tickMarks(axis: TimeAxis, viewport: Viewport, count = 8): Array<{ left: number; time: number }> {
  const span = viewport.to - viewport.from
  if (!(span > 0)) return []
  const marks: Array<{ left: number; time: number }> = []
  for (let index = 0; index <= count; index++) {
    const left = index / count
    const at = viewport.from + span * left
    marks.push({ left, time: axis.unproject(at) - axis.start })
  }
  return marks
}
