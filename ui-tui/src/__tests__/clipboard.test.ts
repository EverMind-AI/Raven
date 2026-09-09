// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it, vi } from 'vitest'

import {
  copyOnSelectNotice,
  createCopyOnSelectReporter,
  graphemeCount,
  copyResultNotice,
  isUsableClipboardText,
  readClipboardText,
  writeClipboardText
} from '../lib/clipboard.js'

describe('readClipboardText', () => {
  it('reads text from pbpaste on macOS', async () => {
    const run = vi.fn().mockResolvedValue({ stdout: 'hello world\n' })

    await expect(readClipboardText('darwin', run)).resolves.toBe('hello world\n')
    expect(run).toHaveBeenCalledWith(
      'pbpaste',
      [],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
  })

  it('reads text from PowerShell on Windows', async () => {
    const run = vi.fn().mockResolvedValue({ stdout: 'from windows\r\n' })

    await expect(readClipboardText('win32', run)).resolves.toBe('from windows\r\n')
    expect(run).toHaveBeenCalledWith(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard -Raw'],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
  })

  it('tries powershell.exe first on WSL', async () => {
    const run = vi.fn().mockResolvedValue({ stdout: 'from wsl\n' })

    await expect(readClipboardText('linux', run, { WSL_INTEROP: '/tmp/socket' } as NodeJS.ProcessEnv)).resolves.toBe(
      'from wsl\n'
    )
    expect(run).toHaveBeenCalledWith(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard -Raw'],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
  })

  it('uses wl-paste on Wayland Linux', async () => {
    const run = vi.fn().mockResolvedValue({ stdout: 'from wayland\n' })

    await expect(readClipboardText('linux', run, { WAYLAND_DISPLAY: 'wayland-1' } as NodeJS.ProcessEnv)).resolves.toBe(
      'from wayland\n'
    )
    expect(run).toHaveBeenCalledWith(
      'wl-paste',
      ['--type', 'text'],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
  })

  it('falls back to xclip on Linux when wl-paste fails', async () => {
    const run = vi
      .fn()
      .mockRejectedValueOnce(new Error('wl-paste missing'))
      .mockResolvedValueOnce({ stdout: 'from xclip\n' })

    await expect(readClipboardText('linux', run, { WAYLAND_DISPLAY: 'wayland-1' } as NodeJS.ProcessEnv)).resolves.toBe(
      'from xclip\n'
    )
    expect(run).toHaveBeenNthCalledWith(
      1,
      'wl-paste',
      ['--type', 'text'],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
    expect(run).toHaveBeenNthCalledWith(
      2,
      'xclip',
      ['-selection', 'clipboard', '-out'],
      expect.objectContaining({ encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, windowsHide: true })
    )
  })

  it('returns null when every clipboard backend fails', async () => {
    const run = vi.fn().mockRejectedValue(new Error('clipboard failed'))

    await expect(
      readClipboardText('linux', run, { WAYLAND_DISPLAY: 'wayland-1' } as NodeJS.ProcessEnv)
    ).resolves.toBeNull()
  })
})

describe('isUsableClipboardText', () => {
  it('accepts normal text', () => {
    expect(isUsableClipboardText('hello world\n')).toBe(true)
  })

  it('rejects empty or whitespace-only content', () => {
    expect(isUsableClipboardText('')).toBe(false)
    expect(isUsableClipboardText('  \n\t')).toBe(false)
  })

  it('rejects binary-looking clipboard payloads', () => {
    expect(isUsableClipboardText('PNG\u0000\u0001\u0002\u0003IHDR')).toBe(false)
    expect(isUsableClipboardText('TIFF\ufffd\ufffd\ufffdmetadata')).toBe(false)
  })
})

describe('writeClipboardText', () => {
  it('does nothing off macOS when no tools are available', async () => {
    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(1) // non-zero exit = failure
        }

        return child
      }),
      stdin: { end: vi.fn() }
    }

    const start = vi.fn().mockReturnValue(child)

    // Linux with no WAYLAND_DISPLAY / no WSL_INTEROP — falls through xclip then xsel, both fail
    await expect(writeClipboardText('hello', 'linux', start, {})).resolves.toBe(false)
  })

  it('writes text to pbcopy on macOS', async () => {
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(writeClipboardText('hello world', 'darwin', start as any)).resolves.toBe(true)
    expect(start).toHaveBeenCalledWith(
      'pbcopy',
      [],
      expect.objectContaining({ stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true })
    )
    expect(stdin.end).toHaveBeenCalledWith('hello world')
  })

  it('returns false when pbcopy fails', async () => {
    const child = {
      once: vi.fn((event: string, cb: () => void) => {
        if (event === 'error') {
          cb()
        }

        return child
      }),
      stdin: { end: vi.fn() }
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(writeClipboardText('hello world', 'darwin', start as any)).resolves.toBe(false)
  })

  it('uses wl-copy on Wayland Linux', async () => {
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(
      writeClipboardText('wayland text', 'linux', start as any, { WAYLAND_DISPLAY: 'wayland-1' })
    ).resolves.toBe(true)
    expect(start).toHaveBeenCalledWith(
      'wl-copy',
      ['--type', 'text/plain'],
      expect.objectContaining({ stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true })
    )
    expect(stdin.end).toHaveBeenCalledWith('wayland text')
  })

  it('falls back to xclip when wl-copy fails on Wayland', async () => {
    let callCount = 0
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          callCount++
          // wl-copy fails, xclip succeeds
          cb(callCount === 1 ? 1 : 0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(writeClipboardText('x11 text', 'linux', start as any, { WAYLAND_DISPLAY: 'wayland-1' })).resolves.toBe(
      true
    )
    expect(start).toHaveBeenNthCalledWith(1, 'wl-copy', ['--type', 'text/plain'], expect.anything())
    expect(start).toHaveBeenNthCalledWith(2, 'xclip', ['-selection', 'clipboard', '-in'], expect.anything())
  })

  it('falls back to xsel when both wl-copy and xclip fail', async () => {
    let callCount = 0
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          callCount++
          cb(callCount < 3 ? 1 : 0) // first two fail, third (xsel) succeeds
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(
      writeClipboardText('xsel text', 'linux', start as any, { WAYLAND_DISPLAY: 'wayland-1' })
    ).resolves.toBe(true)
    expect(start).toHaveBeenNthCalledWith(3, 'xsel', ['--clipboard', '--input'], expect.anything())
  })

  it('uses PowerShell on WSL2 when WSL_DISTRO_NAME is set', async () => {
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(writeClipboardText('wsl text', 'linux', start as any, { WSL_DISTRO_NAME: 'Ubuntu' })).resolves.toBe(
      true
    )
    expect(start).toHaveBeenCalledWith(
      'powershell.exe',
      expect.arrayContaining(['-NoProfile', '-NonInteractive']),
      expect.anything()
    )
    expect(stdin.end).toHaveBeenCalledWith('wsl text')
  })

  it('prefers the Windows clipboard path over wl-copy inside WSLg', async () => {
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(
      writeClipboardText('wslg text', 'linux', start as any, {
        WAYLAND_DISPLAY: 'wayland-0',
        WSL_DISTRO_NAME: 'Ubuntu'
      })
    ).resolves.toBe(true)
    expect(start).toHaveBeenNthCalledWith(
      1,
      'powershell.exe',
      expect.arrayContaining(['-NoProfile', '-NonInteractive']),
      expect.anything()
    )
    expect(stdin.end).toHaveBeenCalledWith('wslg text')
  })

  it('uses PowerShell on Windows', async () => {
    const stdin = { end: vi.fn() }

    const child = {
      once: vi.fn((event: string, cb: (code?: number) => void) => {
        if (event === 'close') {
          cb(0)
        }

        return child
      }),
      stdin
    }

    const start = vi.fn().mockReturnValue(child)

    await expect(writeClipboardText('windows text', 'win32', start as any)).resolves.toBe(true)
    expect(start).toHaveBeenCalledWith(
      'powershell',
      expect.arrayContaining(['-NoProfile', '-NonInteractive']),
      expect.anything()
    )
  })
})

describe('copyResultNotice', () => {
  it('says the copy only left as an escape sequence', () => {
    // The defect this closes: over SSH with no native clipboard tool, a copy
    // that never reached the user's terminal still reported a flat "copied 42
    // characters". The one case where the user has something to fix is the
    // one case the message has to name.
    const notice = copyResultNotice(42, 'osc52')

    expect(notice).toContain('42')
    expect(notice).toContain('OSC 52')
    expect(notice.toLowerCase()).toContain('terminal')
  })

  it('reports a native copy without a caveat', () => {
    // pbcopy/wl-copy actually wrote the clipboard, so hedging here would
    // train the user to ignore the wording in the case that matters.
    const notice = copyResultNotice(7, 'native')

    expect(notice).toBe('copied 7 characters')
  })

  it('names the tmux buffer as the thing that was written', () => {
    // tmux load-buffer succeeded; whether that reaches the system clipboard
    // is the user's set-clipboard setting, not something we can claim.
    const notice = copyResultNotice(9, 'tmux-buffer')

    expect(notice).toContain('9')
    expect(notice).toContain('tmux')
  })

  it('counts one character as one, not as a plural', () => {
    expect(copyResultNotice(1, 'native')).toBe('copied 1 character')
  })
})

describe('copyOnSelectNotice', () => {
  it('carries the path caveat on the first copy of a session', () => {
    // The user has to learn once that OSC 52 is best-effort and where the
    // switch lives. The first drag is the only moment that lands.
    expect(copyOnSelectNotice(42, 'osc52', true)).toBe(copyResultNotice(42, 'osc52'))
  })

  it('goes terse after that', () => {
    // This fires on every drag. Repeating a full sentence about terminal
    // settings would bury the transcript the feature exists to let you read.
    expect(copyOnSelectNotice(42, 'osc52', false)).toBe('sent 42 characters')
    expect(copyOnSelectNotice(1, 'tmux-buffer', false)).toBe('copied 1 character')
  })
})

describe('createCopyOnSelectReporter', () => {
  it('spends the caveat once per session, not once per process', () => {
    // A new or resumed session replaces the sid under a component that stays
    // mounted. Carrying one flag across that boundary loses the caveat for
    // every session after the first, which is where a user meets an OSC 52
    // paste that silently came up empty.
    const report = createCopyOnSelectReporter()

    expect(report(42, 'osc52', 's1')).toBe(copyResultNotice(42, 'osc52'))
    expect(report(42, 'osc52', 's1')).toBe('sent 42 characters')
    expect(report(42, 'osc52', 's2')).toBe(copyResultNotice(42, 'osc52'))
  })

  it('does not repeat the caveat when a session is returned to', () => {
    // The caveat is about this session having been told, so resuming one that
    // already heard it has nothing to add.
    const report = createCopyOnSelectReporter()

    report(42, 'osc52', 's1')
    report(42, 'osc52', 's2')

    expect(report(42, 'osc52', 's1')).toBe('sent 42 characters')
  })

  it('keeps its own tally per reporter', () => {
    // Two TUI processes must not share the fact that one of them has reported.
    expect(createCopyOnSelectReporter()(42, 'osc52', 's1')).toBe(copyResultNotice(42, 'osc52'))
    expect(createCopyOnSelectReporter()(42, 'osc52', 's1')).toBe(copyResultNotice(42, 'osc52'))
  })
})

describe('copyResultNotice honesty', () => {
  it('does not claim a copy on the one path whose outcome it cannot see', () => {
    // OSC 52 writes bytes to the terminal and the terminal decides whether to
    // honour them -- and silently drops an oversized sequence. A 2000-row
    // drag-scroll selection is a single half-megabyte escape sequence that no
    // terminal accepts, and setClipboard() still reports success because bytes
    // were written. "copied" is a claim about the outcome; "sent" is what we
    // actually know.
    expect(copyResultNotice(42, 'osc52')).toContain('sent 42 characters')
    expect(copyResultNotice(42, 'osc52')).not.toContain('copied')
  })

  it('still says copied where a native tool really wrote the clipboard', () => {
    expect(copyResultNotice(42, 'native')).toBe('copied 42 characters')
  })

  it('counts what a reader would call a character, not utf-16 code units', () => {
    // Three emoji are six code units. Reporting "6 characters" for a
    // three-character selection is a small lie in the one line whose whole
    // job is telling the truth about the copy.
    expect(copyResultNotice([...'\u{1f389}\u{1f389}\u{1f389}'].length, 'native')).toBe('copied 3 characters')
    expect(graphemeCount('\u{1f389}\u{1f389}\u{1f389}')).toBe(3)
    expect(graphemeCount('\u4f60\u597d\u4e16\u754c')).toBe(4)
    expect(graphemeCount('hello')).toBe(5)
    // e + combining acute is one character on screen and two code points.
    expect(graphemeCount('e\u0301cole')).toBe(5)
  })
})
