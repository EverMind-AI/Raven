// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { stringWidth } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { Msg } from '../types.js'

import { DagPanel, fitDagHeader } from '../components/dagPanel.js'
import { EpisodeView } from '../components/episodeView.js'
import { $dagOpenNodes, dagNodeKey, dagNodeToggleKey, dagSpanToggleKey, toggleDagNode } from '../lib/dagOpenNodes.js'
import { stripAnsi } from '../lib/text.js'
import { estimatedMsgHeight } from '../lib/virtualHeights.js'
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
  it('names every node on a row, and its agent in the box and the row alike', () => {
    const named: DagRunState = {
      ...DIAMOND,
      nodes: DIAMOND.nodes.map((item, index) => ({ ...item, subagent: `agent-${index + 1}` }))
    }
    const f = frame(<DagPanel run={named} t={DEFAULT_THEME} />)

    for (const node of DIAMOND.nodes) {
      expect(f).toContain(node.id)
    }

    for (const agent of ['agent-1', 'agent-2', 'agent-3', 'agent-4']) {
      // Twice: the box says who is running the node, the row's own column says
      // it again so the run can be scanned by agent without reading the shape.
      expect(f.split(agent).length - 1).toBe(2)
    }

    expect(f).toContain('✓')
    expect(f).toContain('○')
  })

  it('shows the call, the size of the graph and the tally in one header', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('run subagent dag')
    expect(f).toContain('4 nodes')
    // Glyphs, not words: the header has the row it shares with the run id and
    // the elapsed time, and `1 done · 1 running · 2 pending` spent all of it.
    expect(f).toContain('1✓ 1● 2○')
  })

  it('frames the whole run, so a graph reads as one thing in the transcript', () => {
    const lines = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={80} />)
      .split('\n')
      .filter(line => line.trim())

    expect(lines[0]?.startsWith('╭')).toBe(true)
    expect(lines.at(-1)?.startsWith('╰')).toBe(true)
    // The call names itself inside the frame; the transcript row that used to
    // carry it is suppressed for a dag call (see `WorkSegment`).
    expect(lines[1]).toContain('run subagent dag')
  })

  it('draws the topology as a graph under the header', () => {
    const lines = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />).split('\n')

    expect(lines.some(line => line.includes('\u256d') && line.includes('\u256e'))).toBe(true)
    expect(lines.findIndex(line => line.includes('\u25b8'))).toBeGreaterThan(
      lines.findIndex(line => line.includes('4 nodes'))
    )
  })

  it('numbers the boxes so a detail block can be matched to one', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toMatch(/1 .*echo/)
    expect(f).toMatch(/4 .*echo/)
  })

  it('names a dependency from an earlier run in the detail, which has no box', () => {
    // The picture can only draw an edge between two of its own boxes. That
    // dependency used to survive on the node's row; the detail block is the
    // only place left for it.
    const run: DagRunState = {
      runId: 'dag-x',
      done: false,
      nodes: [node('only', 'pending', ['from_an_earlier_run'])]
    }

    $dagOpenNodes.set(new Set([dagNodeKey('dag-x', 'only')]))

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('from_an_earlier_run')
  })

  it('drops the picture rather than overflow a narrow terminal', () => {
    // Boxes plus gutters need more cells than this, in either label style, so
    // the rows are all that is left. The frame stays -- it is the panel's own,
    // not a node's, which is why the wire arrow is what is asserted.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={36} />)

    expect(f).not.toContain('\u25b8')
    expect(f).toContain('parse_a')
  })

  it('keeps the rows when the picture fits but cannot name its boxes', () => {
    // The compact label style is boxes of bare ordinals: it draws the shape and
    // names nothing, so the rows are still the only place a node is identified.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={40} />)

    expect(f).toContain('\u256d')
    expect(f).toContain('parse_a')
  })

  it('keeps sibling boxes adjacent and their join below them', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={36} />)
    // Only the node rows: the line turned in under one of them names the nodes
    // it waits on, and would be found by a search for those names.
    const lines = f
      .split('\n')
      .map(line => line.replace(/^\u2502 ?/, '').trim())
      .filter(line => !line.startsWith('\u2514'))
    const rowOf = (id: string) => lines.findIndex(line => line.includes(id))

    // parse_a and parse_b are siblings, so they are adjacent and above report.
    expect(Math.abs(rowOf('parse_a') - rowOf('parse_b'))).toBe(1)
    expect(rowOf('report')).toBeGreaterThan(rowOf('parse_b'))
  })

  it('shows which sub-agent runs each node', () => {
    const run: DagRunState = { ...DIAMOND, nodes: [node('draft', 'running', [], 'claude')] }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('claude')
  })

  it("reports a failed node's error without a click", () => {
    // An error is the one thing a reader must not have to open a node to read,
    // and a red box can only say that something went wrong somewhere.
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
      dir: '/w/mas_dag/dag-1',
      summary: { total: 1, completed: 1, failed: 0, skipped: 0 },
      nodes: [node('a', 'completed')]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('/w/mas_dag/dag-1')
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

    $dagOpenNodes.set(new Set([dagNodeKey('dag-1', 'revise')]))

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('claude@author')
  })
})

