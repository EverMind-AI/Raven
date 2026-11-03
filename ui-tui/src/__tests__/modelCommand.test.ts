// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/model`: the status bar's effort is a property of the model, so a switch
// reads it back instead of leaving the previous model's word in force.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { rpcErrorMessage } from '../lib/rpc.js'

const cmd = sessionCommands.find(c => c.name === 'model')!

const run = (arg: string, rpc: ReturnType<typeof vi.fn>, sid: null | string = 's1') => {
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
      local: { maybeWarn: () => {} },
      sid,
      stale: () => false,
      transcript: { sys: (text: string) => lines.push(text) }
    } as never,
    'model'
  )
  return { lines, rpc }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  resetUiState()
  patchUiState({ info: { model: 'vendor/model', skills: {}, tools: {}, reasoning_effort: 'high' } })
})

describe('/model', () => {
  it('reads the effort in force after a switch lands and moves the status bar', async () => {
    const rpc = vi.fn((method: string) => {
      if (method === 'config.set') {
        return Promise.resolve({ applied: true, value: 'm2', applies_to_session: true, previous: 'vendor/model' })
      }
      if (method === 'session.set_effort') {
        return Promise.resolve({ session_key: 's1', effort: null, default: 'medium', options: ['low', 'medium'] })
      }
      return Promise.resolve({})
    })
    const { lines } = run('m2 --provider minimax', rpc)
    await flush()
    await flush()

    expect(rpc).toHaveBeenCalledWith('session.set_effort', { session_key: 's1' }, { quiet: true })
    expect(getUiState().info?.model).toBe('m2')
    expect(getUiState().info?.reasoning_effort).toBe('medium')
    expect(lines).toContain('model → m2')
  })

  it('does not read the effort when the switch leaves this conversation alone', async () => {
    const rpc = vi.fn((method: string) => {
      if (method === 'config.set') {
        return Promise.resolve({ applied: true, value: 'm2', applies_to_session: false })
      }
      return Promise.resolve({})
    })
    run('m2 --provider minimax --default', rpc)
    await flush()
    await flush()

    expect(rpc).not.toHaveBeenCalledWith('session.set_effort', expect.anything(), expect.anything())
    expect(getUiState().info?.reasoning_effort).toBe('high')
  })
})
