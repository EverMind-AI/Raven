// SPDX-License-Identifier: MIT
// Portions Copyright (c) original ink contributors (vadimdemedes/ink, MIT).
// Portions Copyright (c) 2025 Nous Research (hermes-agent / hermes-ink, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-{hermes-agent,ink}.txt.

import { describe, expect, it } from 'vitest'

import { INITIAL_STATE, parseMultipleKeypresses } from './parse-keypress.js'
import { PASTE_END, PASTE_START } from './termio/csi.js'

describe('parseMultipleKeypresses bracketed paste recovery', () => {
  it('emits empty bracketed pastes when the terminal sends both markers', () => {
    const [keys, state] = parseMultipleKeypresses(INITIAL_STATE, PASTE_START + PASTE_END)

    expect(keys).toHaveLength(1)
    expect(keys[0]).toMatchObject({ isPasted: true, raw: '' })
    expect(state.mode).toBe('NORMAL')
  })

  it('flushes unterminated paste content back to normal input mode', () => {
    const [pendingKeys, pendingState] = parseMultipleKeypresses(INITIAL_STATE, PASTE_START + 'hello')

    expect(pendingKeys).toEqual([])
    expect(pendingState.mode).toBe('IN_PASTE')

    const [keys, state] = parseMultipleKeypresses(pendingState, null)

    expect(keys).toHaveLength(1)
    expect(keys[0]).toMatchObject({ isPasted: true, raw: 'hello' })
    expect(state.mode).toBe('NORMAL')
    expect(state.pasteBuffer).toBe('')
  })

  it('resets an empty unterminated paste start instead of staying stuck', () => {
    const [pendingKeys, pendingState] = parseMultipleKeypresses(INITIAL_STATE, PASTE_START)

    expect(pendingKeys).toEqual([])
    expect(pendingState.mode).toBe('IN_PASTE')

    const [keys, state] = parseMultipleKeypresses(pendingState, null)

    expect(keys).toEqual([])
    expect(state.mode).toBe('NORMAL')
    expect(state.pasteBuffer).toBe('')
  })
})

describe('mouse wheel modifier decoding', () => {
  // SGR mouse format: ESC [ < button ; col ; row M
  // Wheel up = 64 (0x40), wheel down = 65 (0x41).
  // Modifier bits: shift = 0x04, meta = 0x08, ctrl = 0x10.
  const sgrWheel = (button: number) => `\x1b[<${button};10;10M`

  it('plain wheel up has no modifiers', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x40))

    expect(key).toMatchObject({ name: 'wheelup', ctrl: false, meta: false, shift: false })
  })

  it('plain wheel down has no modifiers', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x41))

    expect(key).toMatchObject({ name: 'wheeldown', ctrl: false, meta: false, shift: false })
  })

  it('decodes meta (Alt/Option) on wheel up', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x40 | 0x08))

    expect(key).toMatchObject({ name: 'wheelup', ctrl: false, meta: true, shift: false })
  })

  it('decodes meta (Alt/Option) on wheel down', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x41 | 0x08))

    expect(key).toMatchObject({ name: 'wheeldown', ctrl: false, meta: true, shift: false })
  })

  it('decodes ctrl on wheel events', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x40 | 0x10))

    expect(key).toMatchObject({ name: 'wheelup', ctrl: true, meta: false, shift: false })
  })

  it('decodes shift on wheel events', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x41 | 0x04))

    expect(key).toMatchObject({ name: 'wheeldown', ctrl: false, meta: false, shift: true })
  })

  it('decodes combined modifiers', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, sgrWheel(0x40 | 0x08 | 0x10))

    expect(key).toMatchObject({ name: 'wheelup', ctrl: true, meta: true, shift: false })
  })

  it('decodes meta on legacy X10 wheel encoding', () => {
    // X10: ESC [ M Cb Cx Cy where each byte is value+32.
    const x10 = `\x1b[M${String.fromCharCode(0x40 + 0x08 + 32)}${String.fromCharCode(10 + 32)}${String.fromCharCode(10 + 32)}`
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, x10)

    expect(key).toMatchObject({ name: 'wheelup', meta: true })
  })
})