describe('DagPanel node rows', () => {
  beforeEach(() => {
    $dagOpenNodes.set(new Set())
  })

  const WITH_PROMPTS: DagRunState = {
    runId: 'dag-2',
    done: false,
    nodes: [
      {
        ...node('inspect_i18n_messages_20260820', 'running', [], 'Coder'),
        instance: 'inspect-i18n-messages-20260820-1a4a53',
        promptTemplate: 'Inspect the i18n messages for dag.* keys\n\nCheck both locales carefully.'
      }
    ]
  }

  // Wide enough for a picture, too narrow for it to fit `Coder` inside a box --
  // the compact label style, where the row is the only place the agent is named.
  const rowFrame = (run: DagRunState) => frame(<DagPanel run={run} t={DEFAULT_THEME} width={44} />)

  it('reads a node as its own name and the agent that ran it', () => {
    // Columns, not a sentence: both are padded to the run's widest, so a reader
    // scans one of them down rather than reading every row.
    expect(rowFrame(WITH_PROMPTS)).toMatch(/inspect_i18n_messages…\s+Coder/)
  })

  it('keeps the instance handle off the collapsed row', () => {
    // Forty cells for a generated suffix, against a row whose point is the
    // words. The agent half of it is already the row's own second column.
    expect(rowFrame(WITH_PROMPTS)).not.toContain('inspect-i18n-messages-20260820-1a4a53')
  })

  it('names the instance handle in the expanded block, beside the node id', () => {
    $dagOpenNodes.set(new Set([dagNodeKey('dag-2', 'inspect_i18n_messages_20260820')]))

    expect(frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)).toContain(
      'Coder@inspect-i18n-messages-20260820-1a4a53'
    )
  })

  it('puts the status mark in the margin column, ahead of the ordinal', () => {
    const row = rowFrame(WITH_PROMPTS)
      .split('\n')
      .find(line => line.includes('inspect_i18n'))!

    // The same margin the transcript's reply marker and the reasoning rule take.
    // The mark itself is a spinner frame while the node runs, so this asserts
    // the columns after it rather than the glyph. The frame and its padding
    // open the line, which is what is sliced off here.
    expect(row.slice(3)).toMatch(/^ 1 {2}inspect/)
  })

  it('turns what the node was asked in under its name, off the columns', () => {
    // The summary is a sentence and the rest of the row is columns; sharing one
    // line, the sentence took every cell the columns did not.
    const lines = rowFrame(WITH_PROMPTS)
      .split('\n')
      .map(line => line.replace(/^\u2502 ?/, '').trim())

    expect(lines.find(line => line.includes('inspect_i18n'))).not.toContain('Inspect the i18n')
    expect(lines.find(line => line.startsWith('\u2514'))).toContain('Inspect the i18n')
  })

  it('keeps the instance handle off the row entirely', () => {
    // Forty cells for a generated suffix, against a row whose point is the
    // node's name. It is one click away, in the node's own block.
    expect(rowFrame(WITH_PROMPTS)).not.toContain('inspect-i18n-messages-20260820-1a4a53')
  })

  it('clips a long node id rather than overrun the row', () => {
    const run: DagRunState = {
      runId: 'dag-3',
      done: false,
      nodes: [{ ...node('a'.repeat(120), 'running', [], 'a-name-too-long-for-a-box') }]
    }
    const lines = frame(<DagPanel run={run} t={DEFAULT_THEME} width={60} />).split('\n')

    lines.forEach(line => expect(line.length).toBeLessThanOrEqual(60))
  })

  it('prefers the summary the node was dispatched with over the template heuristic', () => {
    // Both fields share the same `string | undefined` type, so a swapped
    // argument order at the call site would not be a type error -- only a
    // render test can catch it.
    const run: DagRunState = {
      runId: 'dag-5',
      done: false,
      nodes: [
        {
          ...node('summarize', 'running', [], 'Coder'),
          nodeSummary: 'audit the skills directory',
          promptTemplate: 'Do something else entirely, elaborated at length.'
        }
      ]
    }
    // The line the node was dispatched with is what the block leads with; the
    // row itself carries neither, which is why this opens the node.
    toggleDagNode(dagNodeKey('dag-5', 'summarize'))

    const f = frame(<DagPanel run={run} t={DEFAULT_THEME} />)

    expect(f).toContain('audit the skills directory')
  })

  it('shows the full prompt and the node id once the row is expanded', () => {
    toggleDagNode(dagNodeKey('dag-2', 'inspect_i18n_messages_20260820'))

    const f = frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)

    expect(f).toContain('inspect_i18n_messages_20260820')
    expect(f).toContain('Check both locales carefully.')
  })

  it('leaves the other nodes of the run collapsed', () => {
    const run: DagRunState = {
      runId: 'dag-4',
      done: false,
      nodes: [
        { ...node('a', 'running', [], 'Coder'), promptTemplate: 'first\nAAA_BODY' },
        { ...node('b', 'pending', [], 'Coder'), promptTemplate: 'second\nBBB_BODY' }
      ]
    }

    toggleDagNode(dagNodeKey('dag-4', 'a'))

    const f = frame(<DagPanel run={run} t={DEFAULT_THEME} />)

    expect(f).toContain('AAA_BODY')
    expect(f).not.toContain('BBB_BODY')
  })

  it('still opens a node with no template, which is where its id now lives', () => {
    // It used to open nothing, on the grounds that its row already showed the
    // id. With the graph replacing the rows the box shows an ordinal and an
    // agent, and the detail block is the only place the id survives.
    toggleDagNode(dagNodeKey(DIAMOND.runId, 'fetch'))

    expect(frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)).toContain('fetch')
  })

  it('shows one node at a time, closing whatever the run had open', () => {
    const run: DagRunState = {
      runId: 'dag-5',
      done: false,
      nodes: [
        { ...node('a', 'running', [], 'Coder'), promptTemplate: 'first\nAAA_BODY' },
        { ...node('b', 'pending', [], 'Coder'), promptTemplate: 'second\nBBB_BODY' }
      ]
    }

    toggleDagNode(dagNodeKey('dag-5', 'a'))
    toggleDagNode(dagNodeKey('dag-5', 'b'))

    const f = frame(<DagPanel run={run} t={DEFAULT_THEME} />)

    expect(f).toContain('BBB_BODY')
    expect(f).not.toContain('AAA_BODY')
  })

  it('keys expansion by run as well as node, so two runs cannot share a toggle', () => {
    // Node ids are only unique within a run; a bare node key would expand the
    // same-named node of every graph in the transcript.
    toggleDagNode(dagNodeKey('other-run', 'inspect_i18n_messages_20260820'))

    expect(frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)).not.toContain('Check both locales carefully.')
  })
})

