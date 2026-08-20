// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { isLfReturn } from '../components/textInput.js'

// LF byte (\n, 0x0a) on raw stdin is treated as ctrl+enter (insert newline)
// without depending on Kitty/modifyOtherKeys protocol push, which is not
// honored across many SSH + zellij + VSCode chains. CR byte (\r) remains
// plain enter (submit). Pasted CRLF must not match (paste path is separate).
describe('isLfReturn (ctrl+enter detection via LF byte)', () => {
  it('returns true for bare LF (ctrl+enter / ctrl+j)', () => {
    expect(isLfReturn('\n')).toBe(true)
  })

  it('returns false for bare CR (plain enter — submit path)', () => {
    expect(isLfReturn('\r')).toBe(false)
  })

  it('returns false for ESC+CR (alt+enter — handled by text fall-through)', () => {
    expect(isLfReturn('\x1b\r')).toBe(false)
  })

  it('returns false for ESC+LF (alt+ctrl+j variant — handled elsewhere)', () => {
    expect(isLfReturn('\x1b\n')).toBe(false)
  })

  it('returns false for CSI u return sequences (kitty keyboard protocol)', () => {
    expect(isLfReturn('\x1b[13;5u')).toBe(false)
    expect(isLfReturn('\x1b[13;2u')).toBe(false)
    expect(isLfReturn('\x1b[13;3u')).toBe(false)
  })

  it('returns false for CRLF (pasted line ending, not a single keystroke)', () => {
    expect(isLfReturn('\r\n')).toBe(false)
  })

  it('returns false for empty / undefined / whitespace', () => {
    expect(isLfReturn(undefined)).toBe(false)
    expect(isLfReturn('')).toBe(false)
    expect(isLfReturn(' ')).toBe(false)
    expect(isLfReturn('\n\n')).toBe(false)
  })

  it('returns false for printable characters', () => {
    expect(isLfReturn('a')).toBe(false)
    expect(isLfReturn('n')).toBe(false)
    expect(isLfReturn('\\n')).toBe(false)
  })
})
