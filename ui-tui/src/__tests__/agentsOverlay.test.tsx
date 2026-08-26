// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { Text } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { InstanceRow } from '../rpc/generated.js'
import type { SubagentProgress } from '../types.js'

import { $overlaySectionsOpen, toggleOverlaySection } from '../app/delegationStore.js'
import { directKey, getDirectChat, resetDirectChat, setDirectTranscript } from '../app/directChatStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { conversationOf, InstanceConversation, OverlaySection } from '../components/agentsOverlay.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const section = (scope: string, title: string, body: string) =>
  stripAnsi(
    render(
      <OverlaySection defaultOpen id="transcript" scope={scope} t={DEFAULT_THEME} title={title}>
        <Text>{body}</Text>
      </OverlaySection>
    ).lastFrame() ?? ''
  )

describe('OverlaySection', () => {
  beforeEach(() => {
    $overlaySectionsOpen.set({})
  })

  it('holds one open state per run, not one per section name', () => {
    // What a click on run-a's Transcript header does.
    toggleOverlaySection('run-a:transcript', true)

    expect(section('run-a', 'Transcript', 'ALPHA BODY')).not.toContain('ALPHA BODY')
    expect(section('run-b', 'Transcript', 'BETA BODY')).toContain('BETA BODY')
  })

  it('keeps that state when the title changes as the run finishes', () => {
    toggleOverlaySection('run-a:transcript', true)

    // The live and settled headers of the same section: the reader closed it
    // while it ran, and it must not reopen itself the moment the run lands.
    expect(section('run-a', 'Transcript · live', 'BODY')).not.toContain('BODY')
    expect(section('run-a', 'Transcript', 'BODY')).not.toContain('BODY')
  })
})

const progress = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  depth: 0,
  goal: 'find the bug',
  id: 't1',
  index: 0,
  notes: [],
  parentId: null,
  status: 'running',
  taskCount: 1,
  thinking: [],
  toolCount: 0,
  tools: [],
  ...over
})

const instanceRow = (over: Partial<InstanceRow> = {}): InstanceRow => ({
  agent: 'Coder',
  handle: 'h1',
  kind: 'cli',
  sessionKey: 's1',
  ...over
})

describe('conversationOf', () => {
  it('is null for a run bound to no instance', () => {
    expect(conversationOf(progress(), [instanceRow({ resumable: true })])).toBeNull()
  })

  it('takes resumable from the registry row, and reads an unlisted instance as not resumable', () => {
    const item = progress({ instance: { agent: 'Coder', handle: 'h1' } })

    expect(conversationOf(item, [instanceRow({ resumable: true })])).toEqual({
      resumable: true,
      target: { agent: 'Coder', handle: 'h1' }
    })
    expect(conversationOf(item, [instanceRow({ resumable: false })])?.resumable).toBe(false)
    expect(conversationOf(item, [])?.resumable).toBe(false)
  })
})

describe('InstanceConversation', () => {
  const target = { agent: 'Coder', handle: 'h1' }

  beforeEach(() => {
    resetDirectChat()
    resetUiState()
    patchUiState({ sid: 's1' })
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const answering = (turns: unknown[]) => {
    const calls: string[] = []
    const rpc = async (method: string) => {
      calls.push(method)
      return { turns } as never
    }
    return { calls, rpc: rpc as never }
  }

  it('reads the instance on entry and draws its rows through the transcript renderer', async () => {
    const { calls, rpc } = answering([{ call_id: 'c1', role: 'user', content: 'the task', at_ms: 1 }])
    const app = render(
      <InstanceConversation cols={80} live={false} rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />
    )
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toEqual(['subagents.instance.history'])
    const frame = stripAnsi(app.lastFrame() ?? '')
    expect(frame).toContain('Conversation · Coder/h1')
    expect(frame).toContain('the task')
    // The rows are the store's: what Direct Chat would show for this instance.
    expect(getDirectChat().transcripts.get(directKey('Coder', 'h1'))?.map(m => m.text)).toEqual(['the task'])
    app.unmount()
  })

  it('replaces a stale live snapshot when an idle instance is entered', async () => {
    // The run ended while this pane was closed: the store still holds the last
    // live poll, and the record now has the reply.
    setDirectTranscript(directKey('Coder', 'h1'), [{ role: 'user', text: 'the task' }])
    const { calls, rpc } = answering([
      { call_id: 'c1', role: 'user', content: 'the task', at_ms: 1 },
      { call_id: 'c2', role: 'assistant', content: 'the reply', at_ms: 2 }
    ])
    const app = render(
      <InstanceConversation cols={80} live={false} rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />
    )
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toEqual(['subagents.instance.history'])
    expect(stripAnsi(app.lastFrame() ?? '')).toContain('the reply')
    app.unmount()
  })

  it('keeps re-reading while the run works, and reads once more when it lands', async () => {
    setDirectTranscript(directKey('Coder', 'h1'), [{ role: 'user', text: 'earlier' }])
    const { calls, rpc } = answering([])
    const app = render(
      <InstanceConversation cols={80} live rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />
    )
    await vi.advanceTimersByTimeAsync(1000)
    const whileLive = calls.length
    expect(whileLive).toBeGreaterThanOrEqual(3)
    expect(stripAnsi(app.lastFrame() ?? '')).toContain('· live')

    app.rerender(
      <InstanceConversation cols={80} live={false} rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />
    )
    await vi.advanceTimersByTimeAsync(1000)

    expect(calls.length).toBe(whileLive + 1)
    expect(stripAnsi(app.lastFrame() ?? '')).not.toContain('· live')
    app.unmount()
  })
})