describe('toggleDagNode', () => {
  beforeEach(() => {
    $dagOpenNodes.set(new Set())
  })

  it('opens a closed node and closes an open one', () => {
    toggleDagNode('r/a')
    expect($dagOpenNodes.get().has('r/a')).toBe(true)

    toggleDagNode('r/a')
    expect($dagOpenNodes.get().has('r/a')).toBe(false)
  })

  it('publishes a new set, so a subscriber re-renders', () => {
    const before = $dagOpenNodes.get()

    toggleDagNode('r/a')

    expect($dagOpenNodes.get()).not.toBe(before)
  })
})

describe('dagSpanToggleKey', () => {
  const nodes = [
    { ...node('a', 'running', [], 'Coder'), promptTemplate: 'first line\nBODY' },
    node('b', 'pending', ['a'], 'Coder')
  ]

  it('opens the same block the node row opens', () => {
    // The box is the bigger target and the thing a reader is already looking at,
    // so it must not open a second, separate disclosure.
    expect(dagSpanToggleKey('dag-6', { kind: 'border', nodeId: 'a', text: '\u256d\u2500\u256e' }, nodes)).toBe(
      dagNodeKey('dag-6', 'a')
    )
  })

  it('opens nothing for a wire span, which belongs to no node', () => {
    expect(dagSpanToggleKey('dag-6', { kind: 'wire', text: '\u2500\u252c\u2500' }, nodes)).toBeNull()
  })

  it('opens a node with no template too, which is where its id now lives', () => {
    // It used to return null here, on the grounds that the node's row already
    // printed the id. The graph replaced that row, so the box is the only way
    // in and the detail block is the only way out.
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'b', text: '2 \u25cb Coder' }, nodes)).toBe('dag-6/b')
  })

  it('opens nothing for a node absent from the run', () => {
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'ghost', text: 'x' }, nodes)).toBeNull()
  })
})

