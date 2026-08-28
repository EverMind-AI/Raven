// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Sending pins the transcript back to its bottom. A reader scrolled up has
// broken the ScrollBox's stickiness, and every submission path writes to the
// bottom of the view -- the prompt echo, a slash command's output, the queued
// list -- so without this the reply to what was just typed lands off-screen and
// the composer looks like it swallowed the message.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { resetDirectChat } from '../app/directChatStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'

const ref = <T,>(value: T) => ({ current: value })

const composerActions = () => ({
  clearIn: vi.fn(),
  dequeue: vi.fn(),
  enqueue: vi.fn(),
  handleTextPaste: vi.fn(),
  openEditor: vi.fn(),
  pushHistory: vi.fn(),
  removeQueue: vi.fn(),
  replaceQueue: vi.fn(),
  setCompIdx: vi.fn(),
  setHistoryIdx: vi.fn(),
  setInput: vi.fn(),
  setInputBuf: vi.fn(),
  setPasteSnips: vi.fn(),
  setQueueEdit: vi.fn(),
  syncQueue: vi.fn()
})

const composerState = {
  compIdx: 0,
  compReplace: 0,
  completions: [],
  historyIdx: null,
  input: '',
  inputBuf: [],
  pasteSnips: [],
  queueEditIdx: null,
  queuedDisplay: []
}

const mounted = (slash: (cmd: string) => boolean = () => false) => {
  const revealLatest = vi.fn()
  const submitRef = ref<(value: string) => void>(() => {})

  const Probe = () => {
    useSubmission({
      appendMessage: vi.fn(),
      chatStreamRef: ref<{ send: (content: string) => Promise<unknown> } | null>({
        send: vi.fn().mockResolvedValue(undefined)
      }),
      composerActions: composerActions() as never,
      composerRefs: {
        historyDraftRef: ref(''),
        historyRef: ref<string[]>([]),
        queueEditRef: ref<null | number>(null),
        queueRef: ref<string[]>([]),
        submitRef
      } as never,
      composerState: composerState as never,
      gw: { request: vi.fn().mockResolvedValue({ matched: false }) } as never,
      maybeGoodVibes: vi.fn(),
      revealLatest,
      setLastUserMsg: vi.fn(),
      slashRef: ref(slash),
      submitRef,
      sys: vi.fn()
    })

    return null
  }

  renderSync(<Probe />, { stdout: new PassThrough() as never })

  return { revealLatest, submit: (value: string) => submitRef.current(value) }
}

describe('a submission follows the transcript back down', () => {
  beforeEach(() => {
    resetUiState()
    resetDirectChat()
    patchUiState({ sid: 'sess-1' })
  })

  it('reveals the bottom when a prompt is sent', () => {
    const h = mounted()

    h.submit('what changed in the runner?')

    expect(h.revealLatest).toHaveBeenCalled()
  })

  it('reveals it for a slash command too, whose output also lands at the bottom', () => {
    const h = mounted(() => true)

    h.submit('/agents')

    expect(h.revealLatest).toHaveBeenCalled()
  })

  it('leaves the view alone when there is nothing to send', () => {
    // Enter on an empty composer is the interrupt / dequeue gesture, and a
    // whitespace-only line is not a message. Yanking a reader who is scrolled
    // up to the bottom for either would be scroll the composer never earned.
    const h = mounted()

    h.submit('   ')

    expect(h.revealLatest).not.toHaveBeenCalled()
  })
})
