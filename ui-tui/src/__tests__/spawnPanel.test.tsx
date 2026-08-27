// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { SpawnRunState } from '../domain/spawnRun.js'
import type { TranscriptMessage } from '../rpc/index.js'
import type { Episode } from '../types.js'

import { resetDagNodeTraces, setDagNodeTrace } from '../app/dagNodeStore.js'
import { EpisodeView } from '../components/episodeView.js'
import { SpawnPanel } from '../components/spawnPanel.js'
import { resetSpawnOpen, spawnTraceKey, toggleSpawnTrace } from '../lib/spawnOpen.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')

const run = (over: Partial<SpawnRunState> = {}): SpawnRunState => ({
  taskId: '1a021575',
  toolCallId: 'call-1',
  callId: '20260827T021530000000Z-1a021575',
  label: 'research carbon monoxide',
  agent: 'raven-research',
  status: 'running',
  startedAt: 1000,
  ...over
})

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

beforeEach(() => {
  resetDagNodeTraces()
  resetSpawnOpen()
})

describe('SpawnPanel', () => {
  it('carries the call, the label and the elapsed time in its header', () => {
    const f = frame(<SpawnPanel now={61_000} run={run()} t={DEFAULT_THEME} width={72} />)

    expect(f).toContain('spawn')
    expect(f).toContain('research carbon monoxide')
    expect(f).toContain('1m 0s')
    // The spinner already says "running"; the word would say it twice.
    expect(f).not.toContain('running')
  })

  it('opens the trace by default while the run works, drawn by the transcript renderer', () => {
    setDagNodeTrace(spawnTraceKey(run().callId!), [say('reading the WHO guidance')], false)

    const f = frame(<SpawnPanel run={run()} t={DEFAULT_THEME} width={72} />)

    expect(f).toContain('reading the WHO guidance')
    // The trace header names the record and the handle, the way a dag box does.
    expect(f).toContain('raven-research')
    expect(f).toContain('/agents for the full trace')
  })

  it('shows the prompt until the first step reaches the collector', () => {
    const f = frame(<SpawnPanel prompt="research CO and report back" run={run()} t={DEFAULT_THEME} width={72} />)

    expect(f).toContain('research CO and report back')
  })

  it('folds the trace once the run settles', () => {
    const settled = run({ endedAt: 5000, status: 'completed' })

    setDagNodeTrace(spawnTraceKey(settled.callId!), [say('the answer')], true)

    const f = frame(<SpawnPanel run={settled} t={DEFAULT_THEME} width={72} />)

    expect(f).not.toContain('the answer')
    expect(f).toContain('completed')
    expect(f).toContain('click to open the trace')
  })

  it('lets the reader reopen a settled run, their toggle winning over the default', () => {
    const settled = run({ endedAt: 5000, status: 'completed' })

    setDagNodeTrace(spawnTraceKey(settled.callId!), [say('the answer')], true)
    toggleSpawnTrace(settled)

    expect(frame(<SpawnPanel run={settled} t={DEFAULT_THEME} width={72} />)).toContain('the answer')
  })

  it('tints the frame of a failed run', () => {
    const f = render(
      <SpawnPanel run={run({ endedAt: 5000, status: 'failed' })} t={DEFAULT_THEME} width={72} />
    ).lastFrame()

    expect(stripAnsi(f ?? '')).toContain('failed')
  })
})

describe('EpisodeView with a spawn call', () => {
  const episodes: Episode[] = [
    {
      index: 0,
      tools: [
        {
          id: 'call-1',
          name: 'spawn',
          summary: 'research carbon monoxide for me',
          resultPreview: 'Subagent [research carbon monoxide] started (id: 1a021575).',
          ok: true,
          done: true,
          spawn: run()
        }
      ]
    }
  ]

  it('suppresses the call row and draws the panel instead', () => {
    const f = frame(<EpisodeView cols={80} episodes={episodes} t={DEFAULT_THEME} />)

    // The panel is a titled box carrying the call; the "delegated ..." activity
    // row above it would say the same thing twice.
    expect(f).toContain('spawn')
    expect(f).not.toContain('delegated')
  })
})
