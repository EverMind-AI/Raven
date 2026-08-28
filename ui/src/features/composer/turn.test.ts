import { afterEach, describe, expect, it } from 'vitest'

import * as turn from './turn'

afterEach(() => turn._resetForTests())

describe('the composer turn phase', () => {
  it('distinguishes a sent turn from cancellable and runtime streams', () => {
    turn.dispatch({ type: 'send' })
    expect(turn.snapshot()).toEqual({ phase: 'sending', cancellable: true, resume: null })

    turn.dispatch({ type: 'stream', cancellable: true })
    expect(turn.phase()).toBe('streaming')
    expect(turn.cancellable()).toBe(true)

    turn.dispatch({ type: 'stream', cancellable: false })
    expect(turn.busy()).toBe(true)
    expect(turn.cancellable()).toBe(false)
  })

  it('restores the exact active phase after the reader answers', () => {
    turn.dispatch({ type: 'send' })
    turn.dispatch({ type: 'wait' })
    expect(turn.snapshot()).toEqual({
      phase: 'waiting',
      cancellable: true,
      resume: { phase: 'sending', cancellable: true },
    })
    turn.dispatch({ type: 'resume' })
    expect(turn.snapshot()).toEqual({ phase: 'sending', cancellable: true, resume: null })
  })

  it('keeps cancellation busy and non-cancellable until the response', () => {
    turn.dispatch({ type: 'stream', cancellable: true })
    turn.dispatch({ type: 'cancel' })
    expect(turn.phase()).toBe('cancelling')
    expect(turn.busy()).toBe(true)
    expect(turn.cancellable()).toBe(false)
    turn.dispatch({ type: 'idle' })
    expect(turn.busy()).toBe(false)
  })

  it('copies parked snapshots and applies the same transitions off-screen', () => {
    turn.dispatch({ type: 'stream', cancellable: false })
    const parked = turn.snapshot()
    const waiting = turn.reduce(parked, { type: 'wait' })
    expect(waiting.phase).toBe('waiting')
    expect(turn.phase()).toBe('streaming')

    turn.restore(waiting)
    waiting.phase = 'idle'
    turn.dispatch({ type: 'resume' })
    expect(turn.snapshot()).toEqual({ phase: 'streaming', cancellable: false, resume: null })
  })
})
