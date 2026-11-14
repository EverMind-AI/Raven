// @vitest-environment happy-dom
/* The dialog's own two facts -- whether it is showing, and what was typed.
   Nothing here touches the DOM: the field is uncontrolled and owns its value,
   so what the page can ask this module is exactly the state below. */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, get, set, subscribe, toggle } from './find'

let draws = 0
let unwatch = (): void => {}

beforeEach(() => {
  _resetForTests()
  draws = 0
  unwatch = subscribe(() => {
    draws += 1
  })
})

afterEach(() => {
  unwatch()
})

describe('the search dialog state', () => {
  it('opens and closes on toggle, and honours a forced state', () => {
    toggle()
    expect(get().open).toBe(true)
    toggle()
    expect(get().open).toBe(false)
    toggle(true)
    toggle(true)
    expect(get().open).toBe(true)
    toggle(false)
    expect(get().open).toBe(false)
  })

  it('drops the term when it closes', () => {
    toggle(true)
    set({ query: 'gtm' })
    expect(get().query).toBe('gtm')
    toggle(false)
    expect(get().query).toBe('')
  })

  it('costs no redraw when closing an empty dialog', () => {
    toggle(false)
    expect(draws).toBe(0)
  })

  it('merges a patch and ignores one that changes nothing', () => {
    set({ open: true })
    expect(draws).toBe(1)
    set({ query: 'a' })
    expect(get()).toEqual({ open: true, query: 'a' })
    expect(draws).toBe(2)
    set({ query: 'a' })
    expect(draws).toBe(2)
  })
})
