import { renderSync } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getOverlayState, patchOverlayState, resetFlowOverlays, resetOverlayState } from '../app/overlayStore.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import { ApprovalPrompt } from '../components/prompts.js'
import {
  ALWAYS_OPTION,
  APPROVAL_OPTIONS,
  approvalOptionsFor,
  approvalRemainingSeconds,
  approvalResponseAccepted,
  buildApprovalRespond
} from '../lib/approval.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: () => undefined,
      setInput: () => undefined
    },
    gateway: {
      gw: { request: () => undefined },
      rpc: async () => null
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: () => undefined,
      resetSession: () => undefined,
      resumeById: () => undefined,
      setCatalog: () => undefined
    },
    submission: { submitRef: ref(() => undefined) },
    system: { bellOnComplete: false, sys: () => undefined },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: () => undefined,
      setHistoryItems: () => undefined
    },
    voice: {
      setProcessing: () => undefined,
      setRecording: () => undefined,
      setVoiceEnabled: () => undefined
    }
  }) as any

describe('approval round-trip', () => {
  beforeEach(() => {
    resetOverlayState()
    resetTurnState()
    resetUiState()
  })

  it('stores the approval id from the runtime notification', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent({
      payload: {
        approval_id: 'approval-a',
        command: 'rm file.txt',
        conversation_id: 'session-a',
        description: 'Delete files',
        expires_at: 1735689630
      },
      session_id: 'session-a',
      type: 'approval.request'
    } as any)

    expect(getOverlayState().approval).toEqual({
      approvalId: 'approval-a',
      command: 'rm file.txt',
      conversationId: 'session-a',
      description: 'Delete files',
      expiresAt: 1735689630000
    })
  })

  it('clears only the matching approval when runtime closes it', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent({
      payload: {
        approval_id: 'approval-a',
        command: 'rm file.txt',
        conversation_id: 'session-a',
        description: 'Delete files',
        expires_at: 1735689630
      },
      session_id: 'session-a',
      type: 'approval.request'
    } as any)
    onEvent({
      payload: {
        approval_id: 'approval-b',
        conversation_id: 'session-a',
        reason: 'timeout'
      },
      session_id: 'session-a',
      type: 'approval.closed'
    } as any)

    expect(getOverlayState().approval?.approvalId).toBe('approval-a')

    onEvent({
      payload: {
        approval_id: 'approval-a',
        conversation_id: 'session-a',
        reason: 'timeout'
      },
      session_id: 'session-a',
      type: 'approval.closed'
    } as any)

    expect(getOverlayState().approval).toBeNull()
  })

  it('offers allow once, allow for the session and the two refusals', () => {
    expect(APPROVAL_OPTIONS.map(o => o.choice)).toEqual(['allow', 'allow_session', 'deny', 'deny_stop'])
  })

  it('offers the persisted grant only when the runtime suggested a prefix, with the refusals last', () => {
    expect(approvalOptionsFor(undefined)).toBe(APPROVAL_OPTIONS)
    expect(approvalOptionsFor('')).toBe(APPROVAL_OPTIONS)
    expect(approvalOptionsFor('git push *').map(o => o.choice)).toEqual([
      'allow',
      'allow_session',
      'allow_always',
      'deny',
      'deny_stop'
    ])
    expect(ALWAYS_OPTION.fallback).toContain('{pattern}')
  })

  it('keeps the suggested prefix off the request when the runtime sent none', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent({
      payload: {
        approval_id: 'approval-a',
        command: 'git push origin HEAD',
        conversation_id: 'session-a',
        description: 'Push',
        expires_at: 1735689630,
        suggested_pattern: 'git push *'
      },
      session_id: 'session-a',
      type: 'approval.request'
    } as any)
    expect(getOverlayState().approval?.suggestedPattern).toBe('git push *')

    onEvent({
      payload: {
        approval_id: 'approval-b',
        command: 'uv run pytest',
        conversation_id: 'session-a',
        description: 'Run',
        expires_at: 1735689630,
        suggested_pattern: ''
      },
      session_id: 'session-a',
      type: 'approval.request'
    } as any)
    expect(getOverlayState().approval).not.toHaveProperty('suggestedPattern')
  })

  it('names its choices through i18n, so the keys follow the locale', async () => {
    // The labels used to be English literals captured in the module, which left
    // the one prompt that authorizes a command speaking a language the rest of
    // the TUI had already switched away from.
    const { setLocale, t } = await import('../i18n/index.js')
    const { UI_TEXT } = await import('../i18n/messages.generated.js')
    const before = APPROVAL_OPTIONS.map(o => t(o.key, o.fallback))
    expect(before).toEqual(['Allow once', 'Allow for this session', 'Deny (agent continues)', 'Deny and stop the turn'])

    // Compared against the catalogue rather than spelled out: the zh text is
    // the catalogue's to own, and this file is not a zh fixture zone.
    setLocale('zh')
    try {
      const zh = APPROVAL_OPTIONS.map(o => t(o.key, o.fallback))
      expect(zh).toEqual(APPROVAL_OPTIONS.map(o => UI_TEXT.zh[o.key]))
      expect(zh.every(label => label && !before.includes(label))).toBe(true)
    } finally {
      setLocale('en')
    }
  })

  it('builds a response bound to the approval and session', () => {
    expect(buildApprovalRespond('approval-a', 'session-a', 'deny')).toEqual({
      approval_id: 'approval-a',
      choice: 'deny',
      session_id: 'session-a'
    })
  })

  it('carries the pattern only with a persisted grant that has one', () => {
    expect(buildApprovalRespond('approval-a', 'session-a', 'allow_always', '', 'git push *')).toEqual({
      approval_id: 'approval-a',
      choice: 'allow_always',
      pattern: 'git push *',
      session_id: 'session-a'
    })
    expect(buildApprovalRespond('approval-a', 'session-a', 'allow_session')).toEqual({
      approval_id: 'approval-a',
      choice: 'allow_session',
      session_id: 'session-a'
    })
  })

  it('carries feedback only when a sentence was typed', () => {
    expect(buildApprovalRespond('approval-a', 'session-a', 'deny', 'use the draft dir')).toEqual({
      approval_id: 'approval-a',
      choice: 'deny',
      feedback: 'use the draft dir',
      session_id: 'session-a'
    })
    expect(buildApprovalRespond('approval-a', 'session-a', 'deny_stop')).toEqual({
      approval_id: 'approval-a',
      choice: 'deny_stop',
      session_id: 'session-a'
    })
  })

  it('accepts only an explicit ok response', () => {
    expect(approvalResponseAccepted({ ok: true })).toBe(true)
    expect(approvalResponseAccepted({ ok: false })).toBe(false)
    expect(approvalResponseAccepted(null)).toBe(false)
  })

  it('derives the visible countdown from the runtime deadline', () => {
    expect(approvalRemainingSeconds(31_000, 1_000)).toBe(30)
    expect(approvalRemainingSeconds(1_001, 1_000)).toBe(1)
    expect(approvalRemainingSeconds(999, 1_000)).toBe(0)
  })

  it('answers nothing at the visible deadline, because a lapse is not a refusal', async () => {
    /* It used to send `deny` here, and that was the one place a lapse became a
       refusal. The runtime's hard ceiling sits a few seconds past this deadline
       so that it can be the party that decides; answering first took the
       decision away from it and told the model the user had said no. The
       overlay is cleared by the runtime's own `approval.closed`. */
    vi.useFakeTimers()
    vi.setSystemTime(1_000)
    const onChoice = vi.fn()
    const rendered = render(
      React.createElement(ApprovalPrompt, {
        onChoice,
        req: {
          approvalId: 'approval-a',
          command: 'rm file.txt',
          conversationId: 'session-a',
          description: 'Delete files',
          expiresAt: 2_000
        },
        t: DEFAULT_THEME
      })
    )

    try {
      await vi.advanceTimersByTimeAsync(1_000)
      expect(onChoice).not.toHaveBeenCalled()
      // And well past it: the prompt stays put rather than answering late.
      await vi.advanceTimersByTimeAsync(10_000)
      expect(onChoice).not.toHaveBeenCalled()
    } finally {
      rendered.unmount()
      vi.useRealTimers()
    }
  })
})

