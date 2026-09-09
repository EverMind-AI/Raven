// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Rebuilds what the terminal was actually shown, from a RAVEN_TUI_WRITE_LOG
// recording. Pair with a `/paintdump` taken at the same instant (its marker is
// in the log) to compare the renderer's belief against the terminal's cells.

import { TerminalScreen } from './terminalScreen.js'

export type WriteLogRow = { d?: string; mark?: string; t: number }

export const parseWriteLog = (contents: string): WriteLogRow[] =>
  contents
    .split('\n')
    .filter(line => line.trim())
    .map(line => JSON.parse(line) as WriteLogRow)

/**
 * Replay a log into a screen, stopping at `stopAtMark` when given.
 *
 * `columns`/`rows` must be the size the session ran at -- the dump's header
 * states it. A replay at the wrong size wraps differently and every comparison
 * after the first wrapped line is noise.
 */
export const replayWriteLog = (
  rows: WriteLogRow[],
  { columns, rows: height, stopAtMark }: { columns: number; rows: number; stopAtMark?: string }
): TerminalScreen => {
  const screen = new TerminalScreen(columns, height)

  for (const row of rows) {
    if (row.mark !== undefined) {
      if (stopAtMark !== undefined && row.mark.includes(stopAtMark)) {
        break
      }

      continue
    }

    if (row.d !== undefined) {
      screen.write(row.d)
    }
  }

  return screen
}
