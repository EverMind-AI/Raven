// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Whether anything ever *calls* the live read. The reader and the folding are
// covered elsewhere; this is the half that was not, and it is the half that was
// broken -- three rounds of fixing the reader while nothing polled it.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DirectTargetRef } from '../app/directChatStore.js'

import { directKey, getDirectTranscript, resetDirectChat } from '../app/directChatStore.js'
import { useDirectStepPoll } from '../app/useDirectStepPoll.js'

const target: DirectTargetRef = { agent: 'Coder', handle: 'h1' }
const key = directKey(target.agent, target.handle)

const liveTurn = (over: Record<string, unknown>) => ({ call_id: 'l', content: '', at_ms: 0, live: true, ...over })

const answering = (turns: unknown[]) => {
  const calls: { method: string; params: Record<string, unknown> }[] = []
  const rpc = async (method: string, params?: Record<string, unknown>) => {
    calls.push({ method, params: params ?? {} })
    return { turns } as never
  }

  return { calls, rpc: rpc as never }
}

// One component type across rerenders: a fresh one would remount, and the
// transition this hook reports is remembered in a ref that a remount resets.
const Probe = ({ rpc, target: ref, working }: { rpc: never; target: DirectTargetRef | null; working: boolean }) => {
  useDirectStepPoll(rpc, () => 's1', ref, working)

  return null
}

const mount = (rpc: never, working: boolean, ref: DirectTargetRef | null = target) =>
  renderSync(<Probe rpc={rpc} target={ref} working={working} />, {
    stdout: new PassThrough() as never
  })

beforeEach(() => {
  resetDirectChat()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useDirectStepPoll', () => {
  it('reads at once, so a view opened mid-turn is not blank until the first tick', async () => {
    const { calls, rpc } = answering([liveTurn({ role: 'user', content: 'the task' })])
    const app = mount(rpc, true)
    await vi.advanceTimersByTimeAsync(0)

    expect(calls.map(c => c.method)).toEqual(['subagents.instance.history'])
    expect(calls[0]!.params).toMatchObject({ agent: 'Coder', handle: 'h1', session_key: 's1' })
    expect(getDirectTranscript(key).map(m => m.text)).toEqual(['the task'])
    app.unmount()
  })

  it('keeps reading while the turn runs', async () => {
    const { calls, rpc } = answering([liveTurn({ role: 'user', content: 'the task' })])
    const app = mount(rpc, true)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1300)

    expect(calls.length).toBeGreaterThanOrEqual(4)
    app.unmount()
  })

  it('renders the steps and the answer text as they arrive', async () => {
    // The whole point: a spawned turn reaches this view through nothing else.
    const { rpc } = answering([
      liveTurn({ role: 'user', content: 'the task' }),
      liveTurn({
        role: 'assistant',
        tool_calls: [{ id: 'c1', name: 'exec', arguments: '{"command":"pwd"}' }]
      }),
      liveTurn({ role: 'tool', tool_call_id: 'c1', content: '/repo' }),
      liveTurn({ role: 'assistant', content: 'It is /repo.' })
    ])
    const app = mount(rpc, true)
    await vi.advanceTimersByTimeAsync(0)

    const rows = getDirectTranscript(key)
    expect(rows.map(m => [m.role, m.kind ?? '', m.text])).toEqual([
      ['user', '', 'the task'],
      ['assistant', 'episodes', 'It is /repo.']
    ])
    expect(rows[1]!.episodes![0]!.tools[0]!.summary).toBe('pwd')
    app.unmount()
  })

  it('does not read while the instance is idle', async () => {
    const { calls, rpc } = answering([liveTurn({ role: 'user', content: 'the task' })])
    const app = mount(rpc, false)
    await vi.advanceTimersByTimeAsync(2000)

    expect(calls).toEqual([])
    app.unmount()
  })

  it('does not read with no instance on screen', async () => {
    const { calls, rpc } = answering([])
    const app = mount(rpc, true, null)
    await vi.advanceTimersByTimeAsync(2000)

    expect(calls).toEqual([])
    app.unmount()
  })

  it('reads the record once the work stops', async () => {
    // For a spawn there is no `message.complete` addressed to this instance, so
    // this is the only thing that replaces the last live snapshot.
    const { calls, rpc } = answering([{ call_id: 's', role: 'assistant', content: 'settled', at_ms: 1 }])
    const app = mount(rpc, true)
    await vi.advanceTimersByTimeAsync(0)
    const during = calls.length

    app.rerender(<Probe rpc={rpc} target={target} working={false} />)
    await vi.advanceTimersByTimeAsync(0)

    expect(calls.length).toBe(during + 1)
    expect(getDirectTranscript(key).map(m => m.text)).toEqual(['settled'])
    app.unmount()
  })

  it('stops reading once it is unmounted', async () => {
    const { calls, rpc } = answering([liveTurn({ role: 'user', content: 'the task' })])
    const app = mount(rpc, true)
    await vi.advanceTimersByTimeAsync(0)
    const before = calls.length
    app.unmount()
    await vi.advanceTimersByTimeAsync(2000)

    expect(calls.length).toBe(before)
  })
})
