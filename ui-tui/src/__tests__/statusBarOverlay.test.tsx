// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Regression guard: floating overlays must stay visible when the
// status bar sits below the input (statusBar='bottom'). A blocking overlay
// (pager / picker / skills hub) unmounts the input rows, collapsing the
// overlay's relative anchor box to height 0; at statusBar='bottom' the
// StatusRule sibling then shares the box's computed top, which previously
// tripped the renderer's height-0 skip and dropped the overlay entirely.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import type {
  AppLayoutActions,
  AppLayoutComposerProps,
  AppLayoutProps,
  AppLayoutStatusProps,
  CompletionItem,
  GatewayServices,
  StatusBarMode
} from '../app/interfaces.js'
import type { Msg } from '../types.js'

import { GatewayProvider } from '../app/gatewayContext.js'
import { patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { TerminalScreen } from './support/terminalScreen.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const HISTORY: Msg[] = Array.from({ length: 12 }, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  text: `transcript line ${i} lorem ipsum`
}))

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

const status: AppLayoutStatusProps = {
  cwdLabel: '~/repo',
  goodVibesTick: 0,
  sessionStartedAt: null,
  statusColor: 'green',
  turnStartedAt: null,
  voiceLabel: ''
}

const makeComposer = (completions: CompletionItem[], cols = 80): AppLayoutComposerProps => ({
  cols,
  compIdx: 0,
  completions,
  empty: completions.length === 0,
  handleTextPaste: async () => null,
  input: completions.length ? '/comp' : '',
  inputBuf: completions.length ? ['/comp'] : [],
  pagerPageSize: 10,
  queueEditIdx: null,
  queuedDisplay: [],
  submit: () => {},
  updateInput: () => {},
  voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
})

const gwServices = { gw: {}, rpc: async () => null } as unknown as GatewayServices

const makeProps = (completions: CompletionItem[], cols = 80): AppLayoutProps => ({
  actions,
  composer: makeComposer(completions, cols),
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
})

const App = ({ cols = 80, completions = [] }: { cols?: number; completions?: CompletionItem[] }) => (
  <GatewayProvider value={gwServices}>
    <AppLayout {...makeProps(completions, cols)} />
  </GatewayProvider>
)

// Render one frame at a fixed 80x24 viewport through the real @hermes/ink
// renderer and return the rendered screen text (ANSI stripped). `setup` runs
// after the stores are reset and statusBar is set, before the first render.
const renderFrame = async (
  mode: StatusBarMode,
  { completions = [], setup }: { completions?: CompletionItem[]; setup?: () => void } = {}
): Promise<string> => {
  resetUiState()
  resetOverlayState()
  patchUiState({ statusBar: mode })
  setup?.()

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

  const instance = renderSync(<App completions={completions} />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await delay(40)
  const frame = screen.text()
  instance.unmount()
  instance.cleanup()

  return frame
}

const PAGER_LINES = Array.from({ length: 8 }, (_, i) => `PAGERLINE_${i}`)
const openPager = () => patchOverlayState({ pager: { lines: PAGER_LINES, offset: 0, title: 'STATUS' } })

describe('floating overlays with statusBar position', () => {
  it('renders a blocking pager overlay on-screen when the status bar is at the bottom', async () => {
    const frame = await renderFrame('bottom', { setup: openPager })

    expect(frame).toContain('PAGERLINE_0')
    expect(frame).toContain('PAGERLINE_7')
  })

  it('still renders the pager overlay with the status bar at the top', async () => {
    const frame = await renderFrame('top', { setup: openPager })

    expect(frame).toContain('PAGERLINE_0')
    expect(frame).toContain('PAGERLINE_7')
  })

  it('renders the completion palette on-screen with the status bar at the bottom', async () => {
    const completions: CompletionItem[] = Array.from({ length: 6 }, (_, i) => ({
      display: `COMPLETION_${i}`,
      meta: `m${i}`,
      text: `/completion_${i}`
    }))

    const frame = await renderFrame('bottom', { completions })

    expect(frame).toContain('COMPLETION_0')
    expect(frame).toContain('COMPLETION_5')
  })

  it('clears a closed pager from the physical terminal screen', async () => {
    resetUiState()
    resetOverlayState()
    patchUiState({ statusBar: 'bottom' })
    openPager()

    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    const screen = new TerminalScreen(80, 24)

    Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
    Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
    Object.assign(stderr, { isTTY: true })
    stdout.on('data', chunk => screen.write(chunk.toString()))

    const instance = renderSync(<App />, {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    })

    try {
      await delay(40)
      expect(screen.text()).toContain('PAGERLINE_7')

      patchOverlayState({ pager: null })
      await delay(50)

      expect(screen.text()).not.toContain('PAGERLINE_0')
      expect(screen.text()).not.toContain('PAGERLINE_7')
      expect(screen.text()).toContain('~/repo')
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it('clears stale wide-frame cells after a terminal resize', async () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    const screen = new TerminalScreen(80, 24)

    Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
    Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
    Object.assign(stderr, { isTTY: true })
    stdout.on('data', chunk => screen.write(chunk.toString()))

    const wideCompletions: CompletionItem[] = [{ display: 'WIDE_TAIL', meta: 'old', text: '/wide' }]

    const instance = renderSync(<App completions={wideCompletions} />, {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    })

    try {
      await delay(30)
      expect(screen.text()).toContain('WIDE_TAIL')

      Object.assign(stdout, { columns: 36, rows: 12 })
      screen.resize(36, 12)
      stdout.emit('resize')
      instance.rerender(<App cols={36} />)
      await delay(50)

      expect(screen.text()).toContain('~/repo')
      expect(screen.text()).not.toContain('WIDE_TAIL')
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })
})
