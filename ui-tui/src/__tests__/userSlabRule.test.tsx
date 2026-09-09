// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Renders through hermes-ink (renderSync), not ink-testing-library: the app's
// own renderer is the one that paints borders and background fills, and the
// upstream `ink` that ink-testing-library pulls in has neither this fork's
// border-background behaviour nor its fill geometry.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type { Msg } from '../types.js'

import { MessageLine } from '../components/messageLine.js'
import { DEFAULT_THEME } from '../theme.js'
import { TerminalScreen } from './support/terminalScreen.js'

const COLS = 48

const paint = (msg: Msg): TerminalScreen => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(COLS, 24)

  Object.assign(stdout, { columns: COLS, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => screen.write(chunk.toString()))

  const instance = renderSync(React.createElement(MessageLine, { cols: COLS, msg, t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return screen
}

const userMsg = (text: string): Msg => ({ id: 'u1', kind: 'text', role: 'user', text }) as Msg

/** Rows whose first column carries the rule glyph, and what style it had. */
const ruleRows = (screen: TerminalScreen) => {
  const found: Array<{ row: number; style: string }> = []

  for (let row = 0; row < screen.rows; row++) {
    const [glyph, style] = screen.styledRow(row)[0] ?? ['', '']

    if (glyph === '▎') {
      found.push({ row, style })
    }
  }

  return found
}

describe('the rule down the user slab', () => {
  it('runs the full slab height, pad rows included', () => {
    // Long enough to wrap at 48 columns, so the slab is taller than one row.
    const rows = ruleRows(paint(userMsg('a wrapped prompt that has to run past the right edge of a narrow pane')))

    expect(rows.length).toBeGreaterThan(3)
    // Contiguous: a rule with a hole in it is the bug this replaced.
    expect(rows.map(r => r.row)).toEqual(rows.map((_, i) => rows[0]!.row + i))
  })

  it('sits in column 0, flush against the slab', () => {
    const screen = paint(userMsg('flush left'))
    const rows = ruleRows(screen)

    expect(rows.length).toBeGreaterThan(0)
    // Column 0 is where the glyph was found, and the cell beside it is the
    // slab's own blank -- nothing indents the mark off the edge.
    expect(screen.styledRow(rows[0]!.row)[1]?.[0]).toBe(' ')
  })

  it('paints the rule cell on the slab background, not the terminal ground', () => {
    const screen = paint(userMsg('background continuity'))
    const rows = ruleRows(screen)
    const ruleStyle = rows[0]!.style
    // The cell just inside the rule is pure slab fill; the rule cell must carry
    // the same background, or a sliver of terminal ground shows between the two.
    const fillStyle = screen.styleAt(1, rows[0]!.row)

    const bgOf = (style: string) => /bg:[\d;]+/.exec(style)?.[0]

    expect(bgOf(ruleStyle)).toBeDefined()
    expect(bgOf(fillStyle)).toBeDefined()
    expect(bgOf(ruleStyle)).toBe(bgOf(fillStyle))
  })

  it('carries the accent as its foreground', () => {
    const screen = paint(userMsg('accent rule'))

    const { primary } = DEFAULT_THEME.color
    const rgb = [1, 3, 5].map(i => parseInt(primary.slice(i, i + 2), 16)).join(';')

    expect(ruleRows(screen)[0]!.style).toContain(`fg:2;${rgb}`)
  })

  it('leaves the assistant gutter alone', () => {
    const screen = paint({ id: 'a1', kind: 'text', role: 'assistant', text: 'a reply' } as Msg)

    expect(ruleRows(screen)).toHaveLength(0)
  })
})
