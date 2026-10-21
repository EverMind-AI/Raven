// SPDX-License-Identifier: MIT
// Portions Copyright (c) original ink contributors (vadimdemedes/ink, MIT).
// Portions Copyright (c) 2025 Nous Research (hermes-agent / hermes-ink, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-{hermes-agent,ink}.txt.

import { describe, expect, it } from 'vitest'

import type { Diff, Frame } from './frame.js'
import { LogUpdate } from './log-update.js'
import { CellWidth, CharPool, createScreen, HyperlinkPool, type Screen, setCellAt, StylePool } from './screen.js'

/**
 * Contract tests for LogUpdate.render() — the diff-to-ANSI path that owns
 * whether the terminal picks up each React commit correctly.
 *
 * These tests pin down a few load-bearing invariants so that any fix for
 * the "scattered letters after rapid resize" artifact in xterm.js hosts
 * can be grounded against them.
 */

const stylePool = new StylePool()
const charPool = new CharPool()
const hyperlinkPool = new HyperlinkPool()

const mkScreen = (w: number, h: number) => createScreen(w, h, stylePool, charPool, hyperlinkPool)

const paint = (screen: Screen, y: number, text: string) => {
  for (let x = 0; x < text.length; x++) {
    setCellAt(screen, x, y, {
      char: text[x]!,
      styleId: stylePool.none,
      width: CellWidth.Narrow,
      hyperlink: undefined
    })
  }
}

const mkFrame = (screen: Screen, viewportW: number, viewportH: number, cursorY = 0): Frame => ({
  screen,
  viewport: { width: viewportW, height: viewportH },
  cursor: { x: 0, y: cursorY, visible: true }
})

const stdoutOnly = (diff: ReturnType<LogUpdate['render']>) =>
  diff
    .filter(p => p.type === 'stdout')
    .map(p => (p as { type: 'stdout'; content: string }).content)
    .join('')

