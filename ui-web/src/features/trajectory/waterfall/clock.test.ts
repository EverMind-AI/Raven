// @vitest-environment happy-dom
/* Wall-clock spellings. Every instant is built from local fields, so the
 * expectations hold in whatever zone the suite runs in. */

import { afterEach, describe, expect, it } from 'vitest'

import * as lang from '../../../state/lang'
import { clockOf, dayOf, lengthOf, spanOf, windowText } from './clock'

const at = (day: number, hour: number, minute: number): number => new Date(2026, 9, day, hour, minute).getTime()

afterEach(() => lang._resetForTests())

describe('the waterfall clock', () => {
  it('writes a time as two-digit hours and minutes', () => {
    expect(clockOf(at(28, 9, 5))).toBe('09:05')
    expect(clockOf(at(28, 23, 59))).toBe('23:59')
  })

  it('writes a day in the page’s language, with its weekday when asked', () => {
    lang.set('en')
    expect(dayOf(at(28, 9, 12))).toBe('Oct 28')
    expect(dayOf(at(28, 9, 12), { weekday: true })).toBe('Wed, Oct 28')
    lang.set('zh')
    /* The platform's own spelling for the page's language, not English's. */
    expect(dayOf(at(28, 9, 12))).toBe(new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(at(28, 9, 12)))
    expect(dayOf(at(28, 9, 12))).not.toBe('Oct 28')
  })

  it('names the day once for a stretch inside one day, and both days across midnight', () => {
    lang.set('en')
    expect(spanOf(at(28, 9, 12), at(28, 13, 40))).toEqual({ from: 'Oct 28 09:12', to: '13:40' })
    expect(spanOf(at(28, 9, 12), at(30, 7, 39))).toEqual({ from: 'Oct 28 09:12', to: 'Oct 30 07:39' })
  })

  it('writes a window’s length in its largest whole unit when it has one', () => {
    expect(lengthOf(3 * 3_600_000)).toBe('3h')
    expect(lengthOf(15 * 60_000)).toBe('15m')
    expect(lengthOf(90 * 60_000)).toBe('1h 30m')
    expect(lengthOf(45_000)).toBe('45s')
  })

  it('splits a window into its day and its two ends, seconds only when asked', () => {
    lang.set('en')
    expect(windowText(at(28, 10, 12), at(28, 13, 12), 'minute')).toEqual({ day: 'Wed, Oct 28', from: '10:12', to: '13:12' })
    expect(windowText(at(28, 23, 0), at(29, 1, 0), 'minute')).toEqual({ day: 'Wed, Oct 28', from: '23:00', to: 'Oct 29 01:00' })
    expect(windowText(at(28, 10, 12), at(28, 10, 13), 'second').from).toBe('10:12:00')
  })
})
