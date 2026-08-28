// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Renders through hermes-ink so the screen model can be asked what colour each
// cell ended up: ink-testing-library brings its own upstream `ink`, and every
// assertion here is about ink rather than about text.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type { Episode } from '../types.js'

import { EpisodeView } from '../components/episodeView.js'
import { TRANSCRIPT_GUTTER_INSET, transcriptGutterWidth } from '../lib/inputMetrics.js'
import { DEFAULT_THEME } from '../theme.js'
import { TerminalScreen } from './support/terminalScreen.js'

const COLS = 80
const { color } = DEFAULT_THEME

const ink = (hex: string) => `fg:2;${[1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)).join(';')}`

const paint = async (episodes: Episode[], openKeys?: string[]): Promise<TerminalScreen> => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(COLS, 24)

  Object.assign(stdout, { columns: COLS, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => screen.write(chunk.toString()))

  const instance = renderSync(
    React.createElement(EpisodeView, { cols: COLS, episodes, live: false, openKeys, t: DEFAULT_THEME }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  await new Promise(resolve => setTimeout(resolve, 40))

  instance.unmount()
  instance.cleanup()

  return screen
}

/** The row carrying `needle`, as [glyph, style] pairs with blank tails cut. */
const rowWith = (screen: TerminalScreen, needle: string): Array<[string, string]> => {
  for (let row = 0; row < screen.rows; row++) {
    const cells = screen.styledRow(row)

    if (
      cells
        .map(([g]) => g)
        .join('')
        .includes(needle)
    ) {
      return cells
    }
  }

  throw new Error(`no row containing ${JSON.stringify(needle)}`)
}

/** What ink a run of text was painted in, by its first character. */
const inkOf = (cells: Array<[string, string]>, needle: string): string => {
  const text = cells.map(([g]) => g).join('')
  const at = text.indexOf(needle)

  if (at < 0) {
    throw new Error(`no ${JSON.stringify(needle)} in ${JSON.stringify(text.trimEnd())}`)
  }

  return (cells[at]![1].match(/fg:[\d;]+/) ?? [''])[0]
}

const episode = (tool: Record<string, unknown>): Episode[] =>
  [
    {
      reasoning: 'where the biggest file is',
      reasoningMs: 1200,
      text: '',
      tools: [{ done: true, id: 'c1', ok: true, startedAt: Date.now() - 9000, ...tool }]
    }
  ] as unknown as Episode[]

const READ = episode({ durationMs: 2400, name: 'read_file', summary: '/tmp/NOTICE.txt' })

describe('activity row ink', () => {
  it('gives the verb the emphasis and the target a step back', async () => {
    const cells = rowWith(await paint(READ), 'read')

    expect(inkOf(cells, 'read')).toBe(ink(color.text))
    expect(inkOf(cells, '/tmp/NOTICE.txt')).toBe(ink(color.muted))
  })

  it('paints the target at muted exactly, with no dim stacked on it', async () => {
    const cells = rowWith(await paint(READ), '/tmp/NOTICE.txt')
    const text = cells.map(([g]) => g).join('')
    const style = cells[text.indexOf('/tmp/NOTICE.txt')]![1]

    // `dim` would show up as its own SGR run rather than as a different colour,
    // so the assertion is on the absence of the attribute.
    expect(style).not.toContain('dim')
    expect(style).toContain(ink(color.muted))
  })

  it('marks a settled call in the margin instead of recolouring it', async () => {
    const cells = rowWith(await paint(READ), 'read')

    expect(inkOf(cells, '✓')).toBe(ink(color.ok))
  })

  it('marks a failure in the margin and leaves its text readable', async () => {
    const failed = episode({
      durationMs: 400,
      name: 'run_command',
      ok: false,
      resultPreview: 'Error: exit 2',
      summary: "rg 'TODO'"
    })
    const cells = rowWith(await paint(failed), '✗')

    expect(inkOf(cells, '✗')).toBe(ink(color.error))
    // The old row turned red end to end; the target stays the readable tier.
    expect(inkOf(cells, "rg 'TODO'")).toBe(ink(color.muted))
  })

  it('gives the reasoning row the same margin, quietly', async () => {
    const cells = rowWith(await paint(READ), 'thought')

    expect(inkOf(cells, '·')).toBe(ink(color.label))
    expect(inkOf(cells, 'thought')).toBe(ink(color.muted))
  })
})

describe('activity row geometry', () => {
  const filledRows = (screen: TerminalScreen) => {
    const rows: Array<{ row: number; blank: boolean; text: string }> = []

    for (let row = 0; row < screen.rows; row++) {
      const cells = screen.styledRow(row)

      if (cells.some(([, style]) => style.includes('bg:'))) {
        const text = cells
          .map(([glyph]) => glyph)
          .join('')
          .trim()

        rows.push({ blank: text === '', row, text })
      }
    }

    return rows
  }

  const WITH_OUTPUT = episode({
    durationMs: 3000,
    name: 'run_command',
    resultPreview: 'first line\nsecond line',
    summary: 'cat haiku.txt'
  })

  it('insets every mark one cell off the terminal edge', async () => {
    const screen = await paint(READ)
    const cells = rowWith(screen, 'read')

    expect(cells[0]![0]).toBe(' ')
    expect(cells[TRANSCRIPT_GUTTER_INSET]![0]).toBe('✓')
  })

  it('lands the marker, the reply dot and the reasoning dot on one column', async () => {
    const screen = await paint(episode({ durationMs: 2400, name: 'read_file', summary: '/tmp/x' }))
    const columnOf = (needle: string, glyph: string) => rowWith(screen, needle).findIndex(([g]) => g === glyph)

    expect(columnOf('read', '✓')).toBe(TRANSCRIPT_GUTTER_INSET)
    expect(columnOf('thought', '·')).toBe(TRANSCRIPT_GUTTER_INSET)
  })

  it('starts every row of text on the shared body column', async () => {
    const cells = rowWith(await paint(READ), 'read')
    const text = cells.map(([glyph]) => glyph).join('')

    expect(text.indexOf('read')).toBe(transcriptGutterWidth('assistant', ''))
  })

  it('opens a card with a blank row of its own ground above and below', async () => {
    const rows = filledRows(await paint(WITH_OUTPUT, ['seg:c1', 'call:c1']))

    expect(rows.length).toBeGreaterThan(3)
    expect(rows[0]!.blank).toBe(true)
    expect(rows[rows.length - 1]!.blank).toBe(true)
    // The call's own row is the first thing inside that air, not the top edge.
    expect(rows[1]!.text).toContain('cat haiku.txt')
  })

  it('leaves a collapsed row at exactly one row, air included', async () => {
    const rows = filledRows(await paint(READ))

    expect(rows).toHaveLength(1)
    expect(rows[0]!.blank).toBe(false)
  })
})
