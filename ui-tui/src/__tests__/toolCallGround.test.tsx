// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A tool call stands on filled ground, so a call and what it returned read as
// one object rather than as two runs of transcript -- what the DAG and spawn
// panels get from a border, at a row's budget.
//
// Painted for real rather than through `ink-testing-library`, which renders on
// upstream ink: only this fork carries a Box's `backgroundColor` down into the
// text inside it, so the harness that proves the ground is under the *words*
// has to be the fork's own renderer read back off a terminal model.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type { Episode } from '../types.js'

import { EpisodeView } from '../components/episodeView.js'
import { transcriptGutterWidth } from '../lib/inputMetrics.js'
import { DEFAULT_THEME } from '../theme.js'
import { TerminalScreen } from './support/terminalScreen.js'

const COLS = 64
const ROWS = 12
// EpisodeView's own width: the transcript keeps four columns off the terminal,
// and the card runs the whole of what is left -- the same right edge a DAG
// panel at the top depth lands on.
const CARD = COLS - 4

const paint = async (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(COLS, ROWS)

  Object.assign(stdout, { columns: COLS, isTTY: true, rows: ROWS })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    screen.write(chunk.toString())
  })

  const instance = renderSync(node, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await new Promise(resolve => setTimeout(resolve, 40))
  instance.unmount()
  instance.cleanup()

  return screen
}

const episodes: Episode[] = [
  {
    index: 0,
    narration: 'that is the shape of it',
    reasoning: '',
    tools: [{ id: 'a', name: 'read_file', summary: 'runner.py', ok: true, done: true, durationMs: 2400 }]
  }
]

const at = (screen: TerminalScreen, needle: string) => {
  const rows = screen.text().split('\n')
  const row = rows.findIndex(line => line.includes(needle))

  return { col: rows[row]!.indexOf(needle), row }
}

describe('a tool call row', () => {
  it('carries its ground behind the label, not only in the space around it', async () => {
    const screen = await paint(<EpisodeView cols={COLS} episodes={episodes} t={DEFAULT_THEME} />)
    const { col, row } = at(screen, 'read runner.py')

    expect(row).toBeGreaterThanOrEqual(0)
    expect(screen.styleAt(col, row)).toContain('bg:')
    // The margin the spinner uses is part of the card, and so is the run out to
    // the card's edge -- a fill that stopped at the last word would read as a
    // highlight on the text rather than as ground under the row.
    expect(screen.styleAt(0, row)).toContain('bg:')
    expect(screen.styleAt(CARD - 1, row)).toContain('bg:')
  })

  it('starts its label on the column every other row starts on', async () => {
    // The card must not indent what it wraps. The margin the spinner uses is
    // the same one the reply marker occupies, and a row that moved its text in
    // by a padding cell would be the only one in the view aligned with nothing.
    const screen = await paint(<EpisodeView cols={COLS} episodes={episodes} t={DEFAULT_THEME} />)

    const bodyColumn = transcriptGutterWidth('assistant', '')

    expect(at(screen, 'read runner.py').col).toBe(bodyColumn)
    expect(at(screen, 'that is the shape of it').col).toBe(bodyColumn)
  })

  it('leaves what the model itself said on the transcript ground', async () => {
    const screen = await paint(<EpisodeView cols={COLS} episodes={episodes} t={DEFAULT_THEME} />)
    const { col, row } = at(screen, 'that is the shape of it')

    expect(screen.styleAt(col, row)).not.toContain('bg:')
  })
})
