// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DagRunState } from '../domain/dagRun.js'

import { turnController } from '../app/turnController.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { LiveDagPanels } from '../components/streamingAssistant.js'
import { stripAnsi } from '../lib/text.js'

afterEach(() => {
  resetTurnState()
  turnController.reset()
})

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')

const run = (runId: string, nodeId: string): DagRunState => ({
  runId,
  done: false,
  // The graph labels its boxes with the sub-agent, so that is what identifies a
  // run on screen; the node id only appears once a node is opened.
  nodes: [{ id: nodeId, subagent: nodeId, dependsOn: [], status: 'running' }]
})

describe('LiveDagPanels', () => {
  it('renders nothing when the turn has no DAG run', () => {
    resetTurnState()

    expect(frame(<LiveDagPanels />)).toBe('')
  })

  it('draws the in-flight graph', () => {
    // The legacy transcript has no episode tool rows to pin a graph to, so this
    // is the only place a run shows up in that mode.
    resetTurnState()
    patchTurnState({ dagRuns: [run('dag-1', 'fetch')] })

    const f = frame(<LiveDagPanels />)

    expect(f).toContain('dag-1')
    expect(f).toContain('fetch')
  })

  it('draws every concurrent run', () => {
    resetTurnState()
    patchTurnState({ dagRuns: [run('dag-1', 'fetch'), run('dag-2', 'crawl')] })

    const f = frame(<LiveDagPanels />)

    expect(f).toContain('fetch')
    expect(f).toContain('crawl')
  })
})
