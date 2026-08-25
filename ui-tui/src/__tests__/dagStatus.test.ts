// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail } from '../rpc/index.js'

import { DAG_STATUS_GLYPH, dagNodeNames, dagNodeSummary, dagRunHeadline, formatDagNodeDetail } from '../lib/dagStatus.js'

// Derived from DAG_STATUS_GLYPH's own keys, not hand-copied: its `Record<DagRunNodeStatus, …>`
// type already forces that object to have exactly one entry per status, so this can never
// drift out of sync with a status the type gains.
const ALL_STATUSES = Object.keys(DAG_STATUS_GLYPH) as DagRunNodeStatus[]

const run = (over: Partial<DagRunState> = {}): DagRunState => ({
  runId: 'dag-1',
  done: false,
  nodes: [],
  ...over
})

const node = (id: string, status: DagRunNodeStatus) => ({
  id,
  subagent: 'echo',
  dependsOn: [],
  status
})

describe('DAG_STATUS_GLYPH', () => {
  it('covers every status a node can hold', () => {
    // A Record over the union rather than a lookup with a fallback, so adding a
    // status without a glyph is a type error instead of a blank column.
    ALL_STATUSES.forEach(status => {
      expect(DAG_STATUS_GLYPH[status].glyph).toBeTruthy()
    })
  })

  it('gives failed and skipped distinct glyphs', () => {
    // They are different outcomes -- the node errored vs a dependency did -- and
    // conflating them hides which node actually broke.
    expect(DAG_STATUS_GLYPH.failed.glyph).not.toBe(DAG_STATUS_GLYPH.skipped.glyph)
  })
})

describe('dagRunHeadline', () => {
  it('counts progress while the run is live', () => {
    const line = dagRunHeadline(run({ nodes: [node('a', 'completed'), node('b', 'running'), node('c', 'pending')] }))

    expect(line).toBe('3 nodes · 1 done · 1 running')
  })

  it('reports the tally from the manifest once the run is done', () => {
    const line = dagRunHeadline(
      run({
        done: true,
        summary: { total: 3, completed: 2, failed: 1, skipped: 0 },
        nodes: [node('a', 'completed'), node('b', 'completed'), node('c', 'failed')]
      })
    )

    expect(line).toBe('3 nodes · 2 done · 1 failed')
  })

  it('names skipped nodes so a cascade is visible', () => {
    const line = dagRunHeadline(
      run({
        done: true,
        summary: { total: 3, completed: 1, failed: 1, skipped: 1 },
        nodes: [node('a', 'completed'), node('b', 'failed'), node('c', 'skipped')]
      })
    )

    expect(line).toBe('3 nodes · 1 done · 1 failed · 1 skipped')
  })

  it('falls back to the node list when a finished run carries no summary', () => {
    const line = dagRunHeadline(run({ done: true, nodes: [node('a', 'completed')] }))

    expect(line).toBe('1 node · 1 done')
  })
})

describe('formatDagNodeDetail', () => {
  const detail = (over: Partial<DagNodeDetail> = {}): DagNodeDetail => ({
    run_id: 'dag-1',
    node: 'summarize',
    prompt: 'Summarise the two reports.',
    output: 'Here is the summary.',
    output_chars: 20,
    output_truncated: false,
    ...over
  })

  it('names the node and its run', () => {
    const text = formatDagNodeDetail(detail())

    expect(text).toContain('summarize')
    expect(text).toContain('dag-1')
  })

  it('shows the rendered prompt and the output', () => {
    // The rendered prompt is what the sub-agent actually received (placeholders
    // already substituted), which is the whole reason to look at a node.
    const text = formatDagNodeDetail(detail())

    expect(text).toContain('Summarise the two reports.')
    expect(text).toContain('Here is the summary.')
  })

  it('says when the output was cut short', () => {
    const text = formatDagNodeDetail(detail({ output_chars: 98_000, output_truncated: true }))

    expect(text).toContain('truncated')
    expect(text).toContain('98000')
  })

  it('says so when a node produced no output', () => {
    // A node that never ran has no prompt file and a failed one no output;
    // printing an empty section reads as "it returned nothing".
    const text = formatDagNodeDetail(detail({ output: undefined, output_chars: 0 }))

    expect(text).toContain('no output')
  })

  it('says so when a node never ran', () => {
    const text = formatDagNodeDetail(detail({ prompt: undefined }))

    expect(text).toContain('no prompt')
  })
})

