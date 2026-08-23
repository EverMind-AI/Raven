// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: { gw: { request: vi.fn() }, rpc: vi.fn(async () => null) },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: { submitRef: { current: vi.fn() } },
    system: { bellOnComplete: false, sys: vi.fn() },
    transcript: { appendMessage: (msg: Msg) => appended.push(msg), panel: vi.fn(), setHistoryItems: vi.fn() },
    voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
  }) as any

const ask = (requestId: string) =>
  ({
    payload: { choices: ['keep going', 'stop'], question: 'which way?', request_id: requestId },
    type: 'clarify.request'
  }) as any

const withdraw = (requestId: string | undefined, reason = 'timeout') =>
  ({ payload: { conversation_id: 'tui:default', reason, request_id: requestId }, type: 'clarify.cancel' }) as any

/**
 * The choice box is drawn from clarify.request and, before clarify.cancel
 * existed, came down only on the answer or at end of turn.  An on-call wake gets
 * neither: the server-side timeout resolves the tool ten minutes before the turn
 * ends, and a cron turn runs in the cron:<job_id> conversation, which matches no
 * session subscription, so its end is never delivered here at all.
 *
 * Measured 2026-08-06 (round 11): "user did not answer" at 19:40:23, the next
 * round submitted at 19:40:55, and at 19:45 the box still showed the 19:30
 * question.  The failure direction is what makes it matter -- the box reads as
 * "it is waiting for me" while the loop had already decided and spent an hour of
 * budget, and answering it then does nothing the user expects.
 */
describe('clarify withdrawal', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ showReasoning: true })
  })

  it('takes the choice box down when the server says the question timed out', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(ask('q1'))
    expect(getOverlayState().clarify).not.toBeNull()

    onEvent(withdraw('q1'))

    expect(getOverlayState().clarify).toBeNull()
  })

  it('says on screen that it went on without an answer', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(ask('q1'))
    onEvent(withdraw('q1'))

    const activity = getTurnState().activity.map(item => item.text)

    expect(activity.join(' ')).toContain('timed out')
  })

  it('leaves a newer question alone when a stale withdrawal arrives', () => {
    // Removing a question the loop is still waiting on is the worse failure:
    // nothing on screen to answer, and the turn blocked until its own timeout.
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(ask('q1'))
    onEvent(ask('q2'))
    onEvent(withdraw('q1', 'superseded'))

    expect(getOverlayState().clarify).toMatchObject({ requestId: 'q2' })
  })

  it('withdraws without an id as a last resort', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(ask('q1'))
    onEvent(withdraw(undefined))

    expect(getOverlayState().clarify).toBeNull()
  })

  it('does nothing when no question is on screen', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(withdraw('q1'))

    expect(getOverlayState().clarify).toBeNull()
  })
})

const askConfirm = (requestId: string) =>
  ({ payload: { default: false, prompt: 'delete it?', request_id: requestId }, type: 'confirm.request' }) as any

const withdrawConfirm = (requestId: string | undefined, reason = 'timeout') =>
  ({ payload: { reason, request_id: requestId }, type: 'confirm.cancel' }) as any

describe('confirm withdrawal', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ showReasoning: true })
  })

  it('takes the confirm box down when the server says it timed out', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(askConfirm('c1'))
    expect(getOverlayState().confirm).not.toBeNull()

    onEvent(withdrawConfirm('c1'))

    expect(getOverlayState().confirm).toBeNull()
    expect(getTurnState().activity.map(i => i.text).join(' ')).toContain('timed out')
  })

  it('leaves a newer confirm alone when a stale withdrawal arrives', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(askConfirm('c1'))
    onEvent(askConfirm('c2'))
    onEvent(withdrawConfirm('c1', 'cancelled'))

    expect(getOverlayState().confirm).toMatchObject({ requestId: 'c2' })
  })

  it('does nothing when no confirm is on screen', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(withdrawConfirm('c1'))

    expect(getOverlayState().confirm).toBeNull()
  })
})
