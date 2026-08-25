// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Placement guard: the turn's live indicator belongs at the tail of the
// transcript -- the spot the reply lands in -- not in the status rule below
// the composer, where it used to sit.

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
import type { Msg } from '../types.js'

import { GatewayProvider } from '../app/gatewayContext.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { TerminalScreen } from './support/terminalScreen.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const CWD_LABEL = '~/repo'
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
  resumeById: () => {},
  setStickyPrompt: () => {}
}

const composer: AppLayoutComposerProps = {
  cols: 80,
  compIdx: 0,
  completions: [],
  empty: true,
  handleTextPaste: async () => null,
  input: '',
  inputBuf: [],
  pagerPageSize: 10,
  queueEditIdx: null,
  queuedDisplay: [],
  submit: () => {},
  updateInput: () => {},
  voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
}

const gwServices = { gw: {}, rpc: async () => null } as unknown as GatewayServices

const makeProps = (turnStartedAt: null | number): AppLayoutProps => {
  const status: AppLayoutStatusProps = {
    cwdLabel: CWD_LABEL,
    goodVibesTick: 0,
    sessionStartedAt: null,
    showStickyPrompt: false,
    statusColor: 'green',
    stickyPrompt: '',
    turnStartedAt,
    voiceLabel: ''
  }

  return {
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
}

const renderFrame = async (busy: boolean, turnStartedAt: null | number, live?: () => void): Promise<string[]> => {
  resetUiState()
  resetOverlayState()
  resetTurnState()
  patchUiState({ busy, status: busy ? 'running…' : 'ready' })
  live?.()

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
      <AppLayout {...makeProps(turnStartedAt)} />
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

const rowOf = (lines: string[], needle: string) => lines.findIndex(line => line.includes(needle))

describe('working indicator placement', () => {
  it('draws the elapsed turn above the status rule while busy', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000)
    const indicatorRow = rowOf(lines, '· 5s')
    const statusRow = rowOf(lines, CWD_LABEL)

    expect(indicatorRow).toBeGreaterThanOrEqual(0)
    expect(statusRow).toBeGreaterThanOrEqual(0)
    expect(indicatorRow).toBeLessThan(statusRow)
  })

  it('draws it below the last transcript row, where the reply lands', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000)

    expect(rowOf(lines, '· 5s')).toBeGreaterThan(rowOf(lines, 'PROMPT_ROW'))
  })

  it('keeps the status rule static -- no elapsed turn down there', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000)
    const statusLine = lines[rowOf(lines, CWD_LABEL)] ?? ''

    expect(statusLine).toContain('running…')
    expect(statusLine).not.toContain('· 5s')
  })

  it('draws nothing when idle', async () => {
    const lines = await renderFrame(false, null)

    expect(rowOf(lines, '· 5s')).toBe(-1)
  })

  it('steps aside once the reply is streaming', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000, () => {
      patchTurnState({ streaming: 'STREAMED_REPLY' })
    })

    expect(rowOf(lines, 'STREAMED_REPLY')).toBeGreaterThanOrEqual(0)
    expect(rowOf(lines, '· 5s')).toBe(-1)
  })

  it('steps aside while a tool is running, which shows its own progress', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000, () => {
      patchTurnState({ tools: [{ context: 'repo', id: 't1', name: 'search_files', startedAt: Date.now() }] })
    })

    expect(rowOf(lines, '· 5s')).toBe(-1)
  })

  it('steps aside while reasoning is on screen, which ticks its own elapsed', async () => {
    // The reasoning row already has a spinner, a duration, and a live tail of
    // the reasoning itself. Two clocks on one wait is one too many.
    const lines = await renderFrame(true, Date.now() - 5_000, () => {
      patchTurnState({ reasoning: 'weighing how to open the explanation', reasoningStreaming: true })
    })

    expect(rowOf(lines, '\u00b7 5s')).toBe(-1)
  })

  it('stays for reasoning that has arrived but says nothing yet', async () => {
    // An empty channel is not a row on screen: whitespace or punctuation alone
    // renders no reasoning row, so there is nothing to stand in for this one.
    const lines = await renderFrame(true, Date.now() - 5_000, () => {
      patchTurnState({ reasoning: '\n  ', reasoningStreaming: true })
    })

    expect(rowOf(lines, '\u00b7 5s')).toBeGreaterThanOrEqual(0)
  })

  it('comes back after the tool result, before the text that follows it', async () => {
    const lines = await renderFrame(true, Date.now() - 5_000, () => {
      patchTurnState({
        streamSegments: [{ role: 'assistant', text: 'EARLIER_SEGMENT' }],
        streaming: '',
        tools: []
      })
    })

    expect(rowOf(lines, '· 5s')).toBeGreaterThan(rowOf(lines, 'EARLIER_SEGMENT'))
  })
})