describe('LogUpdate.render diff contract', () => {
  it('emits only changed cells when most rows match', () => {
    const w = 20
    const h = 4
    const prev = mkScreen(w, h)
    paint(prev, 0, 'HELLO')
    paint(prev, 1, 'WORLD')
    paint(prev, 2, 'STAYSHERE')

    const next = mkScreen(w, h)
    paint(next, 0, 'HELLO')
    paint(next, 1, 'CHANGE')
    paint(next, 2, 'STAYSHERE')
    next.damage = { x: 0, y: 0, width: w, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(mkFrame(prev, w, h), mkFrame(next, w, h), true, false)

    const written = stdoutOnly(diff)
    expect(written).toContain('CHANGE')
    expect(written).not.toContain('HELLO')
    expect(written).not.toContain('STAYSHERE')
  })

  it('width change emits a clearTerminal patch before repainting', () => {
    const prevW = 20
    const nextW = 15
    const h = 3

    const prev = mkScreen(prevW, h)
    paint(prev, 0, 'thiswaswiderrow')

    const next = mkScreen(nextW, h)
    paint(next, 0, 'shorterrownow')
    next.damage = { x: 0, y: 0, width: nextW, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(mkFrame(prev, prevW, h), mkFrame(next, nextW, h), true, false)

    expect(diff.some(p => p.type === 'clearTerminal')).toBe(true)
    expect(stdoutOnly(diff)).toContain('shorterrownow')
  })

  it('drift repro: identical prev/next emits no heal, even when the physical terminal is stale', () => {
    // Load-bearing theory for the rapid-resize scattered-letter bug: if the
    // physical terminal has stale cells that prev.screen doesn't know about
    // (e.g. resize-induced reflow wrote past ink's tracked range), the
    // renderer has no signal to heal them. LogUpdate.render only sees
    // prev/next — no view of the physical terminal — so when prev==next,
    // it emits nothing and any orphaned glyphs survive.
    //
    // The fix path is upstream of this diff: either (a) defensively
    // full-repaint on xterm.js frames where prevFrameContaminated is set,
    // or (b) close the drift window so prev.screen cannot diverge.
    const w = 20
    const h = 3

    const prev = mkScreen(w, h)
    paint(prev, 0, 'same')

    const next = mkScreen(w, h)
    paint(next, 0, 'same')
    next.damage = { x: 0, y: 0, width: w, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(mkFrame(prev, w, h), mkFrame(next, w, h), true, false)

    expect(stdoutOnly(diff)).toBe('')
    expect(diff.some(p => p.type === 'clearTerminal')).toBe(false)
  })

  it('ignores main-screen scrollback-only changes instead of resetting repeatedly', () => {
    const w = 20
    const viewportH = 5
    const h = 8

    const prev = mkScreen(w, h)
    paint(prev, 0, 'timer 1s')
    paint(prev, 6, 'visible prompt')

    const next = mkScreen(w, h)
    paint(next, 0, 'timer 2s')
    paint(next, 6, 'visible prompt')
    next.damage = { x: 0, y: 0, width: w, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(mkFrame(prev, w, viewportH, h), mkFrame(next, w, viewportH, h), false, false)

    expect(diff.some(p => p.type === 'clearTerminal')).toBe(false)
    expect(stdoutOnly(diff)).not.toContain('timer2s')
  })

  it('keeps alt-screen full reset for unreachable scrollback row changes', () => {
    const w = 20
    const viewportH = 5
    const h = 8

    const prev = mkScreen(w, h)
    paint(prev, 0, 'timer 1s')
    paint(prev, 6, 'visible prompt')

    const next = mkScreen(w, h)
    paint(next, 0, 'timer 2s')
    paint(next, 6, 'visible prompt')
    next.damage = { x: 0, y: 0, width: w, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })
    const diff = log.render(mkFrame(prev, w, viewportH, h), mkFrame(next, w, viewportH, h), true, false)

    expect(diff.some(p => p.type === 'clearTerminal')).toBe(true)
    expect(stdoutOnly(diff)).toContain('timer2s')
  })
})

/**
 * The bytes the terminal would see, keeping only what positions text:
 * written text and absolute column moves (CHA, rendered as the terminal
 * receives it). Style, hyperlink and row moves are dropped.
 */
const wire = (diff: Diff) =>
  diff
    .map(p => {
      switch (p.type) {
        case 'stdout':
          return p.content
        case 'cursorTo':
          return `\x1b[${p.col}G`
        default:
          return ''
      }
    })
    .join('')

const wide = (screen: Screen, x: number, y: number, char: string) =>
  setCellAt(screen, x, y, { char, styleId: stylePool.none, width: CellWidth.Wide, hyperlink: undefined })

describe('wide graphemes the terminal may size differently from the layout', () => {
  const w = 20
  const h = 2
  const AUTO_WRAP_OFF = '\x1b[?7l'
  const AUTO_WRAP_ON = '\x1b[?7h'
  const cha = /\x1b\[\d+G/

  // One row: "ab", the glyph under test at column 2, then "c" right after it.
  const renderRow = (glyph: string, at = 2) => {
    const prev = mkScreen(w, h)
    const next = mkScreen(w, h)
    paint(next, 0, 'ab')
    wide(next, at, 0, glyph)
    setCellAt(next, at + 2, 0, { char: 'c', styleId: stylePool.none, width: CellWidth.Narrow, hyperlink: undefined })
    next.damage = { x: 0, y: 0, width: w, height: h }

    const log = new LogUpdate({ isTTY: true, stylePool })

    return wire(log.render(mkFrame(prev, w, h), mkFrame(next, w, h), true, false))
  }

  it('writes a ZWJ emoji with auto-wrap off and pins the cursor to the column after it', () => {
    // Terminal.app advances five columns for U+1F426 U+200D U+2B1B, xterm.js
    // and pyte four; the layout counts two. Without the CHA the "c" and every
    // cell after it land three (or two) columns to the right.
    const out = renderRow('🐦‍⬛')

    // The styled space at column 3 first (for terminals that advance only
    // one column), then the glyph guarded against wrapping, then CHA to
    // column 5 (1-based: 2 + 2 + 1) and the next cell.
    expect(out).toContain(`\x1b[4G \x1b[3G${AUTO_WRAP_OFF}🐦‍⬛${AUTO_WRAP_ON}\x1b[5Gc`)
  })

  it('does the same for a skin-tone modifier and a VS16 emoji', () => {
    expect(renderRow('👍🏽')).toContain(`${AUTO_WRAP_OFF}👍🏽${AUTO_WRAP_ON}\x1b[5Gc`)
    expect(renderRow('❤️')).toContain(`${AUTO_WRAP_OFF}❤️${AUTO_WRAP_ON}\x1b[5Gc`)
  })

  it('writes a CJK character plainly', () => {
    const out = renderRow('中')

    expect(out).toContain('ab中c')
    expect(out).not.toContain(AUTO_WRAP_OFF)
    expect(out).not.toMatch(cha)
  })

  it('writes a single-code-point emoji plainly', () => {
    const out = renderRow('🐦')

    expect(out).toContain('ab🐦c')
    expect(out).not.toContain(AUTO_WRAP_OFF)
    expect(out).not.toMatch(cha)
  })

  it('still compensates a newer emoji without toggling auto-wrap', () => {
    // U+1FAE0 (melting face, Unicode 14): one code point, so it cannot
    // overrun the margin, but old wcwidth tables advance one column.
    const out = renderRow('🫠')

    expect(out).toContain('\x1b[4G \x1b[3G🫠\x1b[5Gc')
    expect(out).not.toContain(AUTO_WRAP_OFF)
  })

  it('keeps the guard at the last column a multi-code-point glyph may start in', () => {
    // Width 20: column 17 is the last start that keeps both cells inside
    // (17 + 2 < 20); the CHA then lands on column 20 (1-based).
    const out = renderRow('🐦‍⬛', 17)

    expect(out).toContain(`${AUTO_WRAP_OFF}🐦‍⬛${AUTO_WRAP_ON}\x1b[20G`)
  })

  it('skips a multi-code-point glyph that would touch the margin', () => {
    // Unchanged rule: at column 18 the glyph is not written at all, so no
    // terminal can be driven past the margin by it.
    const out = renderRow('🐦‍⬛', 18)

    expect(out).not.toContain('🐦‍⬛')
    expect(out).not.toContain(AUTO_WRAP_OFF)
  })
})
