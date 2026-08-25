// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The copy-on-select confirmation: a transient line above the composer that
// replaces itself on every new copy and clears after its duration, instead of
// stacking permanent transcript lines.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  AppLayoutActions,
  AppLayoutComposerProps,
  AppLayoutProps,
  AppLayoutStatusProps,
  CompletionItem,
  GatewayServices
} from '../app/interfaces.js'
import type { Msg } from '../types.js'

import { $copyNotice, dismissCopyNotice, showCopyNotice } from '../app/copyNoticeStore.js'
import { GatewayProvider } from '../app/gatewayContext.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { stripAnsi } from '../lib/text.js'

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

// The renderer skips blank cells by moving the cursor with CSI sequences
// instead of writing spaces, so 'copied 7 characters' arrives as
// 'copied<ESC>[1C7<ESC>[1Ccharacters'. Turn those into spaces before
// stripping the rest of the ANSI, so plain-text assertions see real gaps.
const cursorForward = new RegExp(`${String.fromCharCode(27)}\\[(\\d+)?C`, 'g')

const toPlainFrame = (raw: string): string =>
  stripAnsi(raw.replace(cursorForward, (_, n) => ' '.repeat(n ? parseInt(n, 10) : 1)))

const renderFrame = ({ setup }: { setup?: () => void } = {}): string => {
  resetUiState()
  patchUiState({ statusBar: 'bottom' })
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

  const instance = renderSync(<App />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return toPlainFrame(output)
}

describe('copy notice store', () => {
  afterEach(() => {
    vi.useRealTimers()
    dismissCopyNotice()
  })

  it('clears itself after its duration', () => {
    vi.useFakeTimers()

    showCopyNotice('copied 7 characters', 3000)

    expect($copyNotice.get()).toBe('copied 7 characters')

    vi.advanceTimersByTime(2999)

    expect($copyNotice.get()).toBe('copied 7 characters')

    vi.advanceTimersByTime(1)

    expect($copyNotice.get()).toBeNull()
  })

  it('replaces the previous notice instead of stacking', () => {
    vi.useFakeTimers()

    showCopyNotice('copied 7 characters', 3000)
    showCopyNotice('sent 42 characters', 3000)

    expect($copyNotice.get()).toBe('sent 42 characters')
  })

  it("lets a re-shown notice outlive the previous one's deadline", () => {
    vi.useFakeTimers()

    showCopyNotice('copied 7 characters', 3000)
    vi.advanceTimersByTime(2900)
    showCopyNotice('sent 42 characters', 5000)

    // The first notice's deadline passes; the newer one must survive it.
    vi.advanceTimersByTime(100)

    expect($copyNotice.get()).toBe('sent 42 characters')

    vi.advanceTimersByTime(4900)

    expect($copyNotice.get()).toBeNull()
  })

  it('honours a per-notice duration', () => {
    vi.useFakeTimers()

    showCopyNotice('sent 42 characters', 5000)

    vi.advanceTimersByTime(3000)

    expect($copyNotice.get()).toBe('sent 42 characters')

    vi.advanceTimersByTime(2000)

    expect($copyNotice.get()).toBeNull()
  })

  it('dismissing clears the notice and leaves a later one to its own timer', () => {
    vi.useFakeTimers()

    showCopyNotice('copied 7 characters', 3000)
    dismissCopyNotice()

    expect($copyNotice.get()).toBeNull()

    showCopyNotice('sent 42 characters', 3000)
    vi.advanceTimersByTime(2999)

    expect($copyNotice.get()).toBe('sent 42 characters')

    vi.advanceTimersByTime(1)

    expect($copyNotice.get()).toBeNull()
  })
})

describe('copy notice rendering', () => {
  afterEach(() => {
    dismissCopyNotice()
  })

  it('shows the notice above the composer while one is set', () => {
    const frame = renderFrame({ setup: () => showCopyNotice('copied 7 characters', 3000) })

    expect(frame).toContain('copied 7 characters')

    // At statusBar='bottom' the cwd label sits below the input box, so the
    // notice must render somewhere above it.
    expect(frame.indexOf('copied 7 characters')).toBeLessThan(frame.indexOf('~/repo'))
  })

  it('renders nothing for the notice once it has been dismissed', () => {
    dismissCopyNotice()

    const frame = renderFrame()

    expect(frame).not.toContain('copied 7 characters')
  })
})
