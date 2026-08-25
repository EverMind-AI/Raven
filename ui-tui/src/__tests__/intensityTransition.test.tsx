// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Bold and dim share one end code (`22m`) but are separate attributes, so a
// style transition that swaps one for the other has to reset first. Getting it
// wrong left dim switched on underneath bold text, which reads as grey on
// screen and heals on any repaint -- the shape that made it hard to catch.
// These render the swap for real and read the styles back off a terminal model.

import { Box, renderSync, Text } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import { TerminalScreen } from './support/terminalScreen.js'

// The renderer only emits colour and dim at a non-zero chalk level, and the
// suite runs without a terminal. HERMES_TUI_LEVEL pins it, and it has to be set
// before `@hermes/ink` initialises chalk -- vitest.setup would be too late for
// a module-scope import, so this file is the only place it can go.
const paint = async (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(40, 4)

  Object.assign(stdout, { columns: 40, isTTY: true, rows: 4 })
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

const styleOf = (screen: TerminalScreen, needle: string, row = 0) => {
  const line = screen.text().split('\n')[row] ?? ''

  return screen.styleAt(line.indexOf(needle), row)
}

describe('intensity transitions', () => {
  it('does not leave dim on the bold run that follows it', async () => {
    const screen = await paint(
      <Text>
        <Text color="#858482" dim>
          dim
        </Text>
        <Text bold color="#fbe23f">
          BOLD
        </Text>
      </Text>
    )

    expect(styleOf(screen, 'BOLD')).not.toContain('dim')
    expect(styleOf(screen, 'BOLD')).toContain('bold')
  })

  it('does not leave bold on the dim run that follows it', async () => {
    const screen = await paint(
      <Text>
        <Text bold color="#fbe23f">
          bold
        </Text>
        <Text color="#858482" dim>
          DIMMED
        </Text>
      </Text>
    )

    expect(styleOf(screen, 'DIMMED')).not.toContain('bold')
    expect(styleOf(screen, 'DIMMED')).toContain('dim')
  })

  it('clears intensity entirely when the next run has none', async () => {
    const screen = await paint(
      <Text>
        <Text color="#858482" dim>
          dim
        </Text>
        <Text color="#fbe23f">PLAIN</Text>
      </Text>
    )

    expect(styleOf(screen, 'PLAIN')).toBe('fg:2;251;226;63')
  })

  it('survives a wide-character run, where the spacer cells are skipped', async () => {
    const screen = await paint(
      <Box flexDirection="column">
        <Text>
          <Text color="#858482" dim>
            \u5f15\u7528
          </Text>
          <Text bold color="#fbe23f">
            \u6807\u9898
          </Text>
        </Text>
      </Box>
    )

    expect(styleOf(screen, '\u6807')).not.toContain('dim')
  })
})
