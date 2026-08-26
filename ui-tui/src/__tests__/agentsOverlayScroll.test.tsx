// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The detail pane must hold its scroll position while the reader types into
// the conversation composer. Rendered through the real renderer inside an
// AlternateScreen, because the regression lived in yoga's layout cache and no
// unit of the component could show it.

import type * as Ink from '@hermes/ink'

import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const handles: { getScrollTop: () => number; getViewportHeight: () => number }[] = []

vi.mock('@hermes/ink', async importOriginal => {
  const m = await importOriginal<typeof Ink>()
  const Wrapped = React.forwardRef<unknown, Record<string, unknown>>((props, ref) => {
    const inner = React.useRef<(typeof handles)[number] | null>(null)
    React.useImperativeHandle(ref, () => {
      if (inner.current && !handles.includes(inner.current)) {
        handles.push(inner.current)
      }
      return inner.current
    }, [])
    return React.createElement(m.ScrollBox, { ...props, ref: inner })
  })
  return { ...m, ScrollBox: Wrapped }
})

const { AlternateScreen, Box, renderSync } = await import('@hermes/ink')
const { AgentsOverlay } = await import('../components/agentsOverlay.js')
const { applySubagentStatus, resetLiveAgents } = await import('../app/liveAgentsStore.js')
const { directKey, patchDirectChat, resetDirectChat, setDirectTranscript } = await import('../app/directChatStore.js')
const { patchUiState, resetUiState } = await import('../app/uiStore.js')
const { DEFAULT_THEME } = await import('../theme.js')
const { foldDirectTurns } = await import('../domain/directEpisodes.js')

const delay = (ms: number) => new Promise(r => setTimeout(r, ms))
const PAGE_DOWN = '\x1b[6~'
const BACKSPACE = '\x7f'

const seed = () => {
  resetUiState()
  resetDirectChat()
  resetLiveAgents()
  patchUiState({ sid: 's1' })
  applySubagentStatus({
    agent: 'Coder',
    call_id: 'rec-1',
    instance: 'h1',
    label: 'build the site',
    status: 'completed',
    task_id: 't1'
  })
  patchDirectChat({
    instances: [{ agent: 'Coder', handle: 'h1', kind: 'cli', resumable: true, sessionKey: 's1', status: 'completed' }]
  })

  const turns = []
  let at = 1
  for (let i = 0; i < 6; i++) {
    turns.push({ call_id: `u${i}`, role: 'user' as const, content: `question ${i}\n\nwith a second paragraph`, at_ms: at++ })
    turns.push({
      call_id: `a${i}`,
      role: 'assistant' as const,
      content: '',
      at_ms: at++,
      tool_calls: [{ id: `c${i}`, name: 'read', arguments: '{"path":"/repo"}' }]
    })
    turns.push({ call_id: `t${i}`, role: 'tool' as const, tool_call_id: `c${i}`, content: 'listing', at_ms: at++ })
    turns.push({
      call_id: `b${i}`,
      role: 'assistant' as const,
      content: `answer ${i}\n\n| file | path |\n|---|---|\n| a | /a |\n\n- one\n- two\n\ndone.`,
      at_ms: at++
    })
  }
  setDirectTranscript(directKey('Coder', 'h1'), foldDirectTurns(turns))
}

const mount = () => {
  const gw = { request: async (method: string) => (method === 'subagents.instance.history' ? { turns: [] } : {}) }
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  Object.assign(stdout, { columns: 180, isTTY: true, rows: 57 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.resume()

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
    handle: () => handles.at(-1)!,
    type: async (s: string) => {
      stdin.write(s)
      await delay(60)
    },
    unmount: () => {
      inst.unmount()
      inst.cleanup()
    }
  }
}

describe('AgentsOverlay detail pane', () => {
  beforeEach(() => {
    handles.length = 0
    seed()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps its scroll position and viewport while the composer is typed into', async () => {
    const app = mount()
    await delay(150)

    for (let i = 0; i < 12; i++) {
      await app.type(PAGE_DOWN)
    }

    const top = app.handle().getScrollTop()
    const viewport = app.handle().getViewportHeight()
    expect(top).toBeGreaterThan(0)
    expect(viewport).toBeLessThan(57)

    // A draft that ends in a space is what re-measured the sibling row and
    // exposed the stale layout; the letters are the control.
    for (const key of ['a', ' ', 'b', BACKSPACE, BACKSPACE]) {
      await app.type(key)
      // Sample across the frames the keystroke produces, not just the last.
      for (let t = 0; t < 8; t++) {
        expect(app.handle().getScrollTop()).toBe(top)
        expect(app.handle().getViewportHeight()).toBe(viewport)
        await delay(25)
      }
    }

    app.unmount()
  })
})