describe('an approval outlives the turn that opened it', () => {
  beforeEach(() => {
    resetOverlayState()
  })

  it('survives the end-of-turn overlay reset, because a sub-agent asks after its parent turn ends', () => {
    // A spawned sub-agent keeps working once the parent turn has ended, and the
    // command it asks about is still waiting on an answer. Wiping the prompt
    // here took it off the screen with nobody having answered, and the call
    // died at the runtime's hard timeout -- the sub-agent reporting that its
    // write "was denied or expired" with no prompt the user ever saw.
    patchOverlayState({
      approval: {
        approvalId: 'a-1',
        command: 'write_file {"path": "notes.md"}',
        conversationId: 'tui:1',
        description: 'Approve this action',
        expiresAt: Date.now() + 30_000
      },
      clarify: null
    })

    resetFlowOverlays()

    expect(getOverlayState().approval?.approvalId).toBe('a-1')
  })

  it('still clears what the end of a turn owns', () => {
    patchOverlayState({ sudo: { requestId: 's-1' } })

    resetFlowOverlays()

    expect(getOverlayState().sudo).toBeNull()
  })
})

describe('an answer names the request that produced it', () => {
  beforeEach(() => {
    resetOverlayState()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('cannot answer a replacement request, because the countdown answers nothing', () => {
    // The hazard this pinned: the countdown was armed against the request on
    // screen, and if a second one took the slot before the callback ran -- a
    // sub-agent asking a second after the spawn that created it was allowed --
    // the expiry refused the new one, which nobody had been shown. Carrying the
    // rendered prompt's id was the guard against that.
    //
    // The expiry no longer answers at all, so there is no callback to mis-target
    // and no id for it to carry. The runtime's own ceiling decides, and it knows
    // which request it is deciding about. Kept as the case that says so: a
    // countdown that starts answering again brings the whole hazard back.
    const answered: Array<[string, string | undefined]> = []
    const req = {
      approvalId: 'a-1',
      command: 'rm notes.md',
      conversationId: 'tui:1',
      description: 'Approve this action',
      expiresAt: Date.now() + 30_000
    }
    const app = render(
      React.createElement(ApprovalPrompt, {
        cols: 80,
        onChoice: (choice: string, _feedback?: string, approvalId?: string) => answered.push([choice, approvalId]),
        req,
        t: DEFAULT_THEME
      })
    )

    vi.advanceTimersByTime(31_000)
    app.unmount()

    expect(answered).toEqual([])
  })
})

describe('one prompt per request', () => {
  // ink routes keystrokes through useInput only when stdin looks like a raw-mode
  // TTY, which ink-testing-library's stub is not: same PassThrough harness as
  // the clarify prompt tests.
  it('drops a half-edited prefix when a new request replaces the old one', async () => {
    const noop = () => {}
    const delay = (ms: number) => new Promise(r => setTimeout(r, ms))
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    // A non-TTY stdout gets whole frames rather than cursor-movement diffs, so
    // what was written since the last reset is what the screen shows.
    Object.assign(stdout, { columns: 100, isTTY: false, rows: 30 })
    Object.assign(stdin, { isTTY: true, ref: noop, setRawMode: noop, unref: noop })
    Object.assign(stderr, { isTTY: true })
    let out = ''
    stdout.on('data', (d: Buffer) => {
      out += d.toString()
    })
    const onChoice = vi.fn()
    const first = {
      approvalId: 'approval-a',
      command: 'git push origin HEAD',
      conversationId: 'session-a',
      description: 'Push',
      expiresAt: Date.now() + 60_000,
      suggestedPattern: 'git push *'
    }
    const node = (req: typeof first) => React.createElement(ApprovalPrompt, { onChoice, req, t: DEFAULT_THEME })
    const instance = renderSync(node(first), {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    try {
      await delay(30)
      // Pick the persisted grant: the prefix opens for editing.
      out = ''
      stdin.write('3')
      await delay(40)
      expect(stripAnsi(out)).toContain('prefix rule to save')

      // Request B lands on the same instance, with no suggestion: the editor is
      // gone and the list is B's, with B's default.
      const second = { ...first, approvalId: 'approval-b', command: 'touch x', suggestedPattern: undefined }
      out = ''
      instance.rerender(node(second))
      await delay(40)
      expect(stripAnsi(out)).not.toContain('prefix rule to save')
      expect(stripAnsi(out)).toContain('Allow for this session')

      // Enter answers B with B's default (deny), never with A's prefix.
      stdin.write('\r')
      await delay(40)
      expect(onChoice).toHaveBeenCalledTimes(1)
      expect(onChoice.mock.calls[0]).toEqual(['deny', '', 'approval-b'])
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })
})
