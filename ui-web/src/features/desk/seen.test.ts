/** The seen record: identities, bounded, and idempotent at the bound. */

// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests as sessionReset, setCurrent } from '../../lib/session'
import * as seen from './seen'

const ids = (n: number, from = 0): string[] =>
  Array.from({ length: n }, (_, i) => `/w/f${from + i}.ts`)

describe('the seen record', () => {
  beforeEach(() => {
    sessionStorage.clear()
    sessionReset()
    setCurrent('s1')
    seen._resetForTests()
  })

  it('marks a list once and settles', () => {
    const list = ids(3)

    expect(seen.mark('diff', list)).toBe(true)
    expect(seen.mark('diff', list)).toBe(false)
  })

  /* The palette marks the tab it draws on every render, so a mark has to
     settle. Past the cap the record keeps the newest slice while the caller
     hands over the whole tab; appending let the cap drop ids that were still
     inside that slice, which read back as new on the next call -- a record
     that could never settle once a tab outgrew it. */
  it('marks a list one past the cap once and settles', () => {
    const list = ids(301)

    expect(seen.mark('diff', list)).toBe(true)
    expect(seen.mark('diff', list)).toBe(false)
    expect([...seen.of_('diff')]).toEqual(list.slice(0, 300))
  })

  /* A new newest item is one arrival, not a re-shuffle: the caller's order is
     the tab's own order, so the slice it writes is exactly the slice it was
     handed. */
  it('takes a new newest item once and keeps the newest slice', () => {
    const list = ids(300, 1)
    seen.mark('diff', list)

    const next = ['/w/f0.ts', ...list]
    expect(seen.mark('diff', next)).toBe(true)
    expect([...seen.of_('diff')]).toEqual(next.slice(0, 300))
    expect(seen.mark('diff', next)).toBe(false)
  })

  it('remembers an item read on its own beside the slice', () => {
    seen.mark('deliverables', ['/w/a.md'])
    seen.mark('deliverables', ['/w/b.md'])

    expect(seen.of_('deliverables')).toEqual(new Set(['/w/a.md', '/w/b.md']))
  })

  /* Private mode, or over quota. `mark` decides from what it reads, so a write
     storage refused would leave the old record behind and every call would
     report news again -- the same never-settling mark by another door. */
  it('settles when storage refuses the record', () => {
    const setItem = sessionStorage.setItem
    Object.defineProperty(sessionStorage, 'setItem', {
      configurable: true,
      value: () => { throw new DOMException('quota', 'QuotaExceededError') },
    })
    try {
      const list = ids(3)
      expect(seen.mark('diff', list)).toBe(true)
      expect(seen.mark('diff', list)).toBe(false)
    } finally {
      Object.defineProperty(sessionStorage, 'setItem', { configurable: true, value: setItem })
    }
  })
})
