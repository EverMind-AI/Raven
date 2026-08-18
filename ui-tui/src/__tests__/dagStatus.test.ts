// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail } from '../rpc/index.js'

import { DAG_STATUS_GLYPH, dagRunHeadline, formatDagNodeDetail } from '../lib/dagStatus.js'

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
