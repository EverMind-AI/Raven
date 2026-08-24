// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { fitsFastAppend } from '../components/textInput.js'

// Fast append writes the character straight to the terminal and defers the
// React update to the next frame, so a keystroke that takes it costs no
// render. The guard only has to hold what the terminal itself does not: the
// insert lands at the end of a single line, and the caret stays on that line.
describe('fitsFastAppend', () => {
  const COLS = 80

  it('takes an ASCII character appended at the end of a line', () => {
    expect(fitsFastAppend('abc', 3, 'd', 3, COLS)).toBe(true)
  })

  it('takes a wide character - the terminal advances two cells on its own', () => {
    expect(fitsFastAppend('ab', 2, '你', 2, COLS)).toBe(true)
  })

  it('takes an emoji that occupies one grapheme', () => {
    expect(fitsFastAppend('ab', 2, '🙂', 2, COLS)).toBe(true)
  })

  it('rejects an insert away from the end of the value', () => {
    expect(fitsFastAppend('abc', 1, 'd', 3, COLS)).toBe(false)
  })

  it('rejects a value that already spans several lines', () => {
    expect(fitsFastAppend('a\nb', 3, 'c', 1, COLS)).toBe(false)
  })

  it('rejects the first character of an empty value', () => {
    expect(fitsFastAppend('', 0, 'a', 0, COLS)).toBe(false)
  })

  it('rejects a character that would reach the last column', () => {
    expect(fitsFastAppend('x'.repeat(79), 79, 'y', 79, COLS)).toBe(false)
  })

  it('rejects a wide character with only one column left', () => {
    expect(fitsFastAppend('x'.repeat(78), 78, '你', 78, COLS)).toBe(false)
  })

  it('rejects more than one grapheme', () => {
    expect(fitsFastAppend('ab', 2, 'cd', 2, COLS)).toBe(false)
  })

  it('rejects a zero-width combining mark', () => {
    expect(fitsFastAppend('ae', 2, '́', 2, COLS)).toBe(false)
  })
})
