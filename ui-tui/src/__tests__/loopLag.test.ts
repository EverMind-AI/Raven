// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { startLoopLagMonitor } from '../lib/loopLag.js'

// Only the timer is faked. A stall is precisely "the clock moved further than
// the schedule did", so the clock has to be driven independently of it.
describe('startLoopLagMonitor', () => {
  let clock = 0

  beforeEach(() => {
    clock = 0
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  const tick = (elapsedMs: number) => {
    clock += elapsedMs
    vi.advanceTimersByTime(1000)
  }

  it('stays quiet while ticks land on time', () => {
    const onStall = vi.fn()
    const stop = startLoopLagMonitor({ intervalMs: 1000, onStall, thresholdMs: 500 })

    for (let i = 0; i < 5; i++) {
      tick(1000)
    }

    stop()

    expect(onStall).not.toHaveBeenCalled()
  })

  it('reports the lag when the thread was busy across a tick', () => {
    const onStall = vi.fn()
    const stop = startLoopLagMonitor({ intervalMs: 1000, onStall, thresholdMs: 500 })

    tick(2500)
    stop()

    expect(onStall).toHaveBeenCalledTimes(1)
    expect(onStall.mock.calls[0]![0]).toBe(1500)
  })

  it('measures each tick against the previous one, not against the start', () => {
    const onStall = vi.fn()
    const stop = startLoopLagMonitor({ intervalMs: 1000, onStall, thresholdMs: 500 })

    tick(2500)
    tick(1000)
    stop()

    expect(onStall).toHaveBeenCalledTimes(1)
  })

  it('stops sampling once stopped', () => {
    const onStall = vi.fn()
    const stop = startLoopLagMonitor({ intervalMs: 1000, onStall, thresholdMs: 500 })

    stop()
    tick(9000)

    expect(onStall).not.toHaveBeenCalled()
  })
})
