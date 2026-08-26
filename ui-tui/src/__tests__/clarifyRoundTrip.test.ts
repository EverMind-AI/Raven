import { beforeEach, describe, expect, it } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'

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

const request = (requestId: string) =>
  ({
    payload: {
      batch: [{ header: '', question: 'Which weather?' }],
      choices: ['sun', 'rain'],
      conversation_id: 'tui:c1',
      header: '',
      index: 0,
      question: 'Which weather?',
      recommended: '',
      request_id: requestId,
      timeout_s: 600,
      total: 1
    },
    session_id: 'tui:c1',
    type: 'clarify.request'
  }) as any

const closed = (requestId: string) =>
  ({
    payload: { conversation_id: 'tui:c1', request_id: requestId },
    session_id: 'tui:c1',
    type: 'clarify.closed'
  }) as any

describe('clarify round-trip', () => {
  beforeEach(() => {
    resetOverlayState()
    resetTurnState()
    resetUiState()
  })

  it('survives the end of the turn that asked', () => {
    // A `spawn`ed sub-agent asks after the spawning turn has already replied,
    // and the answer is still wanted. Dropping the sheet at idle left the
    // question pending on the backend with nothing on screen to answer it.
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(request('q1'))
    expect(getOverlayState().clarify?.requestId).toBe('q1')

    turnController.idle()

    expect(getOverlayState().clarify?.requestId).toBe('q1')
  })

  it('closes only the question the backend named', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent(request('q1'))
    // A close for an older question must not clear the one now on screen: the
    // backend's fail-safe for a superseded question can land after the next
    // request. Same id match `approval.closed` makes.
    onEvent(closed('q0'))
    expect(getOverlayState().clarify?.requestId).toBe('q1')

    onEvent(closed('q1'))
    expect(getOverlayState().clarify).toBeNull()
  })
})
