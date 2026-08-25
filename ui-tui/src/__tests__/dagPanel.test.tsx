// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'

import { DagPanel } from '../components/dagPanel.js'
import { $dagOpenNodes, dagNodeKey, dagNodeToggleKey, dagSpanToggleKey, toggleDagNode } from '../lib/dagOpenNodes.js'
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
  it('names the agent in its box, and does not repeat it on the row', () => {
    const named: DagRunState = {
      ...DIAMOND,
      nodes: DIAMOND.nodes.map((item, index) => ({ ...item, subagent: `agent-${index + 1}` }))
    }
    const f = frame(<DagPanel run={named} t={DEFAULT_THEME} />)

    for (const agent of ['agent-1', 'agent-2', 'agent-3', 'agent-4']) {
      // Once, in its box. These nodes carry neither a summary nor a template,
      // so their rows fall back to the node id -- the agent column only earns
      // its cells on a row that has something to say beside it.
      expect(f.split(agent).length - 1).toBe(1)
    }

    expect(f).toContain('✓')
    expect(f).toContain('○')
  })

  it('shows the run tally in the header', () => {
    expect(frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)).toContain('4 nodes · 1 done · 1 running')
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
    // Boxes plus gutters need more cells than this, in either label style, so the
    // rows carry the topology in words instead.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={24} />)

    expect(f).not.toContain('\u256d')
    expect(f).toContain('parse_a, parse_b')
  })

  it('keeps the rows when the picture fits but cannot name its boxes', () => {
    // The compact label style is boxes of bare ordinals: it draws the shape and
    // names nothing, so the rows are still the only place a node is identified.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={40} />)

    expect(f).toContain('\u256d')
    expect(f).toContain('parse_a')
  })

  it('keeps sibling boxes adjacent and their join below them', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={24} />)
    // parse_a is running, so its own stream line sits between the two rows;
    // filtered out here since it names no node and would throw off the count.
    const lines = f
      .split('\n')
      .map(line => line.trim())
      .filter(line => !line.includes('working'))
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
  const rowFrame = (run: DagRunState) => frame(<DagPanel run={run} t={DEFAULT_THEME} width={12} />)

  it('reads a node as the agent and what it was asked', () => {
    // Columns, not a sentence: the agent is padded to the run's widest name so
    // the summaries all start on one offset a reader can scan down.
    expect(rowFrame(WITH_PROMPTS)).toContain('Coder  Inspect the i18n messag')
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
      .find(line => line.includes('Inspect the i18n'))!

    // The same margin the transcript's reply marker and the reasoning rule take.
    // The mark itself is a spinner frame while the node runs, so this asserts
    // the columns after it rather than the glyph.
    expect(row.slice(1)).toMatch(/^ 1 {2}Coder/)
  })

  it('keeps the node id off the collapsed row', () => {
    // The id is machine-generated and the widest thing on the row; what the node
    // was asked is what a reader is scanning for. The id is one click away.
    expect(rowFrame(WITH_PROMPTS)).not.toContain('inspect_i18n_messages_20260820')
  })

  it('falls back to the node id when no prompt reached the client', () => {
    // An older run dir writes no template and a host that does not correlate
    // progress with a tool row supplies no call args. A row named after nothing
    // would be worse than a row named after its id.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={40} />)

    expect(f).toContain('fetch')
    expect(f).not.toContain('echo: ')
  })

  it('clips the summary so a long prompt cannot overrun the row', () => {
    const run: DagRunState = {
      runId: 'dag-3',
      done: false,
      nodes: [{ ...node('a', 'running', [], 'a-name-too-long-for-a-box'), promptTemplate: 'x'.repeat(400) }]
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
    const f = frame(<DagPanel run={run} t={DEFAULT_THEME} />)

    // Columns, not `agent: summary` -- the agent is padded to the run's widest
    // name so every summary starts on one offset (see the row-shape test above).
    expect(f).toContain('Coder  audit the skills directory')
    expect(f).not.toContain('Do something else entirely')
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
  it('draws a stream line under a running node without a click', () => {
    // The slot hangs off a node's row, so only the layout that draws rows has
    // one: at 40 columns the picture goes compact and the rows stay.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={40} />)

    expect(f).toContain('working…')
  })

  it('draws the stream line under a labelled picture too', () => {
    // The rows were dropped under a labelled picture for a while, and this line
    // went with them. They are back, because a box has no room for the summary a
    // node was dispatched with -- so a running node has a row to hang its live
    // line off again at every width.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('working…')
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
