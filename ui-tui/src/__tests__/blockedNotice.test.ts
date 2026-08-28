// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Where a runtime notice lands. It reports on the turn it stopped, so it must
// be drawn under that turn's steps -- and the turn's steps reach the transcript
// only when the turn commits, so a notice appended when it arrived sat above
// every step it was reporting on. Two paths have to agree about the position:
// the live stream here, and the resume path in `toTranscriptMessages`.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { TurnEvent, TurnSendResult } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { resetDirectChat } from '../app/directChatStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { setLocale } from '../i18n/index.js'

const BLOCKED_EN =
  'A safety rule stopped this operation, so the turn ended here. Say the word and I will carry on with the parts that do not need it.'
const DENIED = 'Error: User denied this command or the approval request expired'

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

/** A turn that narrates, calls a tool the runtime refuses, and is stopped. */
const runBlockedTurn = (fake: FakeRpc, { complete = true }: { complete?: boolean } = {}) => {
  fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
  fake.__pushEvent({ type: 'episode.start', payload: { index: 0 } })
  fake.__pushEvent({ type: 'token.delta', payload: { text: 'Removing the file now.' } })
  fake.__pushEvent({
    type: 'tool.start',
    payload: { tool_call_id: 'call-1', name: 'terminal', arguments: { command: 'rm -rf build' } }
  })
  fake.__pushEvent({
    type: 'tool.complete',
    payload: { tool_call_id: 'call-1', result_preview: DENIED, truncated: false, ok: false }
  })
  fake.__pushEvent({ type: 'notice', payload: { kind: 'action_blocked', detail: DENIED } })

  if (complete) {
    fake.__pushEvent({
      type: 'message.complete',
      payload: { turn_id: 'turn-1', usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } }
    })
  }
}

describe("a blocked-action notice is the turn's last row, not a row above it", () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    resetDirectChat()
    turnController.fullReset()
  })

  afterEach(() => setLocale('en'))

  it('commits after the episodes message in episodes mode', async () => {
    patchUiState({ transcript: 'episodes' })
    const fake = makeFakeRpc()
    const appended: Msg[] = []
    const stream = createChatStream({
      appendMessage: msg => appended.push(msg),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })

    await stream.attach()
    await stream.send('delete the build dir')

    runBlockedTurn(fake, { complete: false })

    // Nothing is in the transcript yet: the turn is still live, so a notice
    // appended here is a notice above the whole turn.
    expect(appended).toEqual([])
    expect(getTurnState().notice).toBe(`${BLOCKED_EN}\n${DENIED}`)

    fake.__pushEvent({
      type: 'message.complete',
      payload: { turn_id: 'turn-1', usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } }
    })

    expect(appended.map(msg => [msg.role, msg.kind ?? ''])).toEqual([
      ['assistant', 'episodes'],
      ['system', '']
    ])
    expect(appended.at(-1)!.text).toBe(`${BLOCKED_EN}\n${DENIED}`)
    // The refused call is in the turn above it, which is what the notice reports on.
    expect(appended[0]!.episodes?.[0]?.tools[0]).toMatchObject({ id: 'call-1', ok: false })
    // And the live copy is dropped as the turn commits, so the line is never
    // on screen twice.
    expect(getTurnState().notice).toBe('')
  })

  it('commits last in legacy mode too', async () => {
    patchUiState({ transcript: 'legacy' })
    const fake = makeFakeRpc()
    const appended: Msg[] = []
    const stream = createChatStream({
      appendMessage: msg => appended.push(msg),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })

    await stream.attach()
    await stream.send('delete the build dir')
    runBlockedTurn(fake)

    expect(appended.at(-1)).toMatchObject({ role: 'system', text: `${BLOCKED_EN}\n${DENIED}` })
    expect(appended.length).toBeGreaterThan(1)
  })

  it('survives a turn that is cancelled instead of completed', async () => {
    patchUiState({ transcript: 'episodes' })
    const fake = makeFakeRpc()
    const appended: Msg[] = []
    const stream = createChatStream({
      appendMessage: msg => appended.push(msg),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })

    await stream.attach()
    await stream.send('delete the build dir')
    runBlockedTurn(fake, { complete: false })

    fake.__pushEvent({
      type: 'error',
      payload: { code: 499, message: 'cancelled', reason: 'cancelled_by_client' }
    })

    expect(appended.at(-1)).toMatchObject({ role: 'system', text: `${BLOCKED_EN}\n${DENIED}` })
  })

  it('follows the reader locale, detail verbatim', async () => {
    setLocale('zh')
    patchUiState({ transcript: 'episodes' })
    const fake = makeFakeRpc()
    const appended: Msg[] = []
    const stream = createChatStream({
      appendMessage: msg => appended.push(msg),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })

    await stream.attach()
    await stream.send('delete the build dir')
    runBlockedTurn(fake)

    const line = appended.at(-1)!.text.split('\n')

    expect(line[0]).toContain('安全规则')
    expect(line[1]).toBe(DENIED)
  })
})

describe('a resumed transcript draws the notice in the same place', () => {
  afterEach(() => setLocale('en'))

  it('replaces the runtime prose stored as the entry text', () => {
    const msgs = toTranscriptMessages([
      { role: 'user', text: 'delete the build dir' },
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ id: 'call-1', name: 'terminal', arguments: '{"command":"rm -rf build"}' }]
      },
      { role: 'tool', tool_call_id: 'call-1', name: 'terminal', text: DENIED },
      {
        role: 'assistant',
        // What the runtime wrote for the model to read, in English whatever
        // language the turn was in. A reader must never see it as the answer.
        text: 'The operation was not completed, and no alternative method will be attempted.',
        notice: { kind: 'action_blocked', detail: DENIED }
      }
    ])

    expect(msgs.map(msg => [msg.role, msg.kind ?? ''])).toEqual([
      ['user', ''],
      ['assistant', 'episodes'],
      ['system', '']
    ])
    expect(msgs.at(-1)!.text).toBe(`${BLOCKED_EN}\n${DENIED}`)
    expect(msgs.some(msg => msg.text.includes('no alternative method'))).toBe(false)
    expect(msgs[1]!.episodes?.[0]?.tools[0]).toMatchObject({ id: 'call-1', name: 'terminal' })
  })

  it('leaves an entry without a notice alone', () => {
    const msgs = toTranscriptMessages([
      { role: 'user', text: 'hi' },
      { role: 'assistant', text: 'hello' }
    ])

    expect(msgs.map(msg => msg.role)).toEqual(['user', 'assistant'])
    expect(msgs.at(-1)!.text).toBe('hello')
  })
})
