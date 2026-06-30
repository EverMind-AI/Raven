import { describe, expect, it, vi } from 'vitest'

import { resetTerminalModes, TERMINAL_MODE_RESET } from '../lib/terminalModes.js'

describe('terminal mode reset', () => {
  it('includes common sticky input modes', () => {
    expect(TERMINAL_MODE_RESET).toContain('\x1b[0\'z')
    expect(TERMINAL_MODE_RESET).toContain('\x1b[0\'{')
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

  it('skips non-TTY streams', () => {
    const write = vi.fn()

    expect(resetTerminalModes({ isTTY: false, write } as unknown as NodeJS.WriteStream)).toBe(false)
    expect(write).not.toHaveBeenCalled()
  })
})
