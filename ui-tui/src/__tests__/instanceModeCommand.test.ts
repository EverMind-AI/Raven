// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/mode`: showing and changing the effort level of the sub-agent instance the
// user is chatting with. The half three roster rows could never serve -- with
// the modes as separate agents, changing effort meant abandoning the
// conversation and starting a new one against a different name.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  appendDirectMessage,
  directKey,
  enterDirect,
  getDirectChat,
  getDirectTranscript,
  leaveDirect,
  resetDirectChat
} from '../app/directChatStore.js'
import { coreCommands } from '../app/slash/commands/core.js'
import { rpcErrorMessage } from '../lib/rpc.js'

const cmd = coreCommands.find(c => c.name === 'mode')!

const MODES = [
  { id: 'fast', name: 'Fast', description: 'converges early' },
  { id: 'deep', name: 'Deep', description: 'searches longer' }
]

// The stub `sys` routes by view, because the real one does: `useMainApp` hands
// the slash context a wrapper that writes into the open instance's transcript
// (`slashSys`). A stub that always pushed to `main` would score a line the user
// cannot see as delivered, which is the defect these cases exist to catch.
const run = (
  arg: string,
  rpc = vi.fn(() => Promise.resolve({ availableModes: MODES, mode: null })),
  sid: null | string = 's1'
) => {
  const main: string[] = []
  const errors: unknown[] = []
  const say = (text: string) => {
    const active = getDirectChat().active

    return active === null
      ? main.push(text)
      : appendDirectMessage(directKey(active.agent, active.handle), { kind: 'slash', role: 'system', text })
  }
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
      // Reports through `sys`, as `createSlashHandler`'s does -- so a refusal
      // the agent sent back lands on the same surface as everything else.
      guardedErr: (e: unknown) => {
        errors.push(e)
        say(`error: ${rpcErrorMessage(e)}`)
      },
      sid,
      stale: () => false,
      transcript: { sys: say }
    } as never,
    'mode'
  )

  const direct = (agent: string, handle: string) => getDirectTranscript(directKey(agent, handle)).map(m => m.text)

  return { direct, errors, main, rpc }
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  resetDirectChat()
})

describe('/mode', () => {
  it('says where it applies when the user is on the main conversation', () => {
    leaveDirect()
    const h = run('deep')

    expect(h.main[0]).toContain('/mode applies to a sub-agent chat')
    expect(h.rpc).not.toHaveBeenCalled()
  })

  it('reports without changing anything when given no argument', async () => {
    enterDirect('Researcher', 'h1')
    const h = run('')
    await settle()

    // Neither `mode` nor `clear`: the read call. Sending `mode: undefined`
    // would clear the override just by asking what it was.
    expect(h.rpc).toHaveBeenCalledWith(
      'subagents.instance.set_mode',
      { agent: 'Researcher', handle: 'h1', session_key: 's1' },
      { quiet: true }
    )
    const out = h.direct('Researcher', 'h1').join('\n')
    expect(h.main).toEqual([])
    expect(out).toContain('no override set, so its own default is in force')
    // A read changed nothing, so it must not claim a change is coming.
    expect(out).not.toContain('takes effect on the next message')
    expect(out).not.toContain('is now on')
  })

  it('names the mode in force when reporting one, without claiming a change', async () => {
    enterDirect('Researcher', 'h1')
    const h = run(
      '',
      vi.fn(() => Promise.resolve({ availableModes: MODES, mode: 'deep' }))
    )
    await settle()

    const out = h.direct('Researcher', 'h1').join('\n')
    expect(out).toContain('Researcher/h1 is on deep')
    expect(out).toContain('* deep -- searches longer')
    expect(out).not.toContain('is now on')
    expect(out).not.toContain('takes effect on the next message')
  })

  it('shows a refused mode to the user rather than dropping it', async () => {
    // The refusal names what the agent does offer, and it has to reach the
    // surface on screen: a silent drop leaves the next turn at the old effort
    // with the user believing the new one is in force. `guardedErr` would have
    // written it to the main conversation, which a direct chat does not render.
    enterDirect('Researcher', 'h1')
    const rpc = vi.fn(() => Promise.reject(new Error("'Researcher' has no mode 'turbo'; it offers fast, deep")))
    const h = run('turbo', rpc as never)
    await settle()

    expect(h.direct('Researcher', 'h1').join('\n')).toContain('it offers fast, deep')
    expect(h.main).toEqual([])
  })

  it('switches to the named mode and marks it in the listing', async () => {
    enterDirect('Researcher', 'h1')
    const rpc = vi.fn(() => Promise.resolve({ availableModes: MODES, mode: 'deep' }))
    const h = run('deep', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith(
      'subagents.instance.set_mode',
      { agent: 'Researcher', handle: 'h1', mode: 'deep', session_key: 's1' },
      { quiet: true }
    )
    const out = h.direct('Researcher', 'h1').join('\n')
    expect(out).toContain('is now on deep')
    expect(out).toContain('* deep -- searches longer')
    expect(out).toContain('takes effect on the next message')
  })

  it('clears the override with a reset word rather than a sentinel id', async () => {
    // A sentinel mode id would take that name away from an agent free to call
    // one of its own modes "default".
    enterDirect('Researcher', 'h1')
    const rpc = vi.fn(() => Promise.resolve({ availableModes: MODES, mode: null }))
    run('default', rpc)
    await settle()

    expect(rpc).toHaveBeenCalledWith(
      'subagents.instance.set_mode',
      { agent: 'Researcher', clear: true, handle: 'h1', session_key: 's1' },
      { quiet: true }
    )
  })

  it('says so for an agent that offers none, instead of printing an empty menu', async () => {
    enterDirect('Coder', 'h1')
    const h = run(
      '',
      vi.fn(() => Promise.resolve({ availableModes: [], mode: null }))
    )
    await settle()

    expect(h.direct('Coder', 'h1').join('\n')).toContain('no modes to choose from')
  })

  it('writes into the chat on screen, not the main conversation behind it', async () => {
    // The whole command is direct-chat-only, so `transcript.sys` -- the main
    // conversation -- is never the surface being rendered while it runs. Every
    // line it produces went there and was invisible.
    enterDirect('Researcher', 'h1')
    const h = run(
      'deep',
      vi.fn(() => Promise.resolve({ availableModes: MODES, mode: 'deep' }))
    )
    await settle()

    expect(h.main).toEqual([])
    expect(h.direct('Researcher', 'h1').join('\n')).toContain('is now on deep')
  })

  it('does not call without a session', () => {
    enterDirect('Researcher', 'h1')
    const h = run('deep', vi.fn(), null)

    expect(h.direct('Researcher', 'h1')).toEqual(['no active session'])
    expect(h.rpc).not.toHaveBeenCalled()
  })
})