describe('DagPanel node slot', () => {
  it('hangs nothing under a running node until it is opened', () => {
    // A live tail used to sit under every running row. One row per running node,
    // moving several times a second, is what a three-node graph read as; the
    // trace is still one click away, in the box the row opens.
    const rows = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)
      .split('\n')
      .filter(line => line.trim())

    expect(rows.some(line => line.includes('working'))).toBe(false)
  })

  it('expands a node that has no prompt template', () => {
    // Before the trace existed, the template was the only thing a click could
    // reveal, so a node without one was not clickable. There is a trace now.
    const bare: DagRunState = { ...DIAMOND, nodes: [node('solo', 'completed')] }

    expect(dagNodeToggleKey('r1', bare.nodes[0]!)).toBe(dagNodeKey('r1', 'solo'))
  })

  it('expands a pending node that does carry a template', () => {
    expect(dagNodeToggleKey('r1', { ...node('later', 'pending'), promptTemplate: 'do it' })).not.toBeNull()
  })
})

describe('the frame holds', () => {
  // ink's `truncate-end` is a no-op on a Text holding nested Texts, and every
  // line of this panel is nested Texts -- so nothing here is clipped by the
  // renderer and every line has to be cut in cells by the panel itself. When it
  // is not, the row wraps or its right-hand columns walk out through the frame,
  // which is what a terminal resize used to do to it.
  const LOADED: DagRunState = {
    runId: '20260826T06:11:02Z-ed3fc2c3',
    done: false,
    nodes: [
      {
        ...node('gem_research', 'running', [], 'Raven-Research'),
        nodeSummary: 'Research G.E.M. for the album list, the box-office ranking, the Kai Tak show, and the tour.'
      },
      {
        ...node('gem_website', 'failed', [], 'Raven-Code'),
        error: 'CLI agent exited 1 while writing the third file, after a long explanation of why',
        nodeSummary: '写一个介绍邓紫棋的网站，放在 /Users/admin/workspace/test 目录下'
      },
      {
        ...node('gem_detail_page', 'pending', ['gem_research', 'from_an_earlier_run'], 'Raven-Code'),
        nodeSummary: 'Add a detail page'
      }
    ]
  }

  // The test terminal is 100 columns; a panel asked for more than that is
  // clipped by the harness rather than by the panel, which proves nothing.
  it.each([28, 30, 36, 44, 60, 72, 88, 100])('draws every line to exactly its own width at %i columns', cols => {
    const lines = frame(<DagPanel run={LOADED} t={DEFAULT_THEME} width={cols} />)
      .split('\n')
      .filter(line => line.trim())

    lines.forEach(line => expect(stringWidth(line)).toBe(Math.max(28, cols)))
  })
})

