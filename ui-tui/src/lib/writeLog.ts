// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Records every byte the TUI writes to stdout, so a paint that looked wrong can
// be reconstructed after the fact.
//
// `/paintdump` answers what the renderer BELIEVED the screen was. This answers
// what the terminal was actually told -- the other half, and the half no dump
// can reach, because the physical screen is not readable. Replaying the log
// through the VT model in the tests rebuilds the terminal's own cells and
// styles, and the two can then be compared at the moment of failure.
//
// Off unless RAVEN_TUI_WRITE_LOG names a file. On, it costs one JSON line per
// write, so it is a hunting tool rather than something to leave running.

import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

const LOG_PATH = process.env.RAVEN_TUI_WRITE_LOG?.trim() ?? ''

let ready = false
let installed = false

const append = (row: Record<string, unknown>) => {
  if (!ready) {
    ready = true

    try {
      mkdirSync(dirname(LOG_PATH), { recursive: true })
    } catch {
      // Best-effort — never take the TUI down to record a frame.
    }
  }

  try {
    appendFileSync(LOG_PATH, `${JSON.stringify(row)}\n`)
  } catch {
    /* best-effort */
  }
}

/**
 * Tee stdout into the write log. Call once, before anything renders.
 *
 * Wraps rather than replaces: the original write still runs and its return
 * value is passed through, so backpressure behaves exactly as it would
 * unlogged. A second call is a no-op, so the wrapper cannot stack.
 */
export const installWriteLog = (stream: NodeJS.WriteStream = process.stdout): boolean => {
  if (!LOG_PATH || installed) {
    return false
  }

  installed = true

  const original = stream.write.bind(stream)

  stream.write = ((chunk: unknown, ...rest: unknown[]) => {
    if (typeof chunk === 'string') {
      append({ d: chunk, t: Date.now() })
    }

    return (original as (...args: unknown[]) => boolean)(chunk, ...rest)
  }) as typeof stream.write

  append({ mark: 'write-log opened', t: Date.now() })

  return true
}

/** Note a moment in the log, so a replay can be cut at the same point. */
export const markWriteLog = (label: string): void => {
  if (!LOG_PATH || !installed) {
    return
  }

  append({ mark: label, t: Date.now() })
}

export const WRITE_LOG_PATH = LOG_PATH
