/* The ruler's marks, in the reader's own clock time.
 *
 * A mark stands on a round time -- the quarter hour, the hour, midnight --
 * and the step is the shortest that leaves a label room at the ruler's width.
 * The axis folds idle stretches to a few pixels, so a mark that would fall
 * inside a fold is skipped, and one the fold has pulled against its neighbour
 * is dropped rather than printed over it. Whole hours are major, or midnights
 * once a step is an hour or more, so a window across two days shows where
 * one ends.
 */

import { clockOf, dayOf } from './clock'

import type { TimeAxis, Viewport } from './axis'

/** The room one label needs. */
export const LABEL_PX = 90

const SECOND = 1000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE
/* Every step divides the one above it and the day, so a mark stays on a round
   time across midnight. */
const STEPS = [
  100, 200, 500, SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND, 15 * SECOND, 30 * SECOND,
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, 24 * HOUR,
]

export interface ClockTick {
  /** The mark's place, as a fraction of the visible window. */
  left: number
  time: number
  major: boolean
}

/** The first instant at or after `time` that is a whole number of steps past its local midnight.
 *  A step of an hour or more counts the local clock's hours, not milliseconds:
 *  on a day 23 or 25 hours long, milliseconds would put every mark after the
 *  clocks change an hour off its round time. An hour the clocks skip gets no
 *  mark -- `Date` hands back a later time for it -- except a midnight, whose
 *  day still starts: its mark goes on the day's first instant. */
function aligned(time: number, step: number): number {
  const day = new Date(time)
  if (step >= HOUR) {
    for (let k = 0; ; k += 1) {
      const hour = (k * step) / HOUR
      const at = new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour)
      const shown = at.getHours() === hour % 24 && at.getMinutes() === 0
      if (at.getTime() >= time && (shown || hour % 24 === 0)) return at.getTime()
    }
  }
  const midnight = new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime()
  return midnight + Math.ceil((time - midnight) / step) * step
}

/** The shortest round step that leaves a label its room. Projected positions
 *  are milliseconds of time something ran, so the room is a stretch of that. */
export function stepFor(view: Viewport, widthPx: number): number {
  const wanted = ((view.to - view.from) * LABEL_PX) / Math.max(1, widthPx)
  return STEPS.find((step) => step >= wanted) ?? STEPS.at(-1)!
}

/* A day's first instant: midnight, or the time the clocks show first on a day they skip it. */
const atMidnight = (time: number): boolean => {
  const at = new Date(time)
  return time === new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime()
}

/* The next unit up is major: whole minutes on a seconds ruler, whole hours on
   a minutes ruler, midnights once a step is an hour or more. */
function majorAt(time: number, step: number): boolean {
  const at = new Date(time)
  if (at.getSeconds() !== 0 || at.getMilliseconds() !== 0) return false
  if (step < MINUTE) return true
  if (at.getMinutes() !== 0) return false
  return step < HOUR || atMidnight(time)
}

export function clockTicks(axis: TimeAxis, view: Viewport, widthPx: number): ClockTick[] {
  const span = view.to - view.from
  if (!(span > 0) || !(widthPx > 0)) return []
  const step = stepFor(view, widthPx)
  const from = axis.unproject(view.from)
  const to = axis.unproject(view.to)
  const ticks: ClockTick[] = []
  let fold = 0
  let lastPx = -Infinity
  let time = aligned(from, step)
  while (time <= to) {
    while (fold < axis.gaps.length && axis.gaps[fold]!.end <= time) fold++
    const gap = axis.gaps[fold]
    if (gap !== undefined && gap.start < time) {
      /* Inside a fold: carry on from the first round time past it. */
      time = aligned(gap.end, step)
      continue
    }
    const left = (axis.project(time) - view.from) / span
    if ((left * widthPx) - lastPx >= LABEL_PX * 0.6) {
      lastPx = left * widthPx
      ticks.push({ left, time, major: majorAt(time, step) })
    }
    time = aligned(time + 1, step)
  }
  return ticks
}

/** A mark's label, as fine as its step: the day itself at midnight. */
export function tickLabel(time: number, step: number): string {
  if (atMidnight(time)) return dayOf(time)
  return clockOf(time, step >= MINUTE ? 'minute' : step >= SECOND ? 'second' : 'tenth')
}
