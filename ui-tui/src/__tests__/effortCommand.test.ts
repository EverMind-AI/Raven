// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/effort`: showing and changing how much the model thinks for this
// conversation. The pick is the session's own and lives on the server, so the
// command only ever paints from the reply.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { enterDirect, resetDirectChat } from '../app/directChatStore.js'
import { RESET_WORDS } from '../app/slash/commands/core.js'
import { sessionCommands } from '../app/slash/commands/session.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { rpcErrorMessage } from '../lib/rpc.js'

const cmd = sessionCommands.find(c => c.name === 'effort')!

const WORDS = ['minimal', 'low', 'medium', 'high']

const run = (
  arg: string,
  rpc = vi.fn(() => Promise.resolve({ session_key: 's1', effort: null, default: 'low', options: WORDS })),
  sid: null | string = 's1'
) => {
  const lines: string[] = []
  cmd.run(
    arg,
    {
      gateway: { rpc },
      guarded:
        <T>(fn: (r: T) => void) =>
        (r: null | T) => {
          if (r !== null) {
            fn(r)
          }
        },
      guardedErr: (e: unknown) => {
        lines.push(`error: ${rpcErrorMessage(e)}`)
      },
      sid,
      stale: () => false,
      transcript: { sys: (text: string) => lines.push(text) }
    } as never,
    'effort'
  )

  return { lines, rpc }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  resetUiState()
  resetDirectChat()
  patchUiState({ info: { model: 'vendor/model', skills: {}, tools: {}, reasoning_effort: 'low' } })
})

describe('/effort', () => {
  it('refuses without a session and asks the gateway nothing', () => {
    const { lines, rpc } = run('high', undefined, null)

    expect(lines).toEqual(['no active session'])
    expect(rpc).not.toHaveBeenCalled()
  })

  it('reports the default when nothing is picked, naming what the default is', async () => {
    const { lines, rpc } = run('')
    await flush()

    expect(rpc).toHaveBeenCalledWith('session.set_effort', { session_key: 's1' }, { quiet: true })
    expect(lines).toEqual(['reasoning effort: default (low)'])
  })

  it('names the provider when no effort is configured anywhere', async () => {
    const rpc = vi.fn(() => Promise.resolve({ session_key: 's1', effort: null, default: null, options: WORDS }))
    const { lines } = run('', rpc)
    await flush()

    expect(lines).toEqual(["reasoning effort: default (the provider's own)"])
    expect(getUiState().info?.reasoning_effort).toBe('')
  })

  it('sets a word from the reply and moves the status bar with it', async () => {
    const rpc = vi.fn(() => Promise.resolve({ session_key: 's1', effort: 'high', default: 'low', options: WORDS }))
    const { lines } = run('High', rpc)
    await flush()

    expect(rpc).toHaveBeenCalledWith('session.set_effort', { session_key: 's1', effort: 'high' }, { quiet: true })
    expect(lines).toEqual(['reasoning effort: high (this conversation; default is low)'])
    expect(getUiState().info?.reasoning_effort).toBe('high')
  })

  // The words are `/mode`'s: one set, so the two commands cannot drift apart.
  it.each([...RESET_WORDS])('clears the pick on `%s` and shows the default again', async word => {
    const rpc = vi.fn(() => Promise.resolve({ session_key: 's1', effort: null, default: 'medium', options: WORDS }))
    patchUiState(state => ({ ...state, info: { ...state.info!, reasoning_effort: 'high' } }))
    const { lines } = run(word, rpc)
    await flush()

    expect(rpc).toHaveBeenCalledWith('session.set_effort', { session_key: 's1', clear: true }, { quiet: true })
    expect(lines).toEqual(['reasoning effort: default (medium)'])
    expect(getUiState().info?.reasoning_effort).toBe('medium')
  })

  it.each(['', 'high', 'default'])(
    'refuses `/effort %s` inside a direct chat, naming the instance, and asks the gateway nothing',
    arg => {
      // The pick is the main conversation's; sent from an instance's pane it
      // moved the main record while the reply there said "this conversation".
      enterDirect('Raven-Code', 'refactor-auth')
      const { lines, rpc } = run(arg)

      expect(lines).toEqual([
        "/effort is the main conversation's and does not reach Raven-Code/refactor-auth -- Esc returns to Raven"
      ])
      expect(rpc).not.toHaveBeenCalled()
      expect(getUiState().info?.reasoning_effort).toBe('low')
    }
  )

  it('lands the gateway refusal on the transcript and leaves the status bar alone', async () => {
    const rpc = vi.fn(() => Promise.reject(new Error("no reasoning effort 'turbo'; the words are minimal, low, medium, high")))
    const { lines } = run('turbo', rpc)
    await flush()

    expect(lines).toEqual(["error: no reasoning effort 'turbo'; the words are minimal, low, medium, high"])
    expect(getUiState().info?.reasoning_effort).toBe('low')
  })
})
