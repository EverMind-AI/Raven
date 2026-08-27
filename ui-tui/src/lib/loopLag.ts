// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Watches how late a fixed-period timer runs, which is how long the main thread
// was busy between ticks.
//
// The TUI paints, parses keys and folds every stream event on one thread, so a
// single task that overruns takes the whole UI with it -- including Ctrl+C,
// which reaches the app as a keystroke and not as a signal. When that happens
// there is nothing to read afterwards: the FPS counter cannot tell "nothing to
// draw" from "could not draw", and a stall long enough to matter is usually a
// stall long enough that the user kills the process.
//
// So this records the stall itself, unconditionally and out of band, the way
// the memory monitor records a heap it is about to lose. One line per stalled
// tick, with the memory reading that separates a GC pause from a busy loop.

import { appendFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

// RAVEN_HOME first, matching history.ts and perfPane.tsx: a second install
// keeps its own state dir and must not write into the first one's.
const STATE_HOME = process.env.RAVEN_HOME?.trim() || join(homedir(), '.raven')
const STALL_LOG = process.env.RAVEN_TUI_STALL_LOG?.trim() || join(STATE_HOME, 'tui-stalls.log')

/** Below this, a late tick is ordinary scheduling noise rather than a stall. */
export const STALL_THRESHOLD_MS = 500

export const LAG_SAMPLE_MS = 1000

export interface LoopLagOptions {
  intervalMs?: number
  onStall?: (lagMs: number) => void
  thresholdMs?: number
}

export const recordStall = (lagMs: number): void => {
  const { heapUsed, rss } = process.memoryUsage()
  const row = {
    at: new Date().toISOString(),
    heapUsed,
    lagMs: Math.round(lagMs),
    pid: process.pid,
    rss
  }

  try {
    mkdirSync(dirname(STALL_LOG), { recursive: true })
    appendFileSync(STALL_LOG, `${JSON.stringify(row)}\n`)
  } catch {
    // Best-effort — a diagnostic must never be the reason the TUI goes down.
  }
}

/**
 * Start sampling main-thread lag. Returns the stop callback.
 *
 * Deliberately not behind an env flag, unlike the FPS overlay and the write
 * log: those are hunting tools you reach for once you know what to look for,
 * and this is the record that tells you a hunt is warranted at all.
 */
export function startLoopLagMonitor({
  intervalMs = LAG_SAMPLE_MS,
  onStall = recordStall,
  thresholdMs = STALL_THRESHOLD_MS
}: LoopLagOptions = {}): () => void {
  // performance.now, not Date.now: a wall-clock jump (NTP step, laptop wake)
  // would otherwise read as a multi-second stall.
  let due = performance.now() + intervalMs

  const handle = setInterval(() => {
    const now = performance.now()
    const lag = Math.max(0, now - due)

    due = now + intervalMs

    if (lag >= thresholdMs) {
      onStall(lag)
    }
  }, intervalMs)

  return () => clearInterval(handle)
}
