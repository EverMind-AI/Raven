// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One clock for every elapsed-seconds label in the UI.
//
// `fmtDuration` floors, so two components that each keep their own
// `setInterval(1000)` land on opposite sides of a second boundary and stay
// there: the DAG card in the transcript read 13s while the live strip read 14s
// for the same node, off the same `started_at`. Every label reading the same
// tick makes that disagreement unrepresentable, and one timer serves however
// many labels are on screen.

const TICK_MS = 1000

type Listener = (now: number) => void

const listeners = new Set<Listener>()
let current = Date.now()
let handle: ReturnType<typeof setInterval> | undefined

/**
 * The tick every label is currently rendering against.
 *
 * Re-read from the system clock only while nothing is subscribed: a caller that
 * joins mid-second must agree with the labels already on screen, not jump ahead
 * of them.
 */
export const sharedNow = (): number => {
  if (handle === undefined) {
    current = Date.now()
  }

  return current
}

/** Subscribe to the shared tick. The timer runs only while someone listens. */
export const subscribeSharedNow = (listener: Listener): (() => void) => {
  listeners.add(listener)

  if (handle === undefined) {
    current = Date.now()
    handle = setInterval(() => {
      current = Date.now()

      for (const subscriber of [...listeners]) {
        subscriber(current)
      }
    }, TICK_MS)
  }

  return () => {
    listeners.delete(listener)

    if (listeners.size === 0 && handle !== undefined) {
      clearInterval(handle)
      handle = undefined
    }
  }
}