describe('fragmented SGR mouse recovery', () => {
  it('re-synthesizes bracket-only SGR mouse tails as mouse events', () => {
    const [[mouse]] = parseMultipleKeypresses(INITIAL_STATE, '[<35;159;11M')

    expect(mouse).toMatchObject({ kind: 'mouse', button: 35, col: 159, row: 11, action: 'press' })
  })

  it('re-synthesizes angle-only SGR mouse tails as mouse events', () => {
    const [[mouse]] = parseMultipleKeypresses(INITIAL_STATE, '<35;159;11M')

    expect(mouse).toMatchObject({ kind: 'mouse', button: 35, col: 159, row: 11, action: 'press' })
  })

  it('re-synthesizes degraded SGR mouse bursts without leaking prompt text', () => {
    const [events] = parseMultipleKeypresses(INITIAL_STATE, '5;142;11M<35;159;11M35;124;26M35;119;26Mtyped')

    expect(events.slice(0, 4)).toEqual([
      expect.objectContaining({ kind: 'mouse', button: 5, col: 142, row: 11 }),
      expect.objectContaining({ kind: 'mouse', button: 35, col: 159, row: 11 }),
      expect.objectContaining({ kind: 'mouse', button: 35, col: 124, row: 26 }),
      expect.objectContaining({ kind: 'mouse', button: 35, col: 119, row: 26 })
    ])
    expect(events[4]).toMatchObject({ kind: 'key', sequence: 'typed' })
  })

  it('keeps isolated semicolon text that only resembles a prefixless mouse report', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, 'see 1;2;3M for details')

    expect(key).toMatchObject({ kind: 'key', sequence: 'see 1;2;3M for details' })
  })

  it('does not match prefixless fragments inside longer digit runs', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '1234;56;78M9;10;11M')

    expect(key).toMatchObject({ kind: 'key', sequence: '1234;56;78M9;10;11M' })
  })
})

// modifier+enter parsing across raw byte, CSI u (kitty keyboard) and
// modifyOtherKeys (xterm) paths. textInput dispatches on
// k.return + k.{shift,ctrl,meta}; the parser is the source of those flags.
// alt+enter raw `\x1b\r` is intentionally left empty-named — textInput's
// text fall-through normalizes `\r → \n` and inserts a newline universally
// regardless of protocol push state.
describe('modifier+enter parsing (tui-composer-multiline regression lock)', () => {
  it('plain CR is return with no modifiers (submit path)', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\r')

    expect(key).toMatchObject({ name: 'return', ctrl: false, meta: false, shift: false })
  })

  it('plain LF is return with no modifiers (ctrl+j legacy)', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\n')

    expect(key).toMatchObject({ name: 'return', ctrl: false, meta: false, shift: false })
  })

  it('CRLF parses as a single return with no modifier (paste line ending; future-proof against parser regressions)', () => {
    // The parser must not start matching the 2-byte CRLF as 'return' with a
    // modifier flag — that would falsely trigger newline-insert in
    // textInput.tsx when pasted CRLF reaches the keypress path. Today the
    // parser falls through to empty-name; lock that.
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\r\n')

    expect(key.ctrl).toBe(false)
    expect(key.meta).toBe(false)
    expect(key.shift).toBe(false)
  })

  it('alt+enter raw ESC+CR is intentionally empty-named (text fall-through path)', () => {
    // textInput picks up sequence='\x1b\r' via fall-through, strips ESC in
    // input-event.ts, normalizes `\r → \n`, inserts newline. Do NOT add
    // `\x1b\r → {return,meta:true}` to parseKeypress — fall-through is the
    // load-bearing path here and adding a parser branch creates two
    // sources of truth.
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b\r')

    expect(key).toMatchObject({ kind: 'key', name: '', sequence: '\x1b\r' })
  })

  it('alt+enter raw ESC+LF is also empty-named (alt+ctrl+j variant)', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b\n')

    expect(key).toMatchObject({ kind: 'key', name: '', sequence: '\x1b\n' })
  })

  it('CSI u shift+enter (kitty keyboard) parses with shift modifier', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[13;2u')

    expect(key).toMatchObject({ name: 'return', shift: true, ctrl: false, meta: false })
  })

  it('CSI u alt+enter (kitty keyboard) parses with meta modifier', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[13;3u')

    expect(key).toMatchObject({ name: 'return', meta: true, ctrl: false, shift: false })
  })

  it('CSI u ctrl+enter (kitty keyboard) parses with ctrl modifier', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[13;5u')

    expect(key).toMatchObject({ name: 'return', ctrl: true, meta: false, shift: false })
  })

  it('CSI u ctrl+shift+enter (kitty keyboard) parses with both modifiers', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[13;6u')

    expect(key).toMatchObject({ name: 'return', ctrl: true, shift: true, meta: false })
  })

  it('modifyOtherKeys shift+enter (xterm CSI 27;modifier;keycode~) parses with shift', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[27;2;13~')

    expect(key).toMatchObject({ name: 'return', shift: true, ctrl: false, meta: false })
  })

  it('modifyOtherKeys ctrl+enter parses with ctrl', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[27;5;13~')

    expect(key).toMatchObject({ name: 'return', ctrl: true, meta: false, shift: false })
  })

  it('CSI u enter with explicit modifier=1 (no modifiers) is plain return', () => {
    const [[key]] = parseMultipleKeypresses(INITIAL_STATE, '\x1b[13;1u')

    expect(key).toMatchObject({ name: 'return', ctrl: false, meta: false, shift: false })
  })
})
