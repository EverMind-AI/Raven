// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/perm`: reading and switching the permission mode. The read pins the RPC
// shape -- `config.get` takes `keys` (plural) and answers `{config: {...}}` --
// because the singular `key`/`value` shape silently returns nothing, and the
// command then reports the fallback "ask" whatever the engine is really in.

import { describe, expect, it, vi } from 'vitest'

import { coreCommands } from '../app/slash/commands/core.js'
import { rpcErrorMessage } from '../lib/rpc.js'

const cmd = coreCommands.find(c => c.name === 'perm')!

const run = (arg: string, rpc: ReturnType<typeof vi.fn>) => {
  const main: string[] = []
  const errors: unknown[] = []
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
        errors.push(e)
        main.push(`error: ${rpcErrorMessage(e)}`)
      },
      sid: 's1',
      stale: () => false,
      transcript: { sys: (text: string) => main.push(text) }
    } as never,
    'perm'
  )

  return { errors, main, rpc }
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0))

describe('/perm', () => {
  it('reads the mode through the plural keys shape and reports the value', async () => {
    const rpc = vi.fn(() => Promise.resolve({ config: { 'permissions.mode': 'smart' } }))
    const h = run('', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith('config.get', { keys: ['permissions.mode'], session_id: 's1' })
    expect(h.main[0]).toContain('permission mode: smart')
  })

  it('reads the default without naming the session', async () => {
    const rpc = vi.fn(() => Promise.resolve({ config: { 'permissions.mode': 'ask' } }))
    const h = run('default', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith('config.get', { keys: ['permissions.mode'] })
    expect(h.main[0]).toContain('default permission mode: ask')
  })

  it('falls back to ask only when the engine answers nothing', async () => {
    const rpc = vi.fn(() => Promise.resolve({ config: {} }))
    const h = run('', rpc)
    await settle()

    expect(h.main[0]).toContain('permission mode: ask')
  })

  it('writes a valid tier through config.set', async () => {
    const rpc = vi.fn(() => Promise.resolve({}))
    const h = run('full', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith('config.set', {
      key: 'permissions.mode',
      value: 'full',
      scope: 'session',
      session_id: 's1'
    })
    expect(h.main[0]).toContain('permission mode set to full for this conversation')
  })

  it('writes the default when asked to, with no session scope', async () => {
    const rpc = vi.fn(() => Promise.resolve({}))
    const h = run('default smart', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith('config.set', { key: 'permissions.mode', value: 'smart' })
    expect(h.main[0]).toContain('default permission mode set to smart')
  })

  it('refuses a third word without calling the engine', () => {
    const rpc = vi.fn()
    const h = run('default smart now', rpc)

    expect(rpc).not.toHaveBeenCalled()
    expect(h.main[0]).toContain('usage: /perm')
  })

  it('refuses an unknown tier without calling the engine', () => {
    const rpc = vi.fn()
    const h = run('yolo', rpc)

    expect(rpc).not.toHaveBeenCalled()
    expect(h.main[0]).toContain('usage: /perm [ask|smart|full] | /perm default [ask|smart|full]')
  })
})
