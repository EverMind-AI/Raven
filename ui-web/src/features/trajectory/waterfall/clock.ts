/* The waterfall's wall-clock spellings: the header's span, and every label
 * that names an instant rather than an elapsed time.
 *
 * Times are the reader's local clock, written as two-digit hours and minutes
 * in every language; a day is written by the platform in the page's language,
 * so a Chinese page reads its own spelling of the month and day without a
 * catalogue entry per month.
 */

import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'

const pad = (value: number): string => String(value).padStart(2, '0')

/** Hours and minutes; seconds, or seconds and tenths, for a mark that close. */
export function clockOf(ms: number, precision: 'minute' | 'second' | 'tenth' = 'minute'): string {
  const at = new Date(ms)
  const minutes = `${pad(at.getHours())}:${pad(at.getMinutes())}`
  if (precision === 'minute') return minutes
  const seconds = `${minutes}:${pad(at.getSeconds())}`
  return precision === 'second' ? seconds : `${seconds}.${Math.floor(at.getMilliseconds() / 100)}`
}

export function dayOf(ms: number, options: { weekday?: boolean } = {}): string {
  const format: Intl.DateTimeFormatOptions = options.weekday
    ? { weekday: 'short', month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric' }
  return new Intl.DateTimeFormat(lang.tag(), format).format(ms)
}

const sameDay = (a: number, b: number): boolean => new Date(a).toDateString() === new Date(b).toDateString()

/** A stretch's two ends; the second names its day only when it is another one. */
export function spanOf(from: number, to: number): { from: string; to: string } {
  return {
    from: `${dayOf(from)} ${clockOf(from)}`,
    to: sameDay(from, to) ? clockOf(to) : `${dayOf(to)} ${clockOf(to)}`,
  }
}

/** A window's length: "3h" or "15m" when it is a whole number of either, so a
 *  preset reads as itself; the shared duration spelling otherwise. */
export function lengthOf(ms: number): string {
  if (ms > 0 && ms % 3_600_000 === 0) return `${ms / 3_600_000}h`
  if (ms > 0 && ms < 3_600_000 && ms % 60_000 === 0) return `${ms / 60_000}m`
  return formatDuration(ms)
}

/** A window's day, with its weekday, and its two ends; the second names its day only when it is another one. */
export function windowText(from: number, to: number, precision: 'minute' | 'second'): { day: string; from: string; to: string } {
  return {
    day: dayOf(from, { weekday: true }),
    from: clockOf(from, precision),
    to: sameDay(from, to) ? clockOf(to, precision) : `${dayOf(to)} ${clockOf(to, precision)}`,
  }
}
