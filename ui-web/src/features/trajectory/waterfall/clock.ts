/* The waterfall's wall-clock spellings: the header's span, and every label
 * that names an instant rather than an elapsed time.
 *
 * Times are the reader's local clock, written as two-digit hours and minutes
 * in every language; a day is written by the platform in the page's language,
 * so a Chinese page reads its own spelling of the month and day without a
 * catalogue entry per month.
 */

import * as lang from '../../../state/lang'

const pad = (value: number): string => String(value).padStart(2, '0')

export function clockOf(ms: number): string {
  const at = new Date(ms)
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`
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
