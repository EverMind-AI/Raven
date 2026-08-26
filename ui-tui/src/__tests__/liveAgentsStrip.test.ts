// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { LiveAgentRow, LiveDagRun } from '../app/liveAgentsStore.js'

import {
  dagLineName,
  dagLineStats,
  isDagRunActive,
  stripLines,
  stripRowLabel,
  stripRows
} from '../components/liveAgentsStrip.js'

const row = (over: Partial<LiveAgentRow>): LiveAgentRow => ({
  id: 't1',
  kind: 'spawn',
  label: 'find the bug',
  seq: 1,
  status: 'running',
  ...over
})

describe('stripRows', () => {
  it('shows active runs only, in insertion order', () => {
    const { overflow, visible } = stripRows([
      row({ id: 'a', status: 'running' }),
      row({ id: 'b', status: 'completed' }),
      row({ id: 'c', status: 'pending' }),
      row({ id: 'd', status: 'failed' })
    ])

    expect(visible.map(r => r.id)).toEqual(['a', 'c'])
    expect(overflow).toBe(0)
  })

  it('caps the strip and counts the rest as overflow', () => {
    const rows = ['a', 'b', 'c', 'd', 'e', 'f'].map(id => row({ id }))
    const { overflow, visible } = stripRows(rows, 4)

    expect(visible).toHaveLength(4)
    expect(overflow).toBe(2)
  })
})

describe('stripRowLabel', () => {
  it('pairs the agent with the run label', () => {
    expect(stripRowLabel(row({ agent: 'research-raven', label: 'jay chou research' }), 80)).toBe(
      'research-raven · jay chou research'
    )
  })

  it('prefers the instance handle for a spawn that has one', () => {
    expect(stripRowLabel(row({ agent: 'Raven-PPT', instance: 'jay-chou-ppt-6247a0' }), 80)).toBe(
      'Raven-PPT · jay-chou-ppt-6247a0'
    )
  })

  it('truncates to the given budget', () => {
    const label = stripRowLabel(row({ agent: 'a'.repeat(50), label: 'b'.repeat(50) }), 20)

    expect(label.length).toBeLessThanOrEqual(20)
    expect(label.endsWith('…')).toBe(true)
  })
})

describe('stripRows lingering', () => {
  const settled = (agoMs: number) => row({ settledAtMs: 1_000_000 - agoMs, status: 'completed' })

  it('drops settled rows by default, which is what makes the strip a live monitor', () => {
    expect(stripRows([settled(0), row({ id: 't2', seq: 2 })], 4, 1_000_000).visible).toHaveLength(1)
  })

  it('keeps a settled row inside the window when one is configured', () => {
    const { visible } = stripRows([settled(5_000)], 4, 1_000_000, 30_000)

    expect(visible.map(r => r.status)).toEqual(['completed'])
  })

  it('drops it again once the window has passed', () => {
    expect(stripRows([settled(45_000)], 4, 1_000_000, 30_000).visible).toEqual([])
  })

  it('drops a settled row carrying no local timestamp, which predates this session', () => {
    // `settledAtMs` is stamped when this client saw the row turn terminal; a row
    // seeded from disk has none and no window it could still be inside.
    expect(stripRows([row({ status: 'completed' })], 4, 1_000_000, 30_000).visible).toEqual([])
  })
})

const dagRun = (over: Partial<LiveDagRun> = {}): LiveDagRun => ({
  nodes: { a: 'completed', b: 'running', c: 'pending' },
  runId: 'run-1',
  seq: 1,
  startedAtMs: 940_000,
  ...over
})

describe('stripLines', () => {
  it('puts each graph above its own nodes, and loose spawns last', () => {
    const { lines } = stripLines(
      [
        row({ id: 'run-1/b', kind: 'dag-node', label: 'b', runId: 'run-1', seq: 2 }),
        row({ id: 'spawn', seq: 3 }),
        row({ id: 'run-1/c', kind: 'dag-node', label: 'c', runId: 'run-1', seq: 4, status: 'pending' })
      ],
      [dagRun()]
    )

    expect(lines.map(l => (l.kind === 'dag' ? `dag:${l.run.runId}` : `${l.row.id}${l.indent ? ':in' : ''}`))).toEqual([
      'dag:run-1',
      'run-1/b:in',
      'run-1/c:in',
      'spawn'
    ])
  })

  it('keeps a graph on the strip once every node of it has settled', () => {
    // The whole point of the graph layer: the nodes leave as they finish, the
    // line they finished under does not.
    const { lines } = stripLines([], [dagRun({ nodes: { a: 'completed', b: 'completed' } })])

    expect(lines).toHaveLength(1)
    expect(lines[0]?.kind).toBe('dag')
  })

  it('keeps the newest graphs and counts the rest as overflow', () => {
    const runs = ['r1', 'r2', 'r3', 'r4'].map((runId, i) => dagRun({ runId, seq: i }))
    const { lines, overflow } = stripLines([], runs, 4, 3)

    expect(lines.map(l => (l.kind === 'dag' ? l.run.runId : l.row.id))).toEqual(['r2', 'r3', 'r4'])
    expect(overflow).toBe(1)
  })

  it('shows a node whose graph line was dropped as a loose row', () => {
    const { lines } = stripLines([row({ id: 'run-9/x', kind: 'dag-node', runId: 'run-9' })], [dagRun()])

    expect(lines.map(l => (l.kind === 'dag' ? 'dag' : `${l.row.id}${l.indent ? ':in' : ''}`))).toEqual([
      'dag',
      'run-9/x'
    ])
  })
})

describe('dag line', () => {
  it('reads as done / running / queued, then how long the graph has been at it', () => {
    expect(dagLineStats(dagRun(), 1_000_000)).toBe('1/3 done · 1 running · 1 queued · 1m 0s')
  })

  it('drops the counts a finished graph has none of, and freezes its duration', () => {
    const run = dagRun({ endedAtMs: 970_000, nodes: { a: 'completed', b: 'completed' } })

    expect(dagLineStats(run, 1_000_000)).toBe('2/2 done · 30s')
    expect(isDagRunActive(run)).toBe(false)
  })

  it('names the failures, which a done count on its own hides', () => {
    expect(dagLineStats(dagRun({ endedAtMs: 940_000, nodes: { a: 'completed', b: 'failed' } }), 1_000_000)).toBe(
      '2/2 done · 1 failed · 0s'
    )
  })

  it('falls back to the run id when the graph reported no goal', () => {
    expect(dagLineName(dagRun(), 40)).toBe('run-1')
    expect(dagLineName(dagRun({ summary: 'ship the site' }), 40)).toBe('ship the site')
  })
})
