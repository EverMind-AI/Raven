// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The app hands the active theme's selection colors to the ink instance on
// mount and whenever the theme changes. Selection is painted by ink's own
// StylePool, so the effect is the only link between the theme and what a
// selected cell looks like; without it the theme's selection colors are dead
// values. The foreground half had no test: deleting its effect left the whole
// suite green.

import type * as Ink from '@hermes/ink'

import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { GatewayClient } from '../gatewayClientStub.js'

const selection = vi.hoisted(() => ({
  clearSelection: vi.fn(),
  copySelection: vi.fn(async () => ({ path: null, text: '' })),
  copySelectionNoClear: vi.fn(async () => ({ path: null, text: '' })),
  getState: vi.fn(() => null),
  hasSelection: vi.fn(() => false),
  setSelectionBgColor: vi.fn(),
  setSelectionFgColor: vi.fn(),
  subscribe: vi.fn(() => () => {}),
  version: vi.fn(() => 0)
}))

vi.mock('@hermes/ink', async importOriginal => {
  const actual = await importOriginal<typeof Ink>()

  return {
    ...actual,
    useApp: () => ({ exit: () => {} }),
    useHasSelection: () => false,
    useInput: () => {},
    useSelection: () => selection,
    useStdin: () => ({
      internal_exitOnCtrlC: false,
      isRawModeSupported: false,
      setRawMode: () => {},
      stdin: process.stdin
    }),
    useStdout: () => ({ stdout: undefined }),
    useTerminalTitle: () => {}
  }
})

const { renderSync, Text } = await import('@hermes/ink')
const { useMainApp } = await import('../app/useMainApp.js')
const { GatewayClientStub } = await import('../gatewayClientStub.js')
const { patchUiState, resetUiState } = await import('../app/uiStore.js')
const { DEFAULT_THEME } = await import('../theme.js')

const flush = () => new Promise(resolve => setTimeout(resolve, 25))

const waitFor = async (assertion: () => void) => {
  const deadline = Date.now() + 1000

  for (;;) {
    try {
      assertion()

      return
    } catch (err) {
      if (Date.now() > deadline) {
        throw err
      }

      await new Promise(resolve => setTimeout(resolve, 10))
    }
  }
}

function Harness({ gw }: { gw: GatewayClient }) {
  useMainApp(gw)

  return <Text>harness</Text>
}

const mount = (element: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })

  return renderSync(element, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })
}

describe('selection theme wiring', () => {
  afterEach(() => {
    resetUiState()
    vi.clearAllMocks()
  })

  it('pushes the theme selection foreground to ink on mount and on theme change', async () => {
    resetUiState()

    const instance = mount(<Harness gw={new GatewayClientStub() as unknown as GatewayClient} />)

    await flush()

    await waitFor(() => expect(selection.setSelectionFgColor).toHaveBeenLastCalledWith(DEFAULT_THEME.color.selectionFg))

    const next = {
      ...DEFAULT_THEME,
      color: {
        ...DEFAULT_THEME.color,
        selectionBg: 'ansi:cyan',
        selectionFg: 'ansi:black'
      }
    }

    patchUiState({ theme: next })

    await waitFor(() => expect(selection.setSelectionFgColor).toHaveBeenLastCalledWith('ansi:black'))
    await waitFor(() => expect(selection.setSelectionBgColor).toHaveBeenLastCalledWith('ansi:cyan'))

    instance.unmount()
    instance.cleanup()
  })
})
