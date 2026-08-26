// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Guard for the double-Esc clear: the armed window has to show its offer on the
// composer's top border, and it must not steal a row from the input while it is
// idle (an unarmed rule and an armed one are the same height).

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type {
  AppLayoutActions,
  AppLayoutComposerProps,
  AppLayoutProps,
  AppLayoutStatusProps,
  GatewayServices
} from '../app/interfaces.js'
import type { EscapeState } from '../app/useInputHandlers.js'
import type { Msg } from '../types.js'

import { GatewayProvider } from '../app/gatewayContext.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { decideEscape } from '../app/useInputHandlers.js'
import { AppLayout } from '../components/appLayout.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { TerminalScreen } from './support/terminalScreen.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const HINT = 'esc again to clear'
const HISTORY: Msg[] = [{ role: 'user', text: 'PROMPT_ROW' }]

const actions: AppLayoutActions = {
  answerApproval: () => {},
  answerClarify: () => {},
  answerConfirm: () => {},
  answerSecret: () => {},
  answerSudo: () => {},
  clearSelection: () => {},
  deleteSessionWithFallback: async () => false,
  onModelSelect: () => {},
  resumeById: () => {}
}

const composer: AppLayoutComposerProps = {
  cols: 80,
  compIdx: 0,
  completions: [],
  empty: false,
  handleTextPaste: async () => null,
  input: 'a typed line',
  inputBuf: ['a typed line'],
  pagerPageSize: 10,
  queueEditIdx: null,
  queuedDisplay: [],
  submit: () => {},
  updateInput: () => {},
  voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
}

const status: AppLayoutStatusProps = {
  cwdLabel: '~/repo',
  goodVibesTick: 0,
  sessionStartedAt: null,
  statusColor: 'green',
  turnStartedAt: null,
  voiceLabel: ''
}

const gwServices = { gw: {}, rpc: async () => null } as unknown as GatewayServices

const props: AppLayoutProps = {
  actions,
  composer,
  mouseTracking: false,
  progress: { showProgressArea: false },
  status,
  transcript: {
    historyItems: HISTORY,
    scrollRef: { current: null },
    virtualHistory: {
      bottomSpacer: 0,
      end: HISTORY.length,
      measureRef: () => () => {},
      offsets: HISTORY.map((_, i) => i),
      start: 0,
      topSpacer: 0
    },
    virtualRows: HISTORY.map((msg, index) => ({ index, key: `r${index}`, msg }))
  }
}

const renderFrame = async (escClearArmed: boolean): Promise<string[]> => {
  resetUiState()
  resetOverlayState()
  resetTurnState()
  patchUiState({ escClearArmed, status: 'ready' })

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const screen = new TerminalScreen(80, 24)

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    screen.write(chunk.toString())
  })

  const instance = renderSync(
    <GatewayProvider value={gwServices}>
      <AppLayout {...props} />
    </GatewayProvider>,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  await delay(40)
  const lines = screen.text().split('\n')
  instance.unmount()
  instance.cleanup()

  return lines
}

const esc = (over: Partial<EscapeState> = {}): EscapeState => ({
  escClearArmed: false,
  hasPendingInput: false,
  ...over
})

describe('decideEscape', () => {
  it('ignores Esc with nothing to clear', () => {
    expect(decideEscape(esc())).toBe('ignore')
    expect(decideEscape(esc({ escClearArmed: true }))).toBe('ignore')
  })

  it('only arms on the first press over a typed line', () => {
    expect(decideEscape(esc({ hasPendingInput: true }))).toBe('arm-clear')
  })

  it('clears on the second press inside the window', () => {
    expect(decideEscape(esc({ escClearArmed: true, hasPendingInput: true }))).toBe('clear-input')
  })
})

describe('esc-clear hint', () => {
  it('offers the clear at the right end of the composer border once armed', async () => {
    const lines = await renderFrame(true)
    const row = lines.findIndex(line => line.includes(HINT))

    expect(row).toBeGreaterThan(-1)
    expect(lines[row]).toMatch(/─ esc again to clear ─$/)
  })

  it('leaves the border bare while unarmed', async () => {
    const lines = await renderFrame(false)

    expect(lines.some(line => line.includes(HINT))).toBe(false)
  })

  it('keeps the prompt row in place whether or not the hint is up', async () => {
    const armed = await renderFrame(true)
    const bare = await renderFrame(false)
    const promptRow = (lines: string[]) => lines.findIndex(line => line.includes('a typed line'))

    expect(promptRow(armed)).toBe(promptRow(bare))
  })
})
