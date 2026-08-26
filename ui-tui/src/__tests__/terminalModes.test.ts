import { describe, expect, it, vi } from 'vitest'

import {
  resetTerminalModes,
  resetTerminalModesOnStart,
  TERMINAL_MODE_RESET,
  TERMINAL_MODE_RESET_ON_START
} from '../lib/terminalModes.js'

describe('terminal mode reset', () => {
  it('includes common sticky input modes', () => {
    expect(TERMINAL_MODE_RESET).toContain("\x1b[0'z")
    expect(TERMINAL_MODE_RESET).toContain("\x1b[0'{")
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?2029l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1016l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1015l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1006l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1005l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1003l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1002l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1001l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1000l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?9l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1004l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?2004l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1049l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[<u')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[>4m')
  })

  it('resets the cursor color (OSC 112) so a recolored cursor is restored on exit', () => {
    expect(TERMINAL_MODE_RESET).toContain('\x1b]112\x07')
  })

  it('writes reset sequence to TTY streams without fds', () => {
    const write = vi.fn()

    expect(resetTerminalModes({ isTTY: true, write } as unknown as NodeJS.WriteStream)).toBe(true)
    expect(write).toHaveBeenCalledWith(TERMINAL_MODE_RESET)
  })

  it('leaves the alternate screen alone at startup', () => {
    // On the primary screen `?1049l` is not inert: Ghostty restores the saved
    // cursor for it whether or not the alternate screen was ever entered, so
    // sending it before Ink owns the screen moves the cursor off the shell
    // prompt still displayed there -- and the position Ink then saves on
    // `?1049h` is the wrong one, which is what the caller sees on exit as a
    // screenful of blank lines.
    expect(TERMINAL_MODE_RESET_ON_START).not.toContain('\x1b[?1049l')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[?1049l')
  })

  it('clears the same sticky input modes at startup as on exit', () => {
    // The alternate screen is the only difference; a mode dropped from one set
    // and not the other is a mode nothing clears.
    expect(TERMINAL_MODE_RESET_ON_START).toBe(TERMINAL_MODE_RESET.replace('\x1b[?1049l', ''))
  })

  it('writes the startup sequence, not the exit one', () => {
    const write = vi.fn()

    expect(resetTerminalModesOnStart({ isTTY: true, write } as unknown as NodeJS.WriteStream)).toBe(true)
    expect(write).toHaveBeenCalledWith(TERMINAL_MODE_RESET_ON_START)
  })

  it('skips non-TTY streams for the startup reset too', () => {
    const write = vi.fn()

    expect(resetTerminalModesOnStart({ isTTY: false, write } as unknown as NodeJS.WriteStream)).toBe(false)
    expect(write).not.toHaveBeenCalled()
  })

  it('skips non-TTY streams', () => {
    const write = vi.fn()

    expect(resetTerminalModes({ isTTY: false, write } as unknown as NodeJS.WriteStream)).toBe(false)
    expect(write).not.toHaveBeenCalled()
  })
})
