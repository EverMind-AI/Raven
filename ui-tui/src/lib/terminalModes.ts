// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { writeSync } from 'node:fs'

const LEAVE_ALT_SCREEN = '\x1b[?1049l'

const MODE_RESETS = [
  "\x1b[0'z", // DEC locator reporting
  "\x1b[0'{", // selectable locator events
  '\x1b[?2029l', // passive mouse
  '\x1b[?1016l', // SGR-pixels mouse
  '\x1b[?1015l', // urxvt decimal mouse
  '\x1b[?1006l', // SGR mouse
  '\x1b[?1005l', // UTF-8 extended mouse
  '\x1b[?1003l', // any-motion mouse
  '\x1b[?1002l', // button-motion mouse
  '\x1b[?1001l', // highlight mouse
  '\x1b[?1000l', // click mouse
  '\x1b[?9l', // X10 mouse
  '\x1b[?1004l', // focus events
  '\x1b[?2004l', // bracketed paste
  LEAVE_ALT_SCREEN, // alternate screen
  '\x1b[<u', // kitty keyboard
  '\x1b[>4m', // modifyOtherKeys
  '\x1b[0m', // attributes
  '\x1b]112\x07', // reset cursor color (OSC 112) — see useHardwareCursorColor
  '\x1b[?25h' // cursor visible
] as const

/** Every mode this TUI can leave behind. For exit paths, which own the screen. */
export const TERMINAL_MODE_RESET = MODE_RESETS.join('')

/**
 * The same reset minus the alternate-screen exit, for the one call that runs
 * before this TUI owns the screen.
 *
 * `CSI ? 1049 l` is not inert on the primary screen. Ghostty restores the
 * saved cursor for it unconditionally, without first checking that the
 * alternate screen was ever entered (`Terminal.switchScreenMode`), so sending
 * it at startup teleports the cursor away from the shell prompt that is still
 * on screen -- to (0, 0) when nothing has saved one, or to whatever a previous
 * full-screen program left in the slot. Ink's own `?1049h` then saves *that*
 * position, and exiting restores it, which is where the screenful of blank
 * lines between the launch command and the returning prompt comes from.
 *
 * Dropping it costs nothing this call was there for: a previous TUI killed
 * mid-alt-screen is recovered by this run's own `?1049h` / `?1049l` pair, and
 * the sticky input modes -- the mouse, focus and paste reporting this reset
 * exists to clear -- are all still here.
 */
export const TERMINAL_MODE_RESET_ON_START = MODE_RESETS.filter(sequence => sequence !== LEAVE_ALT_SCREEN).join('')

type ResettableStream = Pick<NodeJS.WriteStream, 'isTTY' | 'write'> & {
  fd?: number
}

function writeModeReset(sequence: string, stream: ResettableStream): boolean {
  if (!stream.isTTY) {
    return false
  }

  const fd = typeof stream.fd === 'number' ? stream.fd : stream === process.stdout ? 1 : undefined

  if (fd !== undefined) {
    try {
      writeSync(fd, sequence)

      return true
    } catch {
      // Fall through to stream.write for mocked or unusual TTY streams.
    }
  }

  try {
    stream.write(sequence)

    return true
  } catch {
    return false
  }
}

export function resetTerminalModes(stream: ResettableStream = process.stdout): boolean {
  return writeModeReset(TERMINAL_MODE_RESET, stream)
}

/** See {@link TERMINAL_MODE_RESET_ON_START} for why startup gets its own set. */
export function resetTerminalModesOnStart(stream: ResettableStream = process.stdout): boolean {
  return writeModeReset(TERMINAL_MODE_RESET_ON_START, stream)
}
