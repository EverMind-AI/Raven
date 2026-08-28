// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// What Enter in the detail pane's composer does: send when the instance is
// idle, steer the running turn when it is not. Driven through the real overlay
// with fake stdin, because the decision reads three sources (the store, the
// registry row, the run's own status) and a unit of any one would miss the
// others.

import { AlternateScreen, Box, renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { directKey, getDirectTranscript, patchDirectChat, resetDirectChat } from '../app/directChatStore.js'
import { bindDirectSender, resetDirectSender } from '../app/directSend.js'
import { applySubagentStatus, resetLiveAgents } from '../app/liveAgentsStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AgentsOverlay } from '../components/agentsOverlay.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

const seed = (status: 'completed' | 'running') => {
  resetUiState()
  resetDirectChat()
  resetLiveAgents()
  resetDirectSender()
  patchUiState({ sid: 's1' })
  applySubagentStatus({ agent: 'Coder', call_id: 'rec-1', instance: 'h1', label: 'build', status, task_id: 't1' })
  patchDirectChat({
    instances: [{ agent: 'Coder', handle: 'h1', kind: 'cli', resumable: true, sessionKey: 's1', status }]
  })
}

const mount = (steerStatus: 'injected' | 'no_turn' | 'unsupported' = 'injected') => {
  const calls: { method: string; params: Record<string, unknown> }[] = []
  const gw = {
    request: async (method: string, params: Record<string, unknown> = {}) => {
      calls.push({ method, params })
      if (method === 'subagents.instance.steer') {
        return { status: steerStatus }
      }
      return method === 'subagents.instance.history' ? { turns: [] } : {}
    }
  }
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let out = ''
  Object.assign(stdout, { columns: 120, isTTY: true, rows: 40 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', c => {
    out += c.toString()
  })

  const inst = renderSync(
    <AlternateScreen>
      <Box flexDirection="column" flexGrow={1}>
        <Box flexDirection="row" flexGrow={1}>
          <AgentsOverlay focusId="t1" gw={gw as never} onClose={() => {}} t={DEFAULT_THEME} />
        </Box>
      </Box>
    </AlternateScreen>,
    { patchConsole: false, stderr: stderr as never, stdin: stdin as never, stdout: stdout as never }
  )

  return {
    calls,
    screen: () => out,
    type: async (s: string) => {
      stdin.write(s)
      await delay(80)
    },
    unmount: () => {
      inst.unmount()
      inst.cleanup()
    }
  }
}

describe('the detail pane composer', () => {
  beforeEach(() => {
    resetDirectSender()
  })

  afterEach(() => {
    resetDirectSender()
  })

  it('sends to the instance when it is idle', async () => {
    seed('completed')
    const sent: [unknown, string][] = []
    bindDirectSender(async (target, text) => {
      sent.push([target, text])
    })
    const app = mount()
    await delay(150)

    await app.type('read the docs')
    await app.type('\r')

    expect(sent).toEqual([[{ agent: 'Coder', handle: 'h1' }, 'read the docs']])
    expect(getDirectTranscript(directKey('Coder', 'h1')).map(m => [m.role, m.text])).toEqual([
      ['user', 'read the docs']
    ])
    expect(app.calls.map(c => c.method)).not.toContain('subagents.instance.steer')
    app.unmount()
  })

  it('steers the running turn instead, and echoes nothing itself', async () => {
    seed('running')
    const sent: unknown[] = []
    bindDirectSender(async (...args) => {
      sent.push(args)
    })
    const app = mount('injected')
    await delay(150)

    await app.type('the docs first')
    await app.type('\r')
    await delay(100)

    const steer = app.calls.find(c => c.method === 'subagents.instance.steer')
    expect(steer?.params).toEqual({ agent: 'Coder', handle: 'h1', session_key: 's1', text: 'the docs first' })
    expect(sent).toEqual([])
    // The run announces the merged words on its own transcript; a local echo
    // would draw them twice once the next live read lands.
    expect(getDirectTranscript(directKey('Coder', 'h1'))).toEqual([])
    expect(app.screen()).toContain('steer landed')
    app.unmount()
  })

  it('falls back to a send when the run ended before the steer arrived', async () => {
    seed('running')
    const sent: [unknown, string][] = []
    bindDirectSender(async (target, text) => {
      sent.push([target, text])
    })
    const app = mount('no_turn')
    await delay(150)

    await app.type('carry on')
    await app.type('\r')
    await delay(100)

    expect(sent).toEqual([[{ agent: 'Coder', handle: 'h1' }, 'carry on']])
    app.unmount()
  })

  it('hands the text back when the run cannot be steered', async () => {
    seed('running')
    const app = mount('unsupported')
    await delay(150)

    await app.type('carry on')
    await app.type('\r')
    await delay(100)

    expect(app.screen()).toContain('cannot take a message mid-turn')
    expect(getDirectTranscript(directKey('Coder', 'h1'))).toEqual([])
    app.unmount()
  })
})
