// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'
import type { TranscriptMessage } from '../rpc/index.js'

import { resetDagNodeTraces, setDagNodeTrace } from '../app/dagNodeStore.js'
import { DagNodeSlot } from '../components/dagNodeTrace.js'
import { DAG_TRACE_BOX_ROWS, DAG_TRACE_BOX_ROWS_SETTLED, DAG_TRACE_FIT_MAX_ROWS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const node = (status: DagRunNodeStatus, promptTemplate = 'audit the journal writer'): DagRunNode => ({
  dependsOn: [],
  id: 'n3f8a2',
  promptTemplate,
  status,
  subagent: 'codex-acp'
})

const slot = (over: { node: DagRunNode; open?: boolean }) =>
  stripAnsi(
    render(
      <DagNodeSlot node={over.node} open={over.open ?? false} ordinal={2} runId="r1" t={DEFAULT_THEME} width={64} />
    ).lastFrame() ?? ''
  )

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

// A real dag.node trace always opens with the prompt as a user row (see
// raven/rpc/methods/dag.py:_with_messages) before the subagent's own turns.
// foldRowsIntoEpisodes only closes a fold on a user/system row, so a fixture
// of assistant-only rows always folds to one message and can never leave
// anything "earlier" to report -- this stands the prompt row up so the fold
// produces more than one message, the way a real trace does.
const ask = (text: string): TranscriptMessage => ({ role: 'user', text })

// One tool round trip: the assistant's call, then its matching result -- the
// unit dag.py's wire messages repeat once per step a subagent takes.
const toolTurn = (id: string, text: string): TranscriptMessage[] => [
  { role: 'assistant', text, tool_calls: [{ id, name: 'read_file', arguments: `{"path":"file-${id}.ts"}` }] },
  { role: 'tool', text: `contents of file-${id}.ts`, tool_call_id: id }
]

beforeEach(() => {
  resetDagNodeTraces()
})

describe('DagNodeSlot', () => {
  it('draws nothing for a finished node that is not expanded', () => {
    expect(slot({ node: node('completed') }).trim()).toBe('')
  })

  it('draws nothing under a running node either, until it is opened', () => {
    // The live tail that used to sit here cost a row per running node and moved
    // several times a second; watching one node is what opening it is for.
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('the journal writes one frame per line')], false)

    expect(slot({ node: node('running') }).trim()).toBe('')
  })

  it('draws the trace in a box when expanded', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('the answer')], false)

    const frame = slot({ node: node('running'), open: true })

    expect(frame).toContain('the answer')
    expect(frame).toContain('n3f8a2')
  })

  it('gives a finished node the taller box, since nothing will redraw it', () => {
    // The box cannot scroll, so its height is the only thing deciding how much
    // of a finished trace is readable without `/dag`. A running node keeps the
    // short one: it is re-read twice a second, and a tall box redrawing that
    // often shoves everything under it.
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('the answer')], false)

    const running = slot({ node: node('running'), open: true }).split('\n').length
    const settled = slot({ node: node('completed'), open: true }).split('\n').length

    expect(settled).toBeGreaterThan(running)
  })

  it('never draws more rows than the box holds, whatever one message says', () => {
    // Overflow is not clipped by this fork: the column is squeezed instead,
    // which drops scattered lines and paints the last one over the footer.
    const huge = say(Array.from({ length: 80 }, (_unused, i) => `line ${i} of a long message`).join('\n'))
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [ask('go'), huge], true)

    const rows = slot({ node: node('completed'), open: true })
      .split('\n')
      .filter(Boolean)

    expect(rows).toHaveLength(DAG_TRACE_BOX_ROWS_SETTLED)
    // The tail, contiguous: the cut is at the head and says so.
    expect(rows.some(row => row.includes('line 79 of a long message'))).toBe(true)
    expect(rows.some(row => row.includes('earlier messages'))).toBe(true)
  })

  it('holds the box to its fixed height whatever the trace length', () => {
    const short = [say('one line')]
    const long = Array.from({ length: 60 }, (_unused, i) => say(`message number ${i}`))

    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), short, false)
    const shortRows = slot({ node: node('running'), open: true }).split('\n').length

    resetDagNodeTraces()
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), long, false)
    const longRows = slot({ node: node('running'), open: true }).split('\n').length

    expect(shortRows).toBe(DAG_TRACE_BOX_ROWS)
    expect(longRows).toBe(DAG_TRACE_BOX_ROWS)
  })

  it('says how many messages it could not show', () => {
    setDagNodeTrace(
      dagNodeKey('r1', 'n3f8a2'),
      [ask('audit the journal writer'), ...Array.from({ length: 60 }, (_unused, i) => say(`message number ${i}`))],
      false
    )

    expect(slot({ node: node('running'), open: true })).toContain('earlier')
  })

  it('points at /dag with the ordinal when nothing was cut', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('one line')], false)

    expect(slot({ node: node('running'), open: true })).toContain('/dag 2')
  })

  it('falls back to the prompt template when no trace was fetched', () => {
    expect(slot({ node: node('completed'), open: true })).toContain('audit the journal writer')
  })

  it('shows the node id in the fallback too, since /dag takes it', () => {
    expect(slot({ node: node('completed'), open: true })).toContain('n3f8a2')
  })

  it('shows the end of a trace, not its beginning', () => {
    const trace: TranscriptMessage[] = [
      ask('audit the journal writer'),
      ...toolTurn('c0', 'EARLY MARKER'),
      ...toolTurn('c1', 'looked at the second file'),
      ...toolTurn('c2', 'looked at the third file'),
      ...toolTurn('c3', 'looked at the fourth file'),
      ...toolTurn('c4', 'looked at the fifth file'),
      ...toolTurn('c5', 'looked at the sixth file'),
      say('THE ANSWER')
    ]

    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), trace, false)

    const frame = slot({ node: node('running'), open: true })

    expect(frame).toContain('THE ANSWER')
    expect(frame).not.toContain('EARLY MARKER')
  })

  it('renders a fixed box and an honest hidden count for a trace with no narration', () => {
    // Every tool round trip above gives its step distinct narration, which is
    // what keeps segmentTurn from collapsing the run. Here every step carries
    // only tool_calls and no text, so the whole run folds to one work segment
    // regardless of length -- the shape that let the old unbounded search
    // walk to the end of the transcript without ever overflowing.
    const trace: TranscriptMessage[] = [
      ask('audit the journal writer'),
      ...Array.from({ length: DAG_TRACE_FIT_MAX_ROWS }, (_unused, i) => toolTurn(`u${i}`, '')).flat()
    ]

    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), trace, false)

    const frame = slot({ node: node('running'), open: true })

    expect(frame.split('\n')).toHaveLength(DAG_TRACE_BOX_ROWS)
    expect(frame).toContain('earlier')
  })

  it('renders exactly the rows the height model budgets for a box', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('one line')], false)

    const rows = slot({ node: node('running'), open: true }).split('\n').length

    expect(rows).toBe(DAG_TRACE_BOX_ROWS)
  })
})
