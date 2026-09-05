import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import { ApprovalPrompt } from '../components/prompts.js'
import {
  APPROVAL_OPTIONS,
  approvalRemainingSeconds,
  approvalResponseAccepted,
  buildApprovalRespond
} from '../lib/approval.js'
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

  it('offers only allow once and deny', () => {
    expect(APPROVAL_OPTIONS).toEqual([
      { choice: 'allow', label: 'Allow once' },
      { choice: 'deny', label: 'Deny' }
    ])
  })

  it('builds a response bound to the approval and session', () => {
    expect(buildApprovalRespond('approval-a', 'session-a', 'deny')).toEqual({
      approval_id: 'approval-a',
      choice: 'deny',
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

  it('auto-denies and removes the visible choice at the runtime deadline', async () => {
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
      expect(onChoice).toHaveBeenCalledWith('deny')
    } finally {
      rendered.unmount()
      vi.useRealTimers()
    }
  })
})
