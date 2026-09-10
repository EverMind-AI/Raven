// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { LiveAgentRow, LiveDagRun } from '../app/liveAgentsStore.js'
import type { InstanceRow } from '../rpc/generated.js'

import {
  dagLineName,
  dagLineStats,
  isDagRunActive,
  isHereLine,
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
  it('puts each graph above its own nodes, the whole block after the loose spawns', () => {
    const { lines } = stripLines(
      [
        row({ id: 'run-1/b', kind: 'dag-node', label: 'b', runId: 'run-1', seq: 2 }),
        row({ id: 'spawn', seq: 3 }),
        row({ id: 'run-1/c', kind: 'dag-node', label: 'c', runId: 'run-1', seq: 4, status: 'pending' })
      ],
      [dagRun()]
    )

    expect(lines.map(l => (l.kind === 'dag' ? `dag:${l.run.runId}` : `${l.row.id}${l.indent ? ':in' : ''}`))).toEqual([
      'spawn',
      'dag:run-1',
      'run-1/b:in',
      'run-1/c:in'
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
    const { lines, overflow } = stripLines([], runs, [], null, 4, 3)

    expect(lines.map(l => (l.kind === 'dag' ? l.run.runId : l.row.id))).toEqual(['r2', 'r3', 'r4'])
    expect(overflow).toBe(1)
  })

  it('shows a node whose graph line was dropped as a loose row', () => {
    const { lines } = stripLines([row({ id: 'run-9/x', kind: 'dag-node', runId: 'run-9' })], [dagRun()])

    expect(lines.map(l => (l.kind === 'dag' ? 'dag' : `${l.row.id}${l.indent ? ':in' : ''}`))).toEqual([
      'run-9/x',
      'dag'
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

const inst = (over: Partial<InstanceRow> & Pick<InstanceRow, 'agent' | 'handle'>): InstanceRow => ({
  createdAtMs: 0,
  kind: 'acp',
  resumable: true,
  sessionKey: 's1',
  status: 'completed',
  ...over
})

const rowLines = (lines: ReturnType<typeof stripLines>['lines']) => lines.flatMap(l => (l.kind === 'row' ? [l] : []))

describe('stripLines — instance rows', () => {
  const targetsOf = (lines: ReturnType<typeof stripLines>['lines']) =>
    rowLines(lines).map(l => (l.target === null ? 'Raven' : l.target?.handle))

  it('keeps an idle resumable instance on the strip, as a switch target', () => {
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1' })], null)

    expect(targetsOf(lines)).toEqual(['Raven', 'h1'])
    expect(rowLines(lines)[1]?.row.status).toBe('completed')
  })

  it('heads the layer with the way back to the main conversation', () => {
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1' })], null)
    const [main] = rowLines(lines)

    expect(main?.target).toBeNull()
    expect(main?.row.label).toBe('Raven')
  })

  it('offers no way back when there is nothing to be back from', () => {
    expect(stripLines([], [], [], null).lines).toEqual([])
  })

  it('leaves a non-resumable finished instance out, and does not count it as overflow', () => {
    const { lines, overflow } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1', resumable: false })], null)

    expect(lines).toEqual([])
    expect(overflow).toBe(0)
  })

  it('keeps the active instance whatever its resumable flag says', () => {
    const active = { agent: 'Coder', handle: 'h1' }
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1', resumable: false })], active)

    expect(targetsOf(lines)).toEqual(['Raven', 'h1'])
  })

  it('merges a live spawn row into its instance line, live state winning', () => {
    const live = row({ agent: 'Coder', id: 'task-1', instance: 'h1', startedAtMs: 5, status: 'running' })
    const { lines } = stripLines([live], [], [inst({ agent: 'Coder', handle: 'h1' })], null)
    const line = rowLines(lines)[1]

    expect(targetsOf(lines)).toEqual(['Raven', 'h1'])
    expect(line?.row.id).toBe('task-1')
    expect(line?.row.status).toBe('running')
  })

  it('reads running off the registry row when no live row exists', () => {
    // The legacy gateway bus carries no `subagent.status` events; the bullet
    // still has to be honest there.
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1', status: 'running' })], null)

    expect(rowLines(lines)[1]?.row.status).toBe('running')
  })

  it('does not read an interrupted instance as running', () => {
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1', status: 'interrupted' })], null)

    expect(rowLines(lines)[1]?.row.status).toBe('completed')
  })

  it('suppresses the instance row while its dag node line is up, and hangs it under the graph after', () => {
    const node = row({ id: 'run-1/build', kind: 'dag-node', label: 'build', runId: 'run-1' })
    const instance = inst({ agent: 'Coder', handle: 'build', nodeId: 'build', runId: 'run-1' })

    const during = stripLines([node], [dagRun()], [instance], null)
    expect(rowLines(during.lines)).toHaveLength(1)
    expect(rowLines(during.lines)[0]?.target).toBeUndefined()

    const after = stripLines([], [dagRun()], [instance], null)
    expect(targetsOf(after.lines)).toEqual(['Raven', 'build'])
    expect(rowLines(after.lines)[1]?.indent).toBe(true)
  })

  it('synthesizes the graph header from the rows themselves after a resume', () => {
    const a = inst({
      agent: 'Coder',
      createdAtMs: 1,
      handle: 'node-a',
      nodeId: 'a',
      runId: 'run-9',
      runTitle: 'ship the site'
    })
    const b = inst({ agent: 'Writer', createdAtMs: 2, handle: 'node-b', nodeId: 'b', runId: 'run-9' })
    const { lines } = stripLines([], [], [a, b], null)
    const header = lines.find(l => l.kind === 'dag')

    expect(header?.kind === 'dag' && header.run.summary).toBe('ship the site')
    expect(targetsOf(lines)).toEqual(['Raven', 'node-a', 'node-b'])
  })

  it('gives a synthesized header no tally, having no nodes to count', () => {
    expect(dagLineStats({ nodes: {}, runId: 'run-9', seq: -1 }, 1_000_000)).toBe('')
  })

  it('caps idle instances per graph and counts the dropped', () => {
    const instances = ['a', 'b', 'c'].map((n, i) =>
      inst({ agent: 'Coder', createdAtMs: i, handle: `node-${n}`, nodeId: n, runId: 'run-1' })
    )
    const { lines, overflow } = stripLines([], [dagRun()], instances, null)

    expect(targetsOf(lines)).toEqual(['Raven', 'node-a', 'node-b'])
    expect(overflow).toBe(1)
  })

  it('lets grouped rows fall flat once the graph-line budget is spent', () => {
    const instances = ['r1', 'r2', 'r3', 'r4'].map((runId, i) =>
      inst({ agent: 'Coder', createdAtMs: i, handle: `h-${runId}`, nodeId: 'n', runId, runTitle: runId })
    )
    const { lines } = stripLines([], [], instances, null)
    const headers = lines.flatMap(l => (l.kind === 'dag' ? [l.run.runId] : []))

    expect(headers).toEqual(['r2', 'r3', 'r4'])
    expect(targetsOf(lines)).toEqual(['Raven', 'h-r1', 'h-r2', 'h-r3', 'h-r4'])
  })

  it('caps idle instance rows and counts the dropped as overflow', () => {
    const instances = ['h1', 'h2', 'h3', 'h4'].map((handle, i) => inst({ agent: 'Coder', createdAtMs: i, handle }))
    const { lines, overflow } = stripLines([], [], instances, null)

    expect(targetsOf(lines)).toEqual(['Raven', 'h1', 'h2'])
    expect(overflow).toBe(2)
  })

  it('never drops the active target for the idle cap, nor the way back', () => {
    const instances = ['h1', 'h2', 'h3', 'h4'].map((handle, i) => inst({ agent: 'Coder', createdAtMs: i, handle }))
    const { lines, overflow } = stripLines([], [], instances, { agent: 'Coder', handle: 'h4' })

    expect(targetsOf(lines)).toEqual(['Raven', 'h1', 'h4'])
    expect(overflow).toBe(2)
  })

  it('does not spend the idle budget on running instances', () => {
    const instances = [
      inst({ agent: 'Coder', createdAtMs: 1, handle: 'busy1', status: 'running' }),
      inst({ agent: 'Coder', createdAtMs: 2, handle: 'busy2', status: 'running' }),
      inst({ agent: 'Coder', createdAtMs: 3, handle: 'idle1' }),
      inst({ agent: 'Coder', createdAtMs: 4, handle: 'idle2' })
    ]
    const { lines, overflow } = stripLines([], [], instances, null)

    expect(targetsOf(lines)).toEqual(['Raven', 'busy1', 'busy2', 'idle1', 'idle2'])
    expect(overflow).toBe(0)
  })
})

describe('isHereLine', () => {
  const hereOf = (lines: ReturnType<typeof stripLines>['lines'], active: Parameters<typeof stripLines>[3]) =>
    lines.filter(l => isHereLine(l, active)).map(l => (l.kind === 'row' ? (l.target?.handle ?? 'Raven') : 'dag'))

  it('marks the way-back row while the main conversation is the one on screen', () => {
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1' })], null)

    expect(hereOf(lines, null)).toEqual(['Raven'])
  })

  it('moves to the instance the user switched into, and off the way back', () => {
    const active = { agent: 'Coder', handle: 'h1' }
    const { lines } = stripLines([], [], [inst({ agent: 'Coder', handle: 'h1' })], active)

    expect(hereOf(lines, active)).toEqual(['h1'])
  })

  it('tells two instances of the same agent apart', () => {
    const active = { agent: 'Coder', handle: 'h2' }
    const instances = ['h1', 'h2'].map((handle, i) => inst({ agent: 'Coder', createdAtMs: i, handle }))
    const { lines } = stripLines([], [], instances, active)

    expect(hereOf(lines, active)).toEqual(['h2'])
  })

  it('marks an instance kept under its own graph line', () => {
    const active = { agent: 'Coder', handle: 'h1' }
    const { lines } = stripLines(
      [],
      [dagRun({ runId: 'run-1' })],
      [inst({ agent: 'Coder', handle: 'h1', runId: 'run-1' })],
      active
    )

    expect(hereOf(lines, active)).toEqual(['h1'])
  })

  it('marks nothing a click cannot switch to: graph lines and loose spawn rows', () => {
    const { lines } = stripLines([row({ agent: 'Coder', id: 'task-1', status: 'running' })], [dagRun()], [], null)

    expect(hereOf(lines, null)).toEqual([])
  })

  it('always has a row to mark, since the active target is never dropped', () => {
    const active = { agent: 'Coder', handle: 'h4' }
    const instances = ['h1', 'h2', 'h3', 'h4'].map((handle, i) => inst({ agent: 'Coder', createdAtMs: i, handle }))
    const { lines } = stripLines([], [], instances, active)

    expect(hereOf(lines, active)).toEqual(['h4'])
  })
})
