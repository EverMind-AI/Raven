// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The paint-diagnosis dump: what the renderer believes the terminal shows.
// Its whole value is that a run boundary in the dump is exactly where the
// terminal gets an SGR change, so a "half this line is grey" report can be
// checked against the buffer instead of a screenshot.
//
// Run ids are the style pool's own. They collapse to a single 0 when the
// terminal takes no colour, which is this suite's environment -- so what is
// asserted here is the shape and the cell accounting, not the ids.

import { Box, dumpScreen, renderSync, Text } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import { parseWriteLog, replayWriteLog } from './support/replayWriteLog.js'
import { TerminalScreen } from './support/terminalScreen.js'

const render = async (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 30, isTTY: true, rows: 6 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', () => {})

  const instance = renderSync(node, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await new Promise(resolve => setTimeout(resolve, 40))
  const dump = dumpScreen(stdout as NodeJS.WriteStream)

  instance.unmount()
  instance.cleanup()

  return dump ?? ''
}

describe('dumpScreen', () => {
  it('reports the rows the renderer believes it painted', async () => {
    const dump = await render(
      <Box flexDirection="column">
        <Text>FIRST ROW</Text>
        <Text>SECOND ROW</Text>
      </Box>
    )

    expect(dump).toContain('FIRST ROW')
    expect(dump).toContain('SECOND ROW')
    expect(dump).toContain('screen 30x')
  })

  it('accounts for every cell in the row as style runs', async () => {
    const dump = await render(
      <Text>
        <Text color="green">GREEN</Text>
        <Text color="red">RED</Text>
      </Text>
    )

    const lines = dump.split('\n')
    const runs = lines[lines.findIndex(line => line.includes('GREENRED')) + 1] ?? ''
    const covered = [...runs.matchAll(/(\d+)x(\d+)/g)].reduce((n, m) => n + Number(m[2]), 0)

    expect(runs.trim().startsWith('styles:')).toBe(true)
    // Every column belongs to exactly one run -- a gap would hide the boundary
    // the diagnosis is looking for.
    expect(covered).toBe(30)
  })

  it('carries a legend so a run id can be read as the codes it sets', async () => {
    const dump = await render(<Text color="green">GREEN</Text>)

    expect(dump).toContain('style legend (id: SGR that sets it)')
  })

  it('returns null for a stream with nothing mounted', () => {
    expect(dumpScreen(new PassThrough() as unknown as NodeJS.WriteStream)).toBeNull()
  })
})

// The terminal model's own style tracking, since a paint comparison is only
// worth as much as the model it is measured against.
describe('TerminalScreen SGR tracking', () => {
  it('records the colour and weight each cell was written under', () => {
    const screen = new TerminalScreen(10, 1)

    screen.write('\u001b[38;2;1;2;3m\u001b[1mAB\u001b[22mC\u001b[39mD')

    expect(screen.styleAt(0, 0)).toBe('fg:2;1;2;3,bold')
    expect(screen.styleAt(2, 0)).toBe('fg:2;1;2;3')
    expect(screen.styleAt(3, 0)).toBe('')
  })

  it('resets every attribute on SGR 0', () => {
    const screen = new TerminalScreen(10, 1)

    screen.write('\u001b[1m\u001b[4mA\u001b[0mB')

    expect(screen.styleAt(0, 0)).toBe('bold,underline')
    expect(screen.styleAt(1, 0)).toBe('')
  })

  it('carries a wide char style across both of its cells', () => {
    const screen = new TerminalScreen(10, 1)

    screen.write('\u001b[1m\u4e2d')

    expect(screen.styleAt(0, 0)).toBe('bold')
    expect(screen.styleAt(1, 0)).toBe('bold')
  })
})

describe('write-log replay', () => {
  it('rebuilds the terminal cells and styles a session produced', () => {
    const log = [
      { mark: 'write-log opened', t: 1 },
      { d: '\u001b[1m\u001b[38;2;1;2;3mHI', t: 2 },
      { d: '\u001b[39m\u001b[22m there', t: 3 },
      { mark: 'paintdump /tmp/x.txt', t: 4 },
      { d: '\u001b[2J\u001b[HGONE', t: 5 }
    ]

    const atDump = replayWriteLog(log, { columns: 20, rows: 2, stopAtMark: 'paintdump' })

    expect(atDump.text().split('\n')[0]).toBe('HI there')
    expect(atDump.styleAt(0, 0)).toBe('fg:2;1;2;3,bold')
    expect(atDump.styleAt(3, 0)).toBe('')

    // Past the marker the screen was cleared -- proof the cut is honoured.
    const toEnd = replayWriteLog(log, { columns: 20, rows: 2 })

    expect(toEnd.text().split('\n')[0]).toBe('GONE')
  })

  it('parses a log file line by line', () => {
    const parsed = parseWriteLog('{"t":1,"mark":"a"}\n{"t":2,"d":"x"}\n\n')

    expect(parsed).toHaveLength(2)
    expect(parsed[1]?.d).toBe('x')
  })
})