describe('dagNodeSummary', () => {
  it('prefers the summary the node was dispatched with', () => {
    expect(dagNodeSummary('read the pricing pages', '# Heading\nsomething else', 80)).toBe('read the pricing pages')
  })

  it('falls back to the template heuristic for a run that carried no summary', () => {
    expect(dagNodeSummary(undefined, 'read the pricing pages\nmore detail', 80)).toBe('read the pricing pages')
  })

  it('clips a long summary to the room it is given', () => {
    const summary = dagNodeSummary('w'.repeat(400), undefined, 20)

    expect(summary).toHaveLength(20)
    expect(summary.endsWith('…')).toBe(true)
  })

  it('reads a node as the first line of what it was asked', () => {
    // A DAG prompt opens with its instruction and elaborates below, so the
    // opening line is the query and everything under it is detail.
    const summary = dagNodeSummary(undefined, 'Inspect the i18n messages for dag.* keys\n\nCheck both locales.', 80)

    expect(summary).toBe('Inspect the i18n messages for dag.* keys')
  })

  it('skips blank lines before the instruction', () => {
    expect(dagNodeSummary(undefined, '\n\n  Summarise the bridge files\n', 80)).toBe('Summarise the bridge files')
  })

  it('drops markdown furniture the row has no room to spend on', () => {
    expect(dagNodeSummary(undefined, '## Task: audit the skills\nbody', 80)).toBe('Task: audit the skills')
    expect(dagNodeSummary(undefined, '- inspect the panel\nbody', 80)).toBe('inspect the panel')
    expect(dagNodeSummary(undefined, '1. inspect the panel', 80)).toBe('inspect the panel')
    expect(dagNodeSummary(undefined, '> inspect the panel', 80)).toBe('inspect the panel')
  })

  it('looks past a line that is furniture and nothing else', () => {
    // A template opening with a bare `#` heading marker would otherwise summarise
    // to the empty string and leave the row anonymous.
    expect(dagNodeSummary(undefined, '##\nthe real instruction', 80)).toBe('the real instruction')
  })

  it('looks past a bare section label to the request under it', () => {
    // `## Task` names where the request is, never what it is. A row showing it
    // says nothing about the node at all.
    expect(dagNodeSummary(undefined, '## Task\nList the bridge files this change touches', 80)).toBe(
      'List the bridge files this change touches'
    )
    expect(dagNodeSummary(undefined, 'Context:\nread the panel', 80)).toBe('read the panel')
  })

  it('keeps a heading that states the request rather than naming a section', () => {
    expect(dagNodeSummary(undefined, '## Task: audit the skills\nbody', 80)).toBe('Task: audit the skills')
  })

  it("collapses a placeholder, which would spend the row on a dependency's name", () => {
    // The row already spells its dependencies out at the end, so the node name
    // inside the placeholder is redundant -- and 40 cells of it crowds out the
    // words that say what the node actually does.
    expect(dagNodeSummary(undefined, 'Summarise {{ fetch_the_upstream_thing.output }} into a review note', 80)).toBe(
      'Summarise \u2026 into a review note'
    )
    expect(dagNodeSummary(undefined, 'Read {{ ref:docs/spec.md }} closely', 80)).toBe('Read \u2026 closely')
  })

  it('looks past a line that is only a placeholder', () => {
    expect(dagNodeSummary(undefined, '{{ fetch.output }}\nnow rank them', 80)).toBe('now rank them')
  })

  it('clips to the room the row has left', () => {
    const summary = dagNodeSummary(undefined, 'inspect every single one of the i18n message keys', 20)

    expect(summary).toHaveLength(20)
    expect(summary.endsWith('…')).toBe(true)
  })

  it('is empty when neither the summary nor the template reached the client', () => {
    // The row then falls back to the node id rather than rendering "agent: ".
    expect(dagNodeSummary(undefined, undefined, 80)).toBe('')
    expect(dagNodeSummary(undefined, '   \n\n  ', 80)).toBe('')
  })
})

describe('formatDagNodeDetail trace', () => {
  const base = {
    node: 'n1',
    output_chars: 3,
    output_truncated: false,
    run_id: 'r1'
  }

  it('prints the steps between the prompt and the output', () => {
    const text = formatDagNodeDetail({
      ...base,
      messages: [
        { role: 'assistant', text: 'looking', tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }] },
        { role: 'tool', text: '12 lines', tool_call_id: 'c' }
      ],
      output: 'ok',
      prompt: 'do it'
    })

    expect(text).toContain('do it')
    expect(text).toContain('looking')
    expect(text).toContain('a.ts')
    expect(text).toContain('12 lines')
    expect(text).toContain('ok')
  })

  it('prints what it printed before when there are no messages', () => {
    const text = formatDagNodeDetail({ ...base, messages: [], output: 'ok', prompt: 'do it' })

    expect(text).toContain('prompt:')
    expect(text).toContain('output:')
    expect(text).not.toContain('trace:')
  })

  it('does not echo the prompt or the output a second time from the trace', () => {
    // The real wire shape: the server brackets the transcript with the same
    // prompt and output text `detail.prompt`/`detail.output` already carry
    // (`_with_messages` in raven/rpc/methods/dag.py), so a naive trace print
    // would show each of them twice.
    const text = formatDagNodeDetail({
      ...base,
      messages: [
        { role: 'user', text: 'do it' },
        { role: 'assistant', text: 'reading the file', tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }] },
        { role: 'tool', text: '12 lines', tool_call_id: 'c' },
        { role: 'assistant', text: 'done' }
      ],
      output: 'done',
      prompt: 'do it'
    })
    const occurrences = (needle: string) => text.split(needle).length - 1

    expect(occurrences('do it')).toBe(1)
    expect(occurrences('done')).toBe(1)
    expect(text).toContain('reading the file')
    expect(text).toContain('a.ts')
    expect(text).toContain('12 lines')
  })
})

describe('dagNodeNames', () => {
  const joined = { dependsOn: ['a', 'b'], id: 'c', subagent: 'raven-code' }

  it('drops the parenthetical when the node has no instance', () => {
    // The row already opens with the agent name and the picture's box carries it
    // a second time; a third copy is noise.
    expect(dagNodeNames(joined, null).parens).toBe('')
  })

  it('keeps the parenthetical when there is an instance to name', () => {
    expect(dagNodeNames({ ...joined, instance: 'sess1' }, null).parens).toBe('(raven-code@sess1)')
  })

  it('names every dependency when no picture was drawn', () => {
    expect(dagNodeNames(joined, null).deps).toContain('a, b')
  })

  it('names only the dependencies the picture could not draw', () => {
    // An edge to a node from an earlier run has no box, so the picture cannot
    // show it and the row is the only place it survives.
    expect(dagNodeNames(joined, new Set(['a>c'])).deps.trim()).toBe('\u2190 b')
  })

  it('says nothing when the picture drew every edge', () => {
    expect(dagNodeNames(joined, new Set(['a>c', 'b>c'])).deps).toBe('')
  })
})
