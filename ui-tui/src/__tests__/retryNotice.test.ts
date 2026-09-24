// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Where a transient runtime notice lands. It reports on a turn that is still
// running -- the Model-Error Ladder waiting out a failed call -- so it belongs
// on the status line and not in the transcript, and the next frame of real
// output has to take the line back: nothing else resets it before the turn ends,
// so a call that failed once and then answered would run to completion still
// saying it was trying again.

import { beforeEach, describe, expect, it } from 'vitest'

import type { TurnEvent, TurnSendResult } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { directKey, enterDirect, getDirectTranscript, resetDirectChat } from '../app/directChatStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, resetUiState } from '../app/uiStore.js'

// The catalogue sentences, spelled out rather than read back through the
// helper under test.
const TRYING_AGAIN = 'The model did not answer; trying again.'
const BLOCKED =
  'A safety rule stopped this operation, so the turn ended here. Say the word and I will carry on with the parts that do not need it.'

interface FakeRpc extends ChatStreamRpcClient {
  __pushEvent: (event: TurnEvent) => void
}

const makeFakeRpc = (): FakeRpc => {
  let handler: ((event: TurnEvent) => void) | null = null

  return {
    __pushEvent: (event: TurnEvent) => handler?.(event),
    async rpc<R, P>(method: string, _params: P): Promise<R> {
      if (method === 'turn.send') {
        return { turn_id: 'turn-1', accepted: true } as unknown as TurnSendResult as unknown as R
      }
      return {} as R
    },
    async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
      handler = h as unknown as (event: TurnEvent) => void

      return { subscription_id: 'sub-1', unsubscribe: async () => void (handler = null) }
    }
  }
}

const started = async (appended?: Msg[]) => {
  const fake = makeFakeRpc()
  const stream = createChatStream({
    appendMessage: msg => appended?.push(msg),
    rpcClient: fake,
    sessionKey: 'tui:default'
  })

  await stream.attach()
  await stream.send('build the deck')
  fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

  return fake
}

const retry = { kind: 'llm_retry', detail: 'server', transient: true }

describe('a retry notice claims the status line instead of the transcript', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    resetDirectChat()
    turnController.fullReset()
  })

  it('says the runtime is waiting, in the reader own words, and commits no row', async () => {
    const appended: Msg[] = []
    const fake = await started(appended)

    fake.__pushEvent({ type: 'notice', payload: retry })

    expect(getUiState().status).toBe(TRYING_AGAIN)
    expect(getTurnState().notice).toBe('')
    expect(appended).toEqual([])
  })

  it.each(['token.delta', 'thinking.delta', 'tool.start'] as const)(
    'gives the line back on the first %s that follows',
    async type => {
      const fake = await started()
      fake.__pushEvent({ type: 'notice', payload: retry })

      fake.__pushEvent(
        type === 'tool.start'
          ? { type, payload: { tool_call_id: 'call-1', name: 'read_file', arguments: {} } }
          : { type, payload: { text: 'the answer' } }
      )

      expect(getUiState().status).toBe('running…')
    }
  )

  it('leaves a closing notice on the path that commits it as a row', async () => {
    const fake = await started()

    fake.__pushEvent({ type: 'notice', payload: { kind: 'action_blocked', detail: 'policy refused' } })

    expect(getUiState().status).toBe('running…')
    expect(getTurnState().notice).toBe(`${BLOCKED}\npolicy refused`)
  })

  it('survives the episode boundary that opens the retried call', async () => {
    const fake = await started()

    fake.__pushEvent({ type: 'notice', payload: retry })
    fake.__pushEvent({ type: 'episode.start', payload: { index: 1 } })

    expect(getUiState().status).toBe(TRYING_AGAIN)

    fake.__pushEvent({ type: 'token.delta', payload: { text: 'the answer' } })

    expect(getUiState().status).toBe('running…')
  })

  it('keeps a tagged retry off the main transcript and out of the instance rows', async () => {
    const appended: Msg[] = []
    const fake = await started(appended)
    enterDirect('Raven-Code', 'refactor-auth')
    const target = { agent: 'Raven-Code', handle: 'refactor-auth' }

    fake.__pushEvent({ type: 'notice', payload: { ...retry, target } })

    expect(getUiState().status).toBe(TRYING_AGAIN)
    expect(getTurnState().notice).toBe('')
    expect(getDirectTranscript(directKey(target.agent, target.handle))).toEqual([])
    expect(appended).toEqual([])
  })

  it('draws a closing notice for an instance in that instance transcript', async () => {
    const appended: Msg[] = []
    const fake = await started(appended)
    enterDirect('Raven-Code', 'refactor-auth')
    const target = { agent: 'Raven-Code', handle: 'refactor-auth' }

    fake.__pushEvent({
      type: 'notice',
      payload: { kind: 'action_blocked', detail: 'policy refused', target }
    })

    expect(getDirectTranscript(directKey(target.agent, target.handle))).toEqual([
      { role: 'system', text: `${BLOCKED}\npolicy refused` }
    ])
    expect(appended).toEqual([])
  })

  it('restores the instance label a direct turn was showing, not the main one', async () => {
    const fake = await started()
    const target = { agent: 'Raven-Code', handle: 'refactor-auth' }
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-2', target } })

    fake.__pushEvent({ type: 'notice', payload: { ...retry, target } })
    expect(getUiState().status).toBe(TRYING_AGAIN)

    fake.__pushEvent({ type: 'token.delta', payload: { target, text: 'the answer' } })

    expect(getUiState().status).toBe('Raven-Code/refactor-auth…')
  })
})
