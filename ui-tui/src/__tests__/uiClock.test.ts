// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The DAG card in the transcript said 13s while the live strip said 14s for the
// same node: same `started_at`, two `setInterval(1000)`s whose ticks sat on
// opposite sides of a floored second. These are the properties that make that
// unrepresentable -- one timer, one value, and a late subscriber that joins the
// value already on screen rather than the system clock.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sharedNow, subscribeSharedNow } from '../lib/uiClock.js'

describe('uiClock', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('hands every subscriber the same tick', () => {
    const a: number[] = []
    const b: number[] = []
    const offA = subscribeSharedNow(now => a.push(now))
    vi.advanceTimersByTime(400)
    const offB = subscribeSharedNow(now => b.push(now))

    vi.advanceTimersByTime(2_000)
    offA()
    offB()

    expect(a).toEqual(b)
    expect(a.length).toBeGreaterThan(0)
  })

  it('reads the live tick, not the system clock, while subscribed', () => {
    const off = subscribeSharedNow(() => {})
    const joined = sharedNow()

    vi.advanceTimersByTime(600)
    expect(sharedNow()).toBe(joined)

    vi.advanceTimersByTime(600)
    expect(sharedNow()).toBe(joined + 1_000)
    off()
  })

  it('runs one timer for however many subscribers, and none once they leave', () => {
    const off1 = subscribeSharedNow(() => {})
    const off2 = subscribeSharedNow(() => {})

    expect(vi.getTimerCount()).toBe(1)

    off1()
    expect(vi.getTimerCount()).toBe(1)

    off2()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('resyncs to the system clock once nothing is subscribed', () => {
    const off = subscribeSharedNow(() => {})
    off()

    vi.advanceTimersByTime(5_000)
    expect(sharedNow()).toBe(1_005_000)
  })
})
