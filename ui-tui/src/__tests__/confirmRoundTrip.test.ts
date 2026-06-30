// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { buildConfirmRespond, tickCountdown } from '../lib/confirmCountdown.js'

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn() },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      sys: vi.fn()
    },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: vi.fn(),
      setHistoryItems: vi.fn()
    },
    voice: {
      setProcessing: vi.fn(),
      setRecording: vi.fn(),
      setVoiceEnabled: vi.fn()
    }
  }) as any

describe('confirm round-trip', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
    resetTurnState()
  })

  describe('confirm.request notification', () => {
    it('populates overlay.confirm with requestId/prompt/defaultAnswer and sets status', () => {
      const appended: Msg[] = []
      const onEvent = createGatewayEventHandler(buildCtx(appended))

      onEvent({
        payload: { default: false, prompt: 'Continue?', request_id: 'r1' },
        type: 'confirm.request'
      } as any)

      expect(getOverlayState().confirm).toMatchObject({
        defaultAnswer: false,
        prompt: 'Continue?',
        requestId: 'r1'
      })
      expect(getUiState().status).toBe('confirm needed')
    })
  })

  describe('answerConfirm round-trip', () => {
    // answerConfirm (useMainApp) builds {request_id, answer} via buildConfirmRespond
    // and clears the overlay on a truthy RPC result.  Drive that exact contract.
    const answerConfirm =
      (rpc: (m: string, p: Record<string, unknown>) => Promise<unknown>) => async (answer: boolean) => {
        const confirm = getOverlayState().confirm

        if (!confirm?.requestId) {
          return
        }

        const r = await rpc('confirm.respond', buildConfirmRespond(confirm.requestId, answer))

        if (r) {
          // Mirrors useMainApp.answerConfirm's done callback exactly.
          patchOverlayState({ confirm: null })
          patchUiState({ status: 'running…' })
        }
      }

    it('sends confirm.respond {request_id, answer:true} and clears the overlay', async () => {
      patchOverlayState({ confirm: { defaultAnswer: false, prompt: 'Continue?', requestId: 'r1' } as any })
      const rpc = vi.fn(async () => ({ ok: true }))

      await answerConfirm(rpc)(true)

      expect(rpc).toHaveBeenCalledWith('confirm.respond', { answer: true, request_id: 'r1' })
      expect(getOverlayState().confirm).toBeNull()
    })

    it('sends confirm.respond {request_id, answer:false} and clears the overlay', async () => {
      patchOverlayState({ confirm: { defaultAnswer: false, prompt: 'Continue?', requestId: 'r1' } as any })
      const rpc = vi.fn(async () => ({ ok: true }))

      await answerConfirm(rpc)(false)

      expect(rpc).toHaveBeenCalledWith('confirm.respond', { answer: false, request_id: 'r1' })
      expect(getOverlayState().confirm).toBeNull()
    })

    it('resets the status off "confirm needed" after answering', async () => {
      // Reproduce the live flow: confirm.request sets status, the answer must clear it.
      const onEvent = createGatewayEventHandler(buildCtx([]))
      onEvent({
        payload: { default: false, prompt: 'Continue?', request_id: 'r1' },
        type: 'confirm.request'
      } as any)
      expect(getUiState().status).toBe('confirm needed')

      const rpc = vi.fn(async () => ({ ok: true }))
      await answerConfirm(rpc)(true)

      expect(getOverlayState().confirm).toBeNull()
      expect(getUiState().status).not.toBe('confirm needed')
      expect(getUiState().status).toBe('running…')
    })
  })

  describe('countdown logic', () => {
    it('decrements remaining and does not auto-cancel above 0', () => {
      expect(tickCountdown(30)).toEqual({ autoCancel: false, remaining: 29 })
      expect(tickCountdown(2)).toEqual({ autoCancel: false, remaining: 1 })
    })

    it('reaching 0 signals auto-cancel (answer false)', () => {
      expect(tickCountdown(1)).toEqual({ autoCancel: true, remaining: 0 })
    })

    it('never decrements below 0 and keeps auto-cancel set at the floor', () => {
      expect(tickCountdown(0)).toEqual({ autoCancel: true, remaining: 0 })
    })
  })

  describe('ConfirmPrompt timer via fake timers', () => {
    it('clears the interval and auto-cancels when the countdown drains to 0', () => {
      vi.useFakeTimers()
      try {
        let remaining = 1
        let cancelled = false

        const interval = setInterval(() => {
          const { autoCancel, remaining: next } = tickCountdown(remaining)
          remaining = next

          if (autoCancel) {
            clearInterval(interval)
            cancelled = true
          }
        }, 1000)

        vi.advanceTimersByTime(1000)

        expect(remaining).toBe(0)
        expect(cancelled).toBe(true)

        // Interval is cleared — no further ticks fire.
        vi.advanceTimersByTime(5000)
        expect(remaining).toBe(0)
      } finally {
        vi.useRealTimers()
      }
    })

    it('suspending clears the interval so the countdown stops with no auto-cancel', () => {
      vi.useFakeTimers()
      try {
        let remaining = 30
        let cancelled = false

        const interval = setInterval(() => {
          const { autoCancel, remaining: next } = tickCountdown(remaining)
          remaining = next

          if (autoCancel) {
            cancelled = true
          }
        }, 1000)

        vi.advanceTimersByTime(3000)
        expect(remaining).toBe(27)

        // Any non-answer key suspends → clear the interval.
        clearInterval(interval)

        vi.advanceTimersByTime(60_000)
        expect(remaining).toBe(27)
        expect(cancelled).toBe(false)
      } finally {
        vi.useRealTimers()
      }
    })
  })
})
