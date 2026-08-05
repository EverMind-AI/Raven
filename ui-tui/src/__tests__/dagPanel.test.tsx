// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'

import { DagPanel } from '../components/dagPanel.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')

const node = (id: string, status: DagRunNodeStatus, dependsOn: string[] = [], subagent = 'echo') => ({
  id,
  subagent,
  dependsOn,
  status
})

const DIAMOND: DagRunState = {
  runId: 'dag-20260805-abc',
  toolCallId: 'call-a',
  done: false,
  nodes: [
    node('fetch', 'completed'),
    node('parse_a', 'running', ['fetch']),
    node('parse_b', 'pending', ['fetch']),
    node('report', 'pending', ['parse_a', 'parse_b'])
  ]
}

describe('DagPanel', () => {
  it('draws one row per node with its status glyph', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('fetch')
    expect(f).toContain('parse_a')
    expect(f).toContain('parse_b')
    expect(f).toContain('report')
    expect(f).toContain('✓')
    expect(f).toContain('●')
    expect(f).toContain('○')
  })

  it('shows the run tally in the header', () => {
    expect(frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)).toContain('4 nodes · 1 done · 1 running')
  })

  it('names each node dependencies so the topology is readable', () => {
    // A terminal cannot route edges, so the edges are spelled out. Without them
    // the drawing is just an indented list and the graph shape is lost.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('parse_a, parse_b')
  })

  it('groups independent nodes onto the same level', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)
    const lines = f.split('\n').map(line => line.trim())
    const rowOf = (id: string) => lines.findIndex(line => line.includes(id))

    // parse_a and parse_b are siblings, so they are adjacent and above report.
    expect(Math.abs(rowOf('parse_a') - rowOf('parse_b'))).toBe(1)
    expect(rowOf('report')).toBeGreaterThan(rowOf('parse_b'))
  })

  it('shows which sub-agent runs each node', () => {
    const run: DagRunState = { ...DIAMOND, nodes: [node('draft', 'running', [], 'claude')] }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('claude')
  })

  it("reports a failed node's error", () => {
    const run: DagRunState = {
      runId: 'dag-1',
      done: true,
      summary: { total: 1, completed: 0, failed: 1, skipped: 0 },
      nodes: [{ ...node('boom', 'failed'), error: 'CLI agent exited 1' }]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('CLI agent exited 1')
  })

  it('shows where a finished run wrote its outputs', () => {
    // The tool result naming the run dir is clamped to 200 chars, so for a graph
    // of any size this is the only place the path survives.
    const run: DagRunState = {
      runId: 'dag-1',
      done: true,
      dir: '/w/.ravenx_dag/dag-1',
      summary: { total: 1, completed: 1, failed: 0, skipped: 0 },
      nodes: [node('a', 'completed')]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('/w/.ravenx_dag/dag-1')
  })

  it('renders nothing for a run with no nodes', () => {
    expect(frame(<DagPanel run={{ runId: 'dag-1', done: false, nodes: [] }} t={DEFAULT_THEME} />)).toBe('')
  })

  it('marks a shared stateful instance, which forces nodes to run in sequence', () => {
    const run: DagRunState = {
      runId: 'dag-1',
      done: false,
      nodes: [
        { ...node('draft', 'running', [], 'claude'), instance: 'author' },
        { ...node('revise', 'pending', ['draft'], 'claude'), instance: 'author' }
      ]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('author')
  })
})
