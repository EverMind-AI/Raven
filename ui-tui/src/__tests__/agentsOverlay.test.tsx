// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { AlternateScreen, Box, renderSync, Text } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DagNodeUpdatedEvent, InstanceRow } from '../rpc/generated.js'
import type { SubagentProgress } from '../types.js'

import { $overlaySectionsOpen, toggleOverlaySection } from '../app/delegationStore.js'
import { directKey, getDirectChat, resetDirectChat, setDirectTranscript } from '../app/directChatStore.js'
import { applyDagEvent, resetLiveAgents } from '../app/liveAgentsStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AgentsOverlay, conversationOf, InstanceConversation, OverlaySection } from '../components/agentsOverlay.js'
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
    expect(
      getDirectChat()
        .transcripts.get(directKey('Coder', 'h1'))
        ?.map(m => m.text)
    ).toEqual(['the task'])
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
    const app = render(<InstanceConversation cols={80} live rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />)
    await vi.advanceTimersByTimeAsync(1000)
    const whileLive = calls.length
    expect(whileLive).toBeGreaterThanOrEqual(3)
    expect(stripAnsi(app.lastFrame() ?? '')).toContain('· live')

    app.rerender(<InstanceConversation cols={80} live={false} rpc={rpc} scope="t1" t={DEFAULT_THEME} target={target} />)
    await vi.advanceTimersByTimeAsync(1000)

    expect(calls.length).toBe(whileLive + 1)
    expect(stripAnsi(app.lastFrame() ?? '')).not.toContain('· live')
    app.unmount()
  })
})

describe('AgentsOverlay on a run with a branch', () => {
  const delay = (ms: number) => new Promise(r => setTimeout(r, ms))

  const settle = (
    node: string,
    status: DagNodeUpdatedEvent['payload']['status'],
    extra: Partial<DagNodeUpdatedEvent['payload']> = {}
  ) => applyDagEvent({ payload: { node, run_id: 'run-1', status, ...extra }, type: 'dag.node_updated' })

  const open = (ids: string[]) =>
    applyDagEvent({
      payload: { nodes: ids.map(id => ({ depends_on: [], id, subagent: 'echo' })), run_id: 'run-1' },
      type: 'dag.run_started'
    })

  // The painter moves the cursor over the blanks it need not repaint, so a
  // name is whole in what it writes and the spaces between names may not be.
  const CURSOR_MOVES = new RegExp(`${String.fromCharCode(27)}\\[[0-9;?]*[A-Za-z]`, 'g')
  const plain = (text: string) => stripAnsi(text).replace(CURSOR_MOVES, '')

  // The real overlay with fake stdin. Everything it ever paints is kept, so
  // that a row that appeared in any view, however briefly, is in `screen()`.
  const mount = () => {
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
            <AgentsOverlay gw={{ request: async () => ({}) } as never} onClose={() => {}} t={DEFAULT_THEME} />
          </Box>
        </Box>
      </AlternateScreen>,
      { patchConsole: false, stderr: stderr as never, stdin: stdin as never, stdout: stdout as never }
    )

    return {
      screen: () => plain(out),
      // Everything painted after `mark`, which is a point `mark()` returned.
      since: (mark: number) => plain(out.slice(mark)),
      mark: () => out.length,
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

  // Unmounted after the test, so a failed assertion does not leave a renderer
  // running into the next one.
  const mounted: ReturnType<typeof mount>[] = []

  const show = () => {
    const app = mount()

    mounted.push(app)

    return app
  }

  beforeEach(() => {
    resetUiState()
    resetDirectChat()
    resetLiveAgents()
    patchUiState({ sid: 's1' })
  })

  afterEach(() => {
    mounted.splice(0).forEach(app => app.unmount())
  })

  it('lists the agents that ran and has no row for the node a branch left out, in any filter', async () => {
    open(['route_issue', 'answer_docs', 'fix_bug', 'flaky_step'])
    settle('route_issue', 'completed', { selected_routes: ['docs'] })
    settle('answer_docs', 'completed')
    settle('fix_bug', 'not_executed', { reason: 'when_false' })
    settle('flaky_step', 'failed')

    const app = show()

    await vi.waitFor(() => expect(app.screen()).toContain('flaky_step'), { timeout: 10_000 })
    expect(app.screen()).toContain('route_issue')
    expect(app.screen()).toContain('answer_docs')
    expect(app.screen()).toMatch(/3\s*agents/)

    // `f` goes all, running, failed: the failed filter holds what failed and not
    // the node nobody asked, which is what `interrupted` would have put there.
    const mark = app.mark()
    await app.type('f')
    await app.type('f')
    await vi.waitFor(() => expect(app.since(mark)).toContain('flaky_step'), { timeout: 10_000 })

    expect(app.screen()).not.toContain('fix_bug')
  }, 20_000)

  it('lists every node of a run no branch touched, and files the broken ones under failed as it always did', async () => {
    open(['n_completed', 'n_failed', 'n_skipped', 'n_cancelled', 'n_running', 'n_pending'])
    settle('n_completed', 'completed')
    settle('n_failed', 'failed')
    settle('n_skipped', 'skipped')
    settle('n_cancelled', 'cancelled')
    settle('n_running', 'running')

    const app = show()

    await vi.waitFor(() => expect(app.screen()).toContain('n_pending'), { timeout: 10_000 })

    for (const id of ['n_completed', 'n_failed', 'n_skipped', 'n_cancelled', 'n_running', 'n_pending']) {
      expect(app.screen(), id).toContain(id)
    }

    expect(app.screen()).toMatch(/6\s*agents/)

    const mark = app.mark()
    await app.type('f')
    await app.type('f')
    await vi.waitFor(() => expect(app.since(mark)).toContain('n_skipped'), { timeout: 10_000 })

    expect(app.since(mark)).toContain('n_failed')
    expect(app.since(mark)).toContain('n_cancelled')
  }, 20_000)
})
