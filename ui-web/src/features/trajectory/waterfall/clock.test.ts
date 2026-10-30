// @vitest-environment happy-dom
/* Wall-clock spellings. Every instant is built from local fields, so the
 * expectations hold in whatever zone the suite runs in. */

import { afterEach, describe, expect, it } from 'vitest'

import * as lang from '../../../state/lang'
import { clockOf, dayOf, spanOf } from './clock'

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
})
