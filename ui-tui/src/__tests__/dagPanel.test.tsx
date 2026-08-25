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

  it('draws the topology as a graph above the rows', () => {
    const lines = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />).split('\n')

    expect(lines.some(line => line.includes('\u256d') && line.includes('\u256e'))).toBe(true)
    expect(lines.findIndex(line => line.includes('\u25b8'))).toBeLessThan(
      lines.findIndex(line => line.includes('parse_a'))
    )
  })

  it('numbers the rows so a box can be matched to one', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toMatch(/1 .*fetch/)
    expect(f).toMatch(/4 .*report/)
  })

  it('stops naming a dependency the picture drew', () => {
    // Saying it twice is the state this graph replaced.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    // Paired with the positives: a panel that rendered nothing at all would
    // satisfy the absence on its own.
    expect(f).toContain('\u256d')
    expect(f).toContain('report')
    expect(f).not.toContain('parse_a, parse_b')
  })

  it('still names a dependency from an earlier run, which has no box', () => {
    const run: DagRunState = {
      runId: 'dag-x',
      done: false,
      nodes: [node('only', 'pending', ['from_an_earlier_run'])]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('from_an_earlier_run')
  })

  it('drops the picture rather than overflow a narrow terminal', () => {
    // Boxes plus gutters need more cells than this, in either label style, so the
    // rows carry the topology in words instead.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={24} />)

    expect(f).not.toContain('\u256d')
    expect(f).toContain('parse_a, parse_b')
  })

  it('keeps sibling rows adjacent and their join below them', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)
    // parse_a is running, so its own stream line adds an extra line between the
    // two rows; filtered out here since it names no node and would throw off
    // the count.
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

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('author')
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

  it('reads a node as the agent and what it was asked', () => {
    const f = frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)

    expect(f).toContain('Coder: Inspect the i18n messages for dag.* keys')
  })

  it('names the agent and its instance handle in parentheses', () => {
    const f = frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)

    expect(f).toContain('(Coder@inspect-i18n-messages-20260820-1a4a53)')
  })

  it('keeps the node id off the collapsed row', () => {
    // The id is machine-generated and the widest thing on the row; what the node
    // was asked is what a reader is scanning for. The id is one click away.
    const f = frame(<DagPanel run={WITH_PROMPTS} t={DEFAULT_THEME} />)

    expect(f).not.toContain('inspect_i18n_messages_20260820')
  })

  it('falls back to the node id when no prompt reached the client', () => {
    // An older run dir writes no template and a host that does not correlate
    // progress with a tool row supplies no call args. A row named after nothing
    // would be worse than a row named after its id.
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('fetch')
    expect(f).not.toContain('echo: ')
  })

  it('clips the summary so a long prompt cannot overrun the row', () => {
    const run: DagRunState = {
      runId: 'dag-3',
      done: false,
      nodes: [{ ...node('a', 'running', [], 'Coder'), promptTemplate: 'x'.repeat(400) }]
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

    expect(f).toContain('Coder: audit the skills directory')
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

  it('has nothing to expand for a pending node with no template', () => {
    // Such a node has neither a template nor a trace yet, so an expanded block
    // would add nothing -- the row is left unclickable rather than swallowing
    // a click. A node that has started is a different story: see the node-slot
    // tests below.
    // No running node in this fixture: one's stream line carries a spinner that
    // picks a new random frame on every mount, which would make two renders of
    // it differ for a reason that has nothing to do with this toggle.
    const run: DagRunState = { ...DIAMOND, nodes: [node('later', 'pending')] }
    const closed = frame(<DagPanel run={run} t={DEFAULT_THEME} />)

    toggleDagNode(dagNodeKey(DIAMOND.runId, 'later'))

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toBe(closed)
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

  it('opens nothing for a node with no template, rather than swallowing the click', () => {
    // Such a box has nothing to reveal; a dead affordance that eats a click is
    // worse than no affordance.
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'b', text: '2 \u25cb Coder' }, nodes)).toBeNull()
  })

  it('opens nothing for a node absent from the run', () => {
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'ghost', text: 'x' }, nodes)).toBeNull()
  })
})

describe('DagPanel node slot', () => {
  it('draws a stream line under a running node without a click', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('working…')
  })

  it('expands a node that has no prompt template', () => {
    // Before the trace existed, the template was the only thing a click could
    // reveal, so a node without one was not clickable. There is a trace now.
    const bare: DagRunState = { ...DIAMOND, nodes: [node('solo', 'completed')] }

    expect(dagNodeToggleKey('r1', bare.nodes[0]!)).toBe(dagNodeKey('r1', 'solo'))
  })

  it('still refuses a pending node with nothing to show', () => {
    expect(dagNodeToggleKey('r1', node('later', 'pending'))).toBeNull()
  })

  it('expands a pending node that does carry a template', () => {
    expect(dagNodeToggleKey('r1', { ...node('later', 'pending'), promptTemplate: 'do it' })).not.toBeNull()
  })
})
