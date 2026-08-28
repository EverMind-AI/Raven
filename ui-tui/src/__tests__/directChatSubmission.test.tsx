// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Submitting while a turn is in flight. The rule itself is a pure function
// tested in directChatMode.test.ts; what is tested here is that the submission
// path consults it, ahead of the busy-input policy -- the guard used to sit
// downstream of that policy, which is the only state in which it can ever
// apply, so it was unreachable.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { directKey, enterDirect, getDirectTranscript, markRunning, resetDirectChat } from '../app/directChatStore.js'
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

interface Harness {
  actions: ReturnType<typeof composerActions>
  appendMessage: ReturnType<typeof vi.fn>
  request: ReturnType<typeof vi.fn>
  send: ReturnType<typeof vi.fn>
  submit: (value: string) => void
  sys: ReturnType<typeof vi.fn>
}

/** Mount `useSubmission` with stub collaborators and hand back its submit. */
const mounted = (slash: (cmd: string) => boolean = () => false): Harness => {
  const actions = composerActions()
  const appendMessage = vi.fn()
  const request = vi.fn().mockResolvedValue({ matched: false })
  const send = vi.fn().mockResolvedValue(undefined)
  const sys = vi.fn()
  const submitRef = ref<(value: string) => void>(() => {})

  const Probe = () => {
    useSubmission({
      appendMessage,
      chatStreamRef: ref<{ send: (content: string) => Promise<unknown> } | null>({ send }),
      composerActions: actions as never,
      composerRefs: {
        historyDraftRef: ref(''),
        historyRef: ref<string[]>([]),
        queueEditRef: ref<null | number>(null),
        queueRef: ref<string[]>([]),
        submitRef
      } as never,
      composerState: composerState as never,
      gw: { request } as never,
      maybeGoodVibes: vi.fn(),
      setLastUserMsg: vi.fn(),
      slashRef: ref<(cmd: string) => boolean>(slash),
      submitRef,
      sys
    })

    return null
  }

  const stdout = new PassThrough()
  renderSync(<Probe />, { stdout: stdout as never })

  return { actions, appendMessage, request, send, submit: (value: string) => submitRef.current(value), sys }
}

describe('submitting while a turn is in flight', () => {
  beforeEach(() => {
    resetUiState()
    resetDirectChat()
    patchUiState({ sid: 'sess-1' })
  })

  it('sends to one instance while another is replying', () => {
    // The point of per-instance lanes. Before them this went to the busy-input
    // policy, whose default mode tore down the reply the user was waiting for.
    markRunning({ agent: 'Coder', handle: 'h1' })
    enterDirect('Writer', 'h2')

    const h = mounted()
    h.submit('are you free?')

    expect(h.sys).not.toHaveBeenCalledWith(expect.stringContaining('is still replying'))
    expect(h.actions.clearIn).toHaveBeenCalled()
  })

  it('refuses a second prompt to the instance that is mid-reply', () => {
    markRunning({ agent: 'Coder', handle: 'h1' })
    enterDirect('Coder', 'h1')

    const h = mounted()
    h.submit('and another thing')

    // Into the instance's own transcript, not the main one: `sys` writes to the
    // main transcript, which in this view is not the one being read -- an
    // invisible refusal is indistinguishable from a hang.
    expect(getDirectTranscript(directKey('Coder', 'h1')).map(m => m.text)).toContain(
      'Coder/h1 is still replying; you can continue once it lands'
    )
    expect(h.sys).not.toHaveBeenCalled()
    // A direct view refuses the second prompt outright, so no busy-input mode
    // runs -- including the default, which would interrupt. Stopping the reply
    // is Ctrl+C's job (chatStream.cancel), not a send's side effect.
    expect(h.request).not.toHaveBeenCalled()
    expect(h.send).not.toHaveBeenCalled()
  })

  it('leaves the typed text in the composer when it refuses', () => {
    markRunning({ agent: 'Coder', handle: 'h1' })
    enterDirect('Coder', 'h1')

    const h = mounted()
    h.submit('and another thing')

    expect(h.actions.clearIn).not.toHaveBeenCalled()
  })

  it('leaves the main agent to the busy-input policy', () => {
    // interrupt / steer / queue act on the turn the user is looking at, and on
    // the main view that is the main agent's own.
    markRunning(null)

    const h = mounted()
    h.submit('stop, do this instead')

    expect(h.sys).not.toHaveBeenCalledWith(expect.stringContaining('is still replying'))
    expect(h.actions.clearIn).toHaveBeenCalled()
  })
})

describe('echoing a slash command', () => {
  beforeEach(() => {
    resetUiState()
    resetDirectChat()
    patchUiState({ sid: 'sess-1' })
  })

  it('echoes into the instance transcript while a direct chat is open', () => {
    // The view renders `visibleRows`, which in a direct chat is the instance's
    // own rows -- an echo on the main transcript is invisible here, and shows up
    // in a conversation that never ran the command.
    enterDirect('Researcher', 'h1')

    const ran: string[] = []
    const h = mounted(cmd => (ran.push(cmd), true))
    h.submit('/mode deep')

    expect(getDirectTranscript(directKey('Researcher', 'h1')).map(m => m.text)).toContain('/mode deep')
    expect(h.appendMessage).not.toHaveBeenCalled()
    expect(ran).toEqual(['/mode deep'])
  })

  it('echoes into the main transcript on the main view', () => {
    const h = mounted(() => true)
    h.submit('/status')

    expect(h.appendMessage).toHaveBeenCalledWith({ kind: 'slash', role: 'system', text: '/status' })
    expect(getDirectTranscript(directKey('Researcher', 'h1'))).toEqual([])
  })
})
