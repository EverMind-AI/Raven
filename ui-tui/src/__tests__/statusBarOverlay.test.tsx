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
import { stripAnsi } from '../lib/text.js'

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
  resumeById: () => {},
  setStickyPrompt: () => {}
}

const status: AppLayoutStatusProps = {
  cwdLabel: '~/repo',
  goodVibesTick: 0,
  sessionStartedAt: null,
  showStickyPrompt: false,
  statusColor: 'green',
  stickyPrompt: '',
  turnStartedAt: null,
  voiceLabel: ''
}

const makeComposer = (completions: CompletionItem[]): AppLayoutComposerProps => ({
  cols: 80,
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

const makeProps = (completions: CompletionItem[]): AppLayoutProps => ({
  actions,
  composer: makeComposer(completions),
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

const App = ({ completions = [] }: { completions?: CompletionItem[] }) => (
  <GatewayProvider value={gwServices}>
    <AppLayout {...makeProps(completions)} />
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
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(<App completions={completions} />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  await delay(40)
  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
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
})
