/* The two spellings of a past instant: how long ago it was, and how long a
   state has held since it. */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { ago, since } from './ago'

const NOW = Date.parse('2026-11-01T12:00:00Z')
const MIN = 60_000

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
})

afterEach(() => {
  resetTranslator()
})

describe('since', () => {
  it('spells a span on the same ladder ago climbs, without the "ago"', () => {
    expect(since(NOW - 30_000, NOW)).toBe('gui.time.for_now')
    expect(since(NOW - 5 * MIN, NOW)).toBe('gui.time.for_m {"n":5}')
    expect(since(NOW - 3 * 60 * MIN, NOW)).toBe('gui.time.for_h {"n":3}')
    expect(since(NOW - 50 * 60 * MIN, NOW)).toBe('gui.time.for_d {"n":2}')
  })

  it('says nothing for an instant nothing measured', () => {
    expect([since(null, NOW), since(0, NOW)]).toEqual(['', ''])
  })

  it('leaves ago as it was', () => {
    expect(ago(NOW - 5 * MIN, NOW)).toBe('gui.time.ago_m {"n":5}')
    expect(ago(NOW - 50 * 60 * MIN, NOW)).toBe('gui.time.ago_d {"n":2}')
  })
})
