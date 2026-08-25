// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

// Perf instrumentation for the full render pipeline.
//
//   PerfPane (React.Profiler)  → per-pane commit times
//   logFrameEvent (ink.onFrame) → yoga / renderer / diff / optimize / write
//                                 phases + yoga counters + scroll fast-path
//
// Both gate on RAVEN_DEV_PERF=1 and dump JSON-lines (default ~/.raven/perf.log,
// override RAVEN_DEV_PERF_LOG). Tagged { src: 'react' | 'frame' } for jq.
// RAVEN_DEV_PERF_MS (default 2) skips sub-ms idle frames; set 0 to capture all.
//
// Zero cost when unset: PerfPane returns children directly, logFrameEvent is
// undefined so ink doesn't pay the timing cost.

import type { FrameEvent } from '@hermes/ink'

import { scrollFastPathStats } from '@hermes/ink'
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from 'react'

import { markWriteLog } from './writeLog.js'

const ENABLED = /^(?:1|true|yes|on)$/i.test((process.env.RAVEN_DEV_PERF ?? '').trim())
const THRESHOLD_MS = Number(process.env.RAVEN_DEV_PERF_MS ?? '2') || 0
// RAVEN_HOME first: a second install (its own state dir) would otherwise write
// its samples into the default one, where they read as the other install's.
const STATE_HOME = process.env.RAVEN_HOME?.trim() || join(homedir(), '.raven')
const LOG_PATH = process.env.RAVEN_DEV_PERF_LOG?.trim() || join(STATE_HOME, 'perf.log')

let logReady = false

const writeRow = (row: Record<string, unknown>) => {
  if (!logReady) {
    logReady = true

    try {
      mkdirSync(dirname(LOG_PATH), { recursive: true })
    } catch {
      // Best-effort — never crash the TUI to log a sample.
    }
  }

  try {
    appendFileSync(LOG_PATH, `${JSON.stringify(row)}\n`)
  } catch {
    /* best-effort */
  }
}

const round2 = (n: number) => Math.round(n * 100) / 100

const onRender: ProfilerOnRenderCallback = (id, phase, actualMs, baseMs, startTime, commitTime) => {
  if (actualMs < THRESHOLD_MS) {
    return
  }

  writeRow({
    actualMs: round2(actualMs),
    baseMs: round2(baseMs),
    commitTimeMs: round2(commitTime),
    id,
    phase,
    src: 'react',
    startTimeMs: round2(startTime),
    ts: Date.now()
  })
}

export function PerfPane({ children, id }: { children: ReactNode; id: string }) {
  if (!ENABLED) {
    return children
  }

  return (
    <Profiler id={id} onRender={onRender}>
      {children}
    </Profiler>
  )
}

export const logFrameEvent = ENABLED
  ? (event: FrameEvent) => {
      if (event.durationMs < THRESHOLD_MS) {
        return
      }

      writeRow({
        durationMs: round2(event.durationMs),
        // Cumulative counters — consumers diff pairs to get per-frame deltas.
        fastPath: { ...scrollFastPathStats, declined: { ...scrollFastPathStats.declined } },
        flickers: event.flickers.length ? event.flickers : undefined,
        phases: event.phases
          ? {
              ...event.phases,
              commit: round2(event.phases.commit),
              diff: round2(event.phases.diff),
              optimize: round2(event.phases.optimize),
              prevFrameDrainMs: round2(event.phases.prevFrameDrainMs),
              renderer: round2(event.phases.renderer),
              write: round2(event.phases.write),
              yoga: round2(event.phases.yoga)
            }
          : undefined,
        src: 'frame',
        ts: Date.now()
      })
    }
  : undefined

export const PERF_ENABLED = ENABLED
export const PERF_LOG_PATH = LOG_PATH

/**
 * Write one screen dump next to the perf log and return its path.
 *
 * Unlike the frame log this is not gated on RAVEN_DEV_PERF: it is taken by hand
 * (`/paintdump`) at the moment a paint looks wrong, which is the only moment
 * that carries the evidence. Each dump gets its own file so a before/after pair
 * around `/redraw` can be diffed.
 *
 * Follows RAVEN_HOME when set, so a second install writes into its own state
 * directory rather than the default one.
 */
export const writePaintDump = (dump: null | string): null | string => {
  if (dump === null) {
    return null
  }

  const path = join(STATE_HOME, 'paint-dumps', `${new Date().toISOString().replace(/[:.]/g, '-')}.txt`)

  try {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${dump}\n`)
  } catch {
    return null
  }

  // Same instant in the write log, so a replay can be cut where this dump was
  // taken and the two halves compared at that exact frame.
  markWriteLog(`paintdump ${path}`)

  return path
}