describe('the turned-in line', () => {
  const withSummary = (over: Partial<DagRunState> = {}): DagRunState => ({
    runId: 'dag-7',
    done: false,
    nodes: [
      { ...node('gem_site', 'completed', [], 'Raven-Code'), nodeSummary: '用 Raven-Code 搭建一个介绍邓紫棋的静态网站' },
      node('gem_bare', 'pending', [], 'Raven-Code')
    ],
    ...over
  })

  it('leaves the first line to the columns and turns the summary in under the name', () => {
    const lines = frame(<DagPanel run={withSummary()} t={DEFAULT_THEME} width={80} />)
      .split('\n')
      .map(line => line.replace(/^\u2502 ?/, '').trimEnd())

    const row = lines.findIndex(line => line.includes('gem_site'))

    expect(lines[row]).toContain('Raven-Code')
    expect(lines[row]).not.toContain('搭建')
    expect(lines[row + 1]?.trim().startsWith('\u2514')).toBe(true)
    expect(lines[row + 1]).toContain('搭建一个介绍邓紫棋的静态网站')
  })

  it('stands its mark in the ordinal column and starts under the node name', () => {
    const lines = frame(<DagPanel run={withSummary()} t={DEFAULT_THEME} width={80} />)
      .split('\n')
      .map(line => line.replace(/^\u2502 ?/, ''))

    const row = lines.findIndex(line => line.includes('gem_site'))

    expect(lines[row + 1]?.indexOf('\u2514')).toBe(lines[row]?.indexOf('1'))
    expect(lines[row + 1]?.indexOf('\u7528')).toBe(lines[row]?.indexOf('gem_site'))
  })

  it('costs a node with nothing to say there no second line at all', () => {
    const lines = frame(<DagPanel run={withSummary()} t={DEFAULT_THEME} width={80} />)
      .split('\n')
      .map(line => line.replace(/^\u2502 ?/, '').trimEnd())

    const row = lines.findIndex(line => line.includes('gem_bare'))

    expect(lines[row + 1]?.trim().startsWith('\u2514')).toBe(false)
  })
})

describe('the height the transcript reserves', () => {
  // The estimator draws nothing; it decides how many rows the virtualised
  // transcript holds open for this panel. Under-count and the row beneath
  // paints over the panel's tail, which is what a corrupted-looking box is.
  const msgWith = (dag: DagRunState): Msg => ({
    episodes: [{ index: 0, tools: [{ dag, id: 't0', name: 'run_subagent_dag', ok: true, summary: 'dag' }] }],
    kind: 'episodes',
    role: 'assistant',
    text: ''
  })

  const RUN: DagRunState = {
    runId: 'dag-8',
    done: false,
    nodes: [
      { ...node('gem_site', 'completed', [], 'Raven-Code'), nodeSummary: '用 Raven-Code 搭建一个介绍邓紫棋的静态网站' },
      node('gem_bare', 'running', [], 'Raven-Code'),
      { ...node('gem_last', 'pending', ['gem_site'], 'Raven-Code'), error: 'exited 1' }
    ]
  }

  it.each([
    ['closed', new Set<string>()],
    ['with a node open', new Set([dagNodeKey('dag-8', 'gem_site')])]
  ])('matches what the panel draws, %s', (_label, open) => {
    $dagOpenNodes.set(open)

    // What episodeView hands the panel at 92 columns: one INDENT in, on a
    // transcript body four columns narrower than the terminal.
    const drawn = frame(<EpisodeView cols={92} episodes={msgWith(RUN).episodes!} t={DEFAULT_THEME} />)
      .split('\n')
      .filter(line => line.trim()).length

    expect(estimatedMsgHeight(msgWith(RUN), 92, { compact: false, dagOpen: open, details: false })).toBe(drawn)
  })
})

describe('fitDagHeader', () => {
  const fit = (inner: number) => fitDagHeader(inner, 'run subagent dag  3 nodes', 'dag-1…-abc', '1✓ 1● 1○', '4m 12s')

  it('keeps everything when everything fits', () => {
    expect(fit(80)).toEqual({
      elapsed: '4m 12s',
      left: 'run subagent dag  3 nodes',
      runId: 'dag-1…-abc',
      tally: '1✓ 1● 1○'
    })
  })

  it('gives up the run id first, which names nothing a reader is looking for', () => {
    expect(fit(46)).toMatchObject({ elapsed: '4m 12s', runId: '', tally: '1✓ 1● 1○' })
  })

  it('gives up the elapsed time next, which the strip also carries', () => {
    expect(fit(36)).toMatchObject({ elapsed: '', runId: '', tally: '1✓ 1● 1○' })
  })

  it('keeps the call itself, cut to the row, when even the tally cannot fit', () => {
    const fitted = fit(20)

    expect(fitted).toMatchObject({ elapsed: '', runId: '', tally: '' })
    expect(stringWidth(fitted.left)).toBeLessThanOrEqual(20)
  })
})
