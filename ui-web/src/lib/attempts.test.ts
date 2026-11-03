import { describe, expect, it } from 'vitest'

import { attemptOrder, attemptsOf, instanceOf } from './attempts'

/* The one order rule every surface folds a node report with. Six copies used to
 * agree today and nothing held them together; these pin the three answers a
 * frame, a manifest and a snapshot must all give the same history. */

describe('attemptOrder', () => {
  it('reads a missing or shorter history as the attempt the node moved past', () => {
    expect(attemptOrder(['alpha', 'beta'], undefined)).toBe('stale')
    expect(attemptOrder(['alpha', 'beta'], ['alpha'])).toBe('stale')
  })

  it('reads a longer history as a later attempt', () => {
    expect(attemptOrder(undefined, ['alpha'])).toBe('newer')
    expect(attemptOrder(['alpha'], ['alpha', 'beta'])).toBe('newer')
  })

  it('reads equal histories as the same attempt', () => {
    expect(attemptOrder(undefined, undefined)).toBe('same')
    expect(attemptOrder(['alpha'], ['alpha'])).toBe('same')
  })
})

describe('attemptsOf', () => {
  it('copies a list of non-empty strings', () => {
    const history = ['alpha', 'beta']
    expect(attemptsOf(history)).toEqual(history)
    expect(attemptsOf(history)).not.toBe(history)
  })

  it('answers undefined for absent and malformed values', () => {
    expect(attemptsOf(undefined)).toBeUndefined()
    expect(attemptsOf(['alpha', ''])).toBeUndefined()
    expect(attemptsOf(['alpha', 1])).toBeUndefined()
    expect(attemptsOf('alpha')).toBeUndefined()
  })
})

describe('instanceOf', () => {
  it('keeps a present handle', () => {
    expect(instanceOf({ instance: 'shared' })).toEqual({ instance: 'shared' })
  })

  it('clears a present null or empty handle', () => {
    expect(instanceOf({ instance: null })).toEqual({ instance: null })
    expect(instanceOf({ instance: '' })).toEqual({ instance: null })
    expect(instanceOf({ instance: 1 })).toEqual({ instance: null })
  })

  it('says nothing when the key is absent', () => {
    expect(instanceOf({})).toEqual({})
  })
})
