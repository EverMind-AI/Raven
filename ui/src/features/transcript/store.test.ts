/* Pure helpers of the transcript store, tested without a lane or a DOM. */

import { describe, expect, it } from 'vitest'

import * as store from './store'


/* How long the GRAPH took, which is not how long the call took. A backgrounded
   graph -- the default -- returns as soon as it is submitted, so the call's own
   duration is a number near zero that never moves again. */
describe('dagSpan', () => {
  const node = (status: string, started: number | null, ended: number | null) =>
    ({ status, started_at: started, ended_at: ended })

  it('runs from the first start to the last end once every node has stopped', () => {
    expect(store.dagSpan([
      node('completed', 1000, 4000),
      node('completed', 1200, 9000),
      node('failed', 1500, 2000),
    ])).toEqual({ from: 1000, to: 9000, open: false })
  })

  it('is still open while any node has not stopped', () => {
    /* `to: null` is what makes the row keep counting. Reporting the last end so
       far would show a duration that shrinks and grows as nodes land. */
    expect(store.dagSpan([
      node('completed', 1000, 4000),
      node('running', 1200, null),
    ])).toEqual({ from: 1000, to: null, open: true })
  })

  it('is still open while a node has not started', () => {
    expect(store.dagSpan([node('completed', 1000, 4000), node('pending', null, null)]))
      .toEqual({ from: 1000, to: null, open: true })
  })

  it('counts a skipped or cancelled node as stopped', () => {
    /* They never end, so treating them as live left a finished graph counting
       forever -- which is what a cascade of skips after one failure produces. */
    expect(store.dagSpan([
      node('failed', 1000, 4000),
      node('skipped', null, null),
      node('cancelled', 1200, null),
    ])).toEqual({ from: 1000, to: 4000, open: false })
  })

  it('is closed with no end when every node stopped without a stamp', () => {
    /* What a cancel looks like over the wire: the runner's transition for
       `cancelled` and `skipped` carries neither stamp, so a node that got
       `started_at` from its `running` event settles with `ended_at` null. A
       null `to` here must not read as open, or the card counts up forever on
       the one path that has no end to count towards. */
    expect(store.dagSpan([node('cancelled', 1200, null)]))
      .toEqual({ from: 1200, to: null, open: false })
    expect(store.dagSpan([
      node('cancelled', 1200, null),
      node('skipped', null, null),
    ])).toEqual({ from: 1200, to: null, open: false })
  })

  it('has nothing to report before the first node starts', () => {
    /* Null, not zero: zero reads as an answer. */
    expect(store.dagSpan([node('pending', null, null)])).toBeNull()
    expect(store.dagSpan([])).toBeNull()
  })
})
