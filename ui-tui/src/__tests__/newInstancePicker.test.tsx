// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { InstanceRow, SubagentRow } from '../rpc/generated.js'

import { patchDirectChat, resetDirectChat } from '../app/directChatStore.js'
import { NewInstancePicker } from '../components/newInstancePicker.js'
import { DEFAULT_THEME } from '../theme.js'

const ESC = String.fromCharCode(27)
const ENTER = '\r'
const DOWN = `${ESC}[B`

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const normalize = (raw: string) =>
  raw
    .replace(new RegExp(`${ESC}\\[[0-9;?<>=]*[a-zA-Z]`, 'g'), ' ')
    .replace(new RegExp(`${ESC}\\][^\\u0007]*\\u0007?`, 'g'), ' ')
    .replace(new RegExp(ESC, 'g'), ' ')
    .replace(/\s+/g, ' ')

// No `as SubagentRow`: the cast would switch off exactly the field check this
// fixture exists for, and the defaults below already satisfy the type.
const agent = (over: Partial<SubagentRow> & { name: string }): SubagentRow =>
  ({
    configured: true,
    description: `${over.name} does things`,
    enabled: true,
    group: 'installed',
    has_api_key: false,
    kind: 'cli',
    preset: null,
    probe_detail: '',
    probe_status: 'ready',
    stateful: true,
    test_running: false,
    ...over
  }) satisfies SubagentRow

const instance = (over: Partial<InstanceRow> & { agent: string; handle: string }): InstanceRow =>
  ({
    createdAtMs: 1,
    kind: 'cli',
    sessionKey: 's1',
    status: 'idle',
    updatedAtMs: 1,
    ...over
  }) satisfies InstanceRow

const mount = (rows: SubagentRow[], onCreate?: (params: Record<string, unknown>) => unknown, sessionKey = 's1') => {
  const onCancel = vi.fn()
  const onCreated = vi.fn()
  const request = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'subagents.list') {
      return Promise.resolve({ rows })
    }
    if (method === 'subagents.instance.create') {
      const out = onCreate ? onCreate(params) : { instance: instance({ agent: String(params.agent), handle: 'h-1' }) }
      return out instanceof Error ? Promise.reject(out) : Promise.resolve(out)
    }
    return Promise.resolve({})
  })

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  // Not a TTY: ink diffs frames against the previous one on a real terminal
  // and emits cursor-forward moves in place of characters it need not repaint,
  // so the accumulated buffer would hold only what changed. Off it writes each
  // frame whole, which is what these assertions read.
  Object.assign(stdout, { columns: 100, isTTY: false, rows: 30 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  renderSync(
    <NewInstancePicker
      gw={{ request } as never}
      onCancel={onCancel}
      onCreated={onCreated}
      sessionKey={sessionKey}
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  return {
    frame: () => normalize(output),
    onCancel,
    onCreated,
    request,
    type: async (s: string) => {
      stdin.write(s)
      await delay(30)
    }
  }
}

const waitFor = async (frame: () => string, text: string) => {
  for (let i = 0; i < 25; i++) {
    if (frame().includes(text)) {
      return
    }
    await delay(25)
  }
  expect(frame()).toContain(text)
}

beforeEach(() => {
  resetDirectChat()
})

describe('NewInstancePicker', () => {
  it('offers only agents a direct chat can actually address', async () => {
    // `chat` refuses a stateless agent outright, and a disabled one is not on
    // the roster at all -- offering either is offering a refusal.
    const h = mount([
      agent({ name: 'Raven-Code' }),
      agent({ name: 'Researcher', stateful: false }),
      agent({ enabled: false, name: 'Retired' })
    ])
    await waitFor(h.frame, 'Raven-Code')

    expect(h.frame()).not.toContain('Researcher')
    expect(h.frame()).not.toContain('Retired')
  })

  it('treats a server that does not report statefulness as offering nothing', async () => {
    const h = mount([agent({ name: 'Raven-Code', stateful: undefined })])
    await waitFor(h.frame, 'no sub-agent')

    expect(h.frame()).not.toContain('Raven-Code')
  })

  it('opens without paying for a probe', async () => {
    const h = mount([agent({ name: 'Raven-Code' })])
    await waitFor(h.frame, 'Raven-Code')

    expect(h.request).toHaveBeenCalledWith('subagents.list', { probe: false })
  })

  it('says how many instances of that agent are already open', async () => {
    patchDirectChat({
      instances: [
        instance({ agent: 'Raven-Code', handle: 'a' }),
        instance({ agent: 'Raven-Code', handle: 'b' }),
        instance({ agent: 'Raven-PPT', handle: 'c' })
      ]
    })
    const h = mount([agent({ name: 'Raven-Code' }), agent({ name: 'Raven-PPT' })])
    await waitFor(h.frame, 'Raven-Code')

    expect(h.frame()).toContain('2 open')
    expect(h.frame()).toContain('1 open')
  })

  it('creates the selected agent and hands back the row the server persisted', async () => {
    const h = mount([agent({ name: 'Raven-Code' }), agent({ name: 'Raven-PPT' })])
    await waitFor(h.frame, 'Raven-PPT')

    await h.type(DOWN)
    await h.type(ENTER)
    await delay(40)

    expect(h.request).toHaveBeenCalledWith('subagents.instance.create', { agent: 'Raven-PPT', session_key: 's1' })
    expect(h.onCreated).toHaveBeenCalledWith(expect.objectContaining({ agent: 'Raven-PPT', handle: 'h-1' }))
  })

  it('quick-selects by the number it printed', async () => {
    const h = mount([agent({ name: 'Raven-Code' }), agent({ name: 'Raven-PPT' })])
    await waitFor(h.frame, 'Raven-PPT')

    await h.type('2')
    await delay(40)

    expect(h.request).toHaveBeenCalledWith('subagents.instance.create', { agent: 'Raven-PPT', session_key: 's1' })
  })

  it('shows a refusal inline and stays open', async () => {
    const h = mount([agent({ name: 'Raven-Code' })], () => new Error("'Raven-Code' is stateless"))
    await waitFor(h.frame, 'Raven-Code')

    await h.type(ENTER)
    await waitFor(h.frame, 'is stateless')

    expect(h.onCreated).not.toHaveBeenCalled()
    expect(h.onCancel).not.toHaveBeenCalled()
  })

  it('does not create without a session', async () => {
    const h = mount([agent({ name: 'Raven-Code' })], undefined, '')
    await waitFor(h.frame, 'Raven-Code')

    await h.type(ENTER)
    await delay(40)

    expect(h.request).not.toHaveBeenCalledWith('subagents.instance.create', expect.anything())
    expect(h.onCreated).not.toHaveBeenCalled()
  })

  it('cancels on Esc', async () => {
    const h = mount([agent({ name: 'Raven-Code' })])
    await waitFor(h.frame, 'Raven-Code')

    await h.type(ESC)
    await delay(30)

    expect(h.onCancel).toHaveBeenCalled()
  })

  it('says so when nothing can be instantiated', async () => {
    const h = mount([])
    await waitFor(h.frame, 'no sub-agent')

    expect(h.frame()).toContain('Esc/q cancel')
  })
})
