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

export function createAxis(intervals: readonly Interval[], options: { idleMs?: number; gapWidth?: number } = {}): TimeAxis {
  const idleMs = options.idleMs ?? IDLE_MS
  const runs = merge(intervals)
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

  /* Lower bound so a shared boundary keeps the earlier piece, which is what
     makes an instant at the end of a run project to the end of that run. */
  const find = (value: number, key: 'start' | 'end'): Piece => {
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
    end,
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
