// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { stringWidth } from '@hermes/ink'
import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeBranch } from '../lib/dagStatus.js'
import type { DagNodeDetail } from '../rpc/index.js'
import type { Theme } from '../theme.js'

import {
  DAG_STATUS_GLYPH,
  dagNodeDeps,
  dagNodeElapsed,
  dagNodeHandle,
  dagNodeOutcome,
  dagNodeSummary,
  dagRowDetail,
  dagRunElapsedMs,
  dagRunHeadline,
  dagRunTally,
  dagSharedInstances,
  formatDagNodeDetail,
  hasDagRowDetail
} from '../lib/dagStatus.js'
import { clipToWidth } from '../lib/text.js'

// Derived from DAG_STATUS_GLYPH's own keys, not hand-copied: its `Record<DagRunNodeStatus, …>`
// type already forces that object to have exactly one entry per status, so this can never
// drift out of sync with a status the type gains.
const ALL_STATUSES = Object.keys(DAG_STATUS_GLYPH) as DagRunNodeStatus[]

// What a node could be before branches existed: every status but the one they added.
const OLD_STATUSES = ALL_STATUSES.filter(status => status !== 'not_executed')

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

  it('gives a suspended node the warning colour, distinct from failed and pending', () => {
    // 'exception' means the node is waiting on a decision, not that it or a
    // dependency broke -- reusing either colour would misreport which nodes
    // still need attention.
    const theme = { color: { error: 'error', muted: 'muted', warn: 'warn' } } as Theme

    expect(DAG_STATUS_GLYPH.exception.color(theme)).toBe('warn')
    expect(DAG_STATUS_GLYPH.exception.glyph).not.toBe(DAG_STATUS_GLYPH.failed.glyph)
    expect(DAG_STATUS_GLYPH.exception.glyph).not.toBe(DAG_STATUS_GLYPH.pending.glyph)
  })

  it('styles every status the wire can send, included a node a branch left out', () => {
    expect(DAG_STATUS_GLYPH.not_executed).toEqual({ color: expect.any(Function), glyph: '◌' })
  })

  it('draws a node a branch left out muted, and apart from a skipped one', () => {
    // A skipped node is a dependency that broke; this one was never due to run.
    // Neither reds the row nor shares the other's mark.
    const theme = { color: { error: 'error', muted: 'muted', warn: 'warn' } } as Theme

    expect(DAG_STATUS_GLYPH.not_executed.color(theme)).toBe('muted')
    expect(DAG_STATUS_GLYPH.not_executed.glyph).not.toBe(DAG_STATUS_GLYPH.skipped.glyph)
  })
})

describe('dagRunHeadline', () => {
  it('counts progress while the run is live', () => {
    const line = dagRunHeadline(run({ nodes: [node('a', 'completed'), node('b', 'running'), node('c', 'pending')] }))

    expect(line).toBe('3 nodes · 1 done · 1 running')
  })

  it('surfaces a suspended node in the live headline', () => {
    const line = dagRunHeadline(run({ nodes: [node('a', 'completed'), node('b', 'exception'), node('c', 'pending')] }))

    expect(line).toBe('3 nodes · 1 done · 1 exception')
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

  it('names the nodes a branch left out, apart from the skipped ones', () => {
    // A cascade and an unselected branch are different stories: the first is a
    // failure to chase, the second is the graph working as written.
    const line = dagRunHeadline(
      run({
        done: true,
        summary: { total: 5, completed: 1, failed: 0, skipped: 1, not_executed: 2, cancelled: 1 },
        nodes: [
          node('a', 'completed'),
          node('b', 'skipped'),
          node('c', 'not_executed'),
          node('d', 'not_executed'),
          node('e', 'cancelled')
        ]
      })
    )

    expect(line).toBe('5 nodes · 1 done · 1 skipped · 2 not executed · 1 cancelled')
  })

  it('counts them off the nodes while the run is live, whatever a summary already says', () => {
    const line = dagRunHeadline(
      run({
        summary: { total: 4, completed: 1, not_executed: 3 },
        nodes: [node('a', 'completed'), node('b', 'not_executed'), node('c', 'not_executed'), node('d', 'pending')]
      })
    )

    expect(line).toBe('4 nodes · 1 done · 2 not executed')
  })

  it('takes their count from the manifest once the run is done', () => {
    // A frame the client missed leaves a node pending in its own list.
    const line = dagRunHeadline(
      run({
        done: true,
        summary: { total: 3, completed: 1, not_executed: 2 },
        nodes: [node('a', 'completed'), node('b', 'not_executed'), node('c', 'pending')]
      })
    )

    expect(line).toBe('3 nodes · 1 done · 2 not executed')
  })

  it('says nothing of them on a run no branch touched', () => {
    const line = dagRunHeadline(
      run({
        done: true,
        summary: { total: 2, completed: 2, failed: 0, skipped: 0 },
        nodes: [node('a', 'completed'), node('b', 'completed')]
      })
    )

    expect(line).toBe('2 nodes · 2 done')
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
        {
          role: 'assistant',
          text: 'looking',
          tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }]
        },
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
        {
          role: 'assistant',
          text: 'reading the file',
          tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }]
        },
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

describe('dagNodeDeps', () => {
  const joined = { dependsOn: ['a', 'b'], id: 'c', subagent: 'raven-code' }

  it('names every dependency when no picture was drawn', () => {
    expect(dagNodeDeps(joined, null)).toContain('a, b')
  })

  it('names only the dependencies the picture could not draw', () => {
    // An edge to a node from an earlier run has no box, so the picture cannot
    // show it and the row is the only place it survives.
    expect(dagNodeDeps(joined, new Set(['a>c'])).trim()).toBe('\u2190 b')
  })

  it('says nothing when the picture drew every edge', () => {
    expect(dagNodeDeps(joined, new Set(['a>c', 'b>c']))).toBe('')
  })
})

describe('dagNodeHandle', () => {
  it('is empty when the node ran on no stateful handle', () => {
    expect(dagNodeHandle({ subagent: 'raven-code' })).toBe('')
  })

  it('joins the agent to its instance for the expanded block', () => {
    expect(dagNodeHandle({ instance: 'sess1', subagent: 'raven-code' })).toBe('raven-code@sess1')
  })
})

describe('dagSharedInstances', () => {
  it('reports a handle two nodes ran on, which forced them into sequence', () => {
    expect([...dagSharedInstances([{ instance: 'author' }, { instance: 'author' }])]).toEqual(['author'])
  })

  it('ignores a handle only one node used, which constrains nothing', () => {
    expect(dagSharedInstances([{ instance: 'author' }, { instance: 'editor' }]).size).toBe(0)
  })

  it('ignores nodes with no handle at all', () => {
    expect(dagSharedInstances([{}, {}]).size).toBe(0)
  })
})

describe('dagRunTally', () => {
  it('counts by status, in one order, dropping the zeroes', () => {
    const tally = dagRunTally(
      run({ nodes: [node('a', 'completed'), node('b', 'running'), node('c', 'pending'), node('d', 'pending')] })
    )

    expect(tally).toEqual([
      { count: 1, status: 'completed' },
      { count: 1, status: 'running' },
      { count: 2, status: 'pending' }
    ])
  })

  it('takes a finished run at the manifest word, which is authoritative', () => {
    // The client can miss a node's terminal frame; the manifest cannot.
    const tally = dagRunTally(
      run({ done: true, nodes: [node('a', 'completed')], summary: { completed: 3, failed: 1, total: 4 } })
    )

    expect(tally).toEqual([
      { count: 3, status: 'completed' },
      { count: 1, status: 'failed' }
    ])
  })

  it('counts a not-executed node apart from a skipped one', () => {
    const tally = dagRunTally(
      run({
        done: true,
        nodes: [node('a', 'completed'), node('b', 'not_executed'), node('c', 'not_executed')],
        summary: { completed: 1, not_executed: 2, skipped: 0, total: 3 }
      })
    )

    expect(tally.map(t => t.status)).toEqual(['completed', 'not_executed'])
    expect(tally[1]!.count).toBe(2)
  })

  it('counts them off the nodes while the run is live, after skipped and before cancelled', () => {
    const tally = dagRunTally(
      run({
        summary: { not_executed: 7, total: 5 },
        nodes: [
          node('a', 'cancelled'),
          node('b', 'not_executed'),
          node('c', 'skipped'),
          node('d', 'not_executed'),
          node('e', 'completed')
        ]
      })
    )

    expect(tally.map(t => [t.status, t.count])).toEqual([
      ['completed', 1],
      ['skipped', 1],
      ['not_executed', 2],
      ['cancelled', 1]
    ])
  })

  it('takes their count at the manifest word once the run is done', () => {
    const tally = dagRunTally(
      run({ done: true, nodes: [node('a', 'not_executed')], summary: { completed: 1, not_executed: 3, total: 4 } })
    )

    expect(tally).toEqual([
      { count: 1, status: 'completed' },
      { count: 3, status: 'not_executed' }
    ])
  })
})

describe('dag elapsed', () => {
  const timed = (over: { endedAt?: number; startedAt?: number; status: DagRunNodeStatus }) => ({
    ...node('n', over.status),
    ...over
  })

  it('reads a finished node as what it took and a running one as what it has taken', () => {
    expect(dagNodeElapsed(timed({ endedAt: 5_000, startedAt: 2_000, status: 'completed' }), 9_000)).toBe('3s')
    expect(dagNodeElapsed(timed({ startedAt: 2_000, status: 'running' }), 9_000)).toBe('7s')
  })

  it('says what a node that has not started is waiting as', () => {
    expect(dagNodeElapsed(timed({ status: 'pending' }), 9_000)).toBe('queued')
  })

  it('says nothing for a node whose run dir recorded no timings', () => {
    // A zero would read as a node that did nothing, which is a different claim.
    expect(dagNodeElapsed(timed({ status: 'completed' }), 9_000)).toBe('')
  })

  it('says nothing for a node that never ran, whatever stamps the sweep invented for it', () => {
    expect(dagNodeElapsed(timed({ endedAt: 9_000, startedAt: 9_000, status: 'skipped' }), 10_000)).toBe('')
  })

  it("leaves a skipped node's invented stamps out of the run window", () => {
    const skippedLast = run({
      done: true,
      nodes: [
        { ...timed({ endedAt: 4_000, startedAt: 1_000, status: 'failed' }), id: 'a' },
        { ...timed({ endedAt: 9_000, startedAt: 9_000, status: 'skipped' }), id: 'b' }
      ]
    })

    expect(dagRunElapsedMs(skippedLast, 100_000)).toBe(3_000)
  })

  it('measures a run from its first start to its last end', () => {
    const finished = run({
      done: true,
      nodes: [
        { ...timed({ endedAt: 4_000, startedAt: 1_000, status: 'completed' }), id: 'a' },
        { ...timed({ endedAt: 9_000, startedAt: 4_000, status: 'completed' }), id: 'b' }
      ]
    })

    expect(dagRunElapsedMs(finished, 100_000)).toBe(8_000)
  })

  it('keeps a live run running against the clock, whatever has already ended', () => {
    const live = run({
      nodes: [
        { ...timed({ endedAt: 4_000, startedAt: 1_000, status: 'completed' }), id: 'a' },
        { ...timed({ startedAt: 4_000, status: 'running' }), id: 'b' }
      ]
    })

    expect(dagRunElapsedMs(live, 10_000)).toBe(9_000)
  })

  it('has nothing to report for a run whose nodes carry no timings', () => {
    expect(dagRunElapsedMs(run({ nodes: [node('a', 'pending')] }), 10_000)).toBeUndefined()
  })

  it('says nothing of how long a node a branch left out took', () => {
    // The sweep that decided against the node stamped it with the moment it
    // decided, and `dag.get` hands that stamp back. It never ran, so it has no
    // clock of its own -- and a made-up zero reads as a node that did nothing.
    expect(dagNodeElapsed(timed({ endedAt: 5_000, startedAt: 5_000, status: 'not_executed' }), 9_000)).toBe('')
    expect(dagNodeElapsed(timed({ endedAt: 8_000, startedAt: 2_000, status: 'not_executed' }), 9_000)).toBe('')
  })

  it('does not let a node a branch left out stretch the run past the nodes that ran', () => {
    // Stamped before the first start and after the last end, so either side of
    // the window would move if the node were counted.
    const finished = run({
      done: true,
      nodes: [
        { ...timed({ endedAt: 4_000, startedAt: 1_000, status: 'completed' }), id: 'a' },
        { ...timed({ endedAt: 90_000, startedAt: 500, status: 'not_executed' }), id: 'b' }
      ]
    })

    expect(dagRunElapsedMs(finished, 100_000)).toBe(3_000)
  })

  it('has no clock for a run in which nothing ran', () => {
    const none = run({
      done: true,
      nodes: [{ ...timed({ endedAt: 5_000, startedAt: 5_000, status: 'not_executed' }), id: 'a' }]
    })

    expect(dagRunElapsedMs(none, 10_000)).toBeUndefined()
  })
})

describe('dagNodeOutcome', () => {
  it.each([
    ['when_false', 'not selected'],
    ['when_unknown', 'decision missing'],
    ['upstream_not_executed', 'upstream not executed']
  ] as const)('says why a node was left out: %s reads %s', (reason, words) => {
    expect(dagNodeOutcome({ reason, status: 'not_executed' })).toBe(words)
  })

  it('says only that a node was not executed when no reason reached the client', () => {
    // A run reloaded before it finished can carry a not-executed node with no
    // reason: the registry write that carries one is best-effort and can be
    // dropped. Say what is known rather than invent a cause.
    expect(dagNodeOutcome({ status: 'not_executed' })).toBe('not executed')
  })

  it('reads a reason this client has no words for as not executed', () => {
    expect(dagNodeOutcome({ reason: 'a_reason_from_a_newer_server' as never, status: 'not_executed' })).toBe(
      'not executed'
    )
  })

  it.each(['constructor', '__proto__', 'toString'])('does not resolve %s through the prototype chain', reason => {
    // A reason is a server's word, so it is untrusted here: an inherited member
    // looked up by bracket would print a function's source into the detail line.
    const outcome = dagNodeOutcome({ reason: reason as never, status: 'not_executed' })

    expect(outcome).toBe('not executed')
    expect(typeof outcome).toBe('string')
  })

  it('shows what a decision node selected, after its status', () => {
    expect(dagNodeOutcome({ selectedRoutes: ['question'], status: 'completed' })).toBe('→ question')
    expect(dagNodeOutcome({ selectedRoutes: ['docs', 'bug'], status: 'completed' })).toBe('→ docs, bug')
  })

  it('shows an empty selection as a selection of nothing, not as no selection', () => {
    // A `select: many` decision may choose nothing, and that is an answer.
    expect(dagNodeOutcome({ selectedRoutes: [], status: 'completed' })).toBe('→ (none)')
  })

  it('shows nothing for a node that decided nothing, or has not decided yet', () => {
    expect(dagNodeOutcome({ status: 'completed' })).toBe('')
    expect(dagNodeOutcome({ status: 'running' })).toBe('')
  })

  it('shows nothing for a plain node, whatever its status', () => {
    OLD_STATUSES.forEach(status => expect(dagNodeOutcome({ status })).toBe(''))
  })
})

describe('dagRowDetail on a branching run', () => {
  const ROOM = 120
  const row = (over: Partial<DagRunNode> = {}, room = ROOM, drawn: ReadonlySet<string> | null = new Set()) =>
    dagRowDetail({ dependsOn: [], id: 'n', status: 'completed', ...over }, drawn, room)

  it('opens a not-executed node with its reason', () => {
    expect(row({ reason: 'when_false', status: 'not_executed' }).outcome).toBe('not selected')
  })

  it('opens a decision node with its selection', () => {
    expect(row({ selectedRoutes: ['question'] }).outcome).toBe('→ question')
    expect(row({ selectedRoutes: [] }).outcome).toBe('→ (none)')
  })

  it('shows the guard in the words the graph was written with', () => {
    expect(row({ when: 'triage.bug and not risk.high' }).guard).toBe('when triage.bug and not risk.high')
  })

  it('shows a guard of blanks as no guard', () => {
    expect(row({ when: '   ' }).guard).toBe('')
  })

  it('quotes the route evidence, which is the judge quoting the node', () => {
    expect(row({ routeEvidence: 'it crashes on startup' }).evidence).toBe('"it crashes on startup"')
  })

  it('folds a long evidence to one line, ending in an ellipsis', () => {
    const evidence = row({ routeEvidence: `line one\nline two ${'w'.repeat(300)}` }, 40).evidence

    expect(evidence.startsWith('"line one line two')).toBe(true)
    expect(evidence.endsWith('…"')).toBe(true)
    expect(stringWidth(evidence)).toBeLessThanOrEqual(40)
  })

  it('leaves a node with none of the branch fields exactly as plain as before', () => {
    const plain = row({ nodeSummary: 'read the pricing pages' })

    expect([plain.outcome, plain.guard, plain.evidence]).toEqual(['', '', ''])
    expect(plain.summary).toBe('read the pricing pages')
  })

  it('counts any one part as a line, so the panel and the height model agree it exists', () => {
    expect(hasDagRowDetail(row())).toBe(false)
    expect(hasDagRowDetail(row({ reason: 'when_false', status: 'not_executed' }))).toBe(true)
    expect(hasDagRowDetail(row({ selectedRoutes: [] }))).toBe(true)
    expect(hasDagRowDetail(row({ when: 'a.b' }))).toBe(true)
    expect(hasDagRowDetail(row({ routeEvidence: 'because' }))).toBe(true)
    expect(hasDagRowDetail(row({ nodeSummary: 'say something' }))).toBe(true)
    expect(hasDagRowDetail(row({ error: 'boom' }))).toBe(true)
    expect(hasDagRowDetail(row({ dependsOn: ['x'] }, ROOM, null))).toBe(true)
  })

  it('gives the room to the parts in the order a reader needs them', () => {
    const left: Partial<DagRunNode> = {
      dependsOn: ['triage'],
      nodeSummary: 'Fix the defect in the parser',
      reason: 'when_false',
      status: 'not_executed',
      when: 'triage.bug'
    }

    // Wide: every part, the summary taking what the others leave.
    const wide = row(left, 60, null)

    expect(wide.outcome).toBe('not selected')
    expect(wide.deps).toBe('← triage')
    expect(wide.guard).toBe('when triage.bug')
    expect(wide.summary.startsWith('Fix the defect')).toBe(true)

    // Narrow: the reason and what the picture cannot draw survive; the guard
    // and the summary are what gives.
    const narrow = row(left, 24, null)

    expect(narrow.outcome).toBe('not selected')
    expect(narrow.deps).toBe('← triage')
    expect([narrow.guard, narrow.summary]).toEqual(['', ''])
  })

  it('lets an error take the room before anything else', () => {
    const failed = row({ error: 'E'.repeat(60), reason: 'when_false', status: 'not_executed' }, 40)

    expect(stringWidth(failed.error)).toBeLessThanOrEqual(40)
    expect(failed.outcome).toBe('')
  })

  it('lets what a decision node concluded outrank what it was asked', () => {
    const decided = row(
      {
        nodeSummary: 'Classify the issue',
        routeEvidence: 'the issue asks for behaviour the product does not have yet',
        selectedRoutes: ['feature']
      },
      50
    )

    expect(decided.outcome).toBe('→ feature')
    expect(decided.evidence.startsWith('"the issue asks')).toBe(true)
    expect(decided.summary).toBe('')
  })

  const BRANCH_PARTS: [string, Partial<DagRunNode>][] = [
    ['a reason', { reason: 'when_false', status: 'not_executed' }],
    ['a guard', { when: 'triage.bug and not triage.docs' }],
    ['a selection', { routeEvidence: 'the reporter describes a crash on startup', selectedRoutes: ['bug'] }],
    ['all of them', { reason: 'when_false', routeEvidence: 'a quote', status: 'not_executed', when: 'triage.bug' }]
  ]

  it.each(BRANCH_PARTS)(
    'keeps a line with %s and an error inside its room, the gap before the error included',
    (_what, part) => {
      const node: Partial<DagRunNode> = {
        dependsOn: ['triage'],
        error: 'CLI agent exited 1 while writing the third file',
        nodeSummary: 'Fix the defect in the parser and add a regression test for it',
        ...part
      }

      // From the narrowest room the panel can hand a row: 28 columns, less its frame, the
      // mark and the ordinal.
      for (let room = 17; room <= 160; room++) {
        const parts = Object.values(row(node, room, null)).filter(Boolean)
        const used = parts.reduce((sum, text) => sum + stringWidth(text), 0) + 2 * Math.max(0, parts.length - 1)

        expect(used, `room ${room}`).toBeLessThanOrEqual(room)
      }
    }
  )
})

describe('dagRowDetail on a run no branch touched', () => {
  type PlainNode = Pick<DagRunNode, 'dependsOn' | 'error' | 'id' | 'nodeSummary' | 'promptTemplate'>

  // The arithmetic as it stood before branches existed, kept verbatim: a node
  // with none of the branch fields has to get exactly these three parts, down
  // to the cell, or every ordinary run's panel moves.
  const before = (node: PlainNode, drawn: ReadonlySet<string> | null, room: number) => {
    const error = node.error ? clipToWidth(node.error, Math.max(0, room)) : ''
    const afterError = room - (error ? stringWidth(error) : 0)
    const wanted = dagNodeDeps(node, drawn).trim()
    const deps = wanted && afterError > 4 ? clipToWidth(wanted, afterError - 2) : ''
    const left = afterError - (deps ? stringWidth(deps) + 2 : 0)

    return { deps, error, summary: left > 4 ? dagNodeSummary(node.nodeSummary, node.promptTemplate, left) : '' }
  }

  const WIDE = String.fromCodePoint(0x4e2d, 0x6587).repeat(14)

  const NODES: PlainNode[] = [
    { dependsOn: [], id: 'bare' },
    { dependsOn: [], id: 'summarised', nodeSummary: 'read the pricing pages and write them up' },
    { dependsOn: [], id: 'wide', nodeSummary: WIDE },
    { dependsOn: [], id: 'templated', promptTemplate: '## Task: audit the skills\nthen report' },
    { dependsOn: [], error: 'CLI agent exited 1', id: 'failed' },
    { dependsOn: ['x', 'from_an_earlier_run'], id: 'joined' },
    { dependsOn: ['x'], error: 'CLI agent exited 1', id: 'everything', nodeSummary: 'read the pricing pages' },
    { dependsOn: [], error: 'E'.repeat(200), id: 'long_error', nodeSummary: 'read the pricing pages' }
  ]

  const ROOMS = [-3, 0, 1, 4, 5, 6, 8, 12, 20, 33, 60, 91, 200]

  it.each(NODES.map(node => [node.id, node] as const))(
    'gives %s the parts it always got, at every width and in every status',
    (_id, node) => {
      for (const status of OLD_STATUSES) {
        for (const drawn of [null, new Set<string>(), new Set(['x>' + node.id])]) {
          for (const room of ROOMS) {
            const got = dagRowDetail({ ...node, status }, drawn, room)

            expect({ deps: got.deps, error: got.error, summary: got.summary }, `${status} in room ${room}`).toEqual(
              before(node, drawn, room)
            )
            expect([got.outcome, got.guard, got.evidence]).toEqual(['', '', ''])
          }
        }
      }
    }
  )
})

describe('formatDagNodeDetail on a branching run', () => {
  const detail = (over: Partial<DagNodeDetail> = {}): DagNodeDetail => ({
    run_id: 'dag-1',
    node: 'summarize',
    prompt: 'Summarise the two reports.',
    output: 'Here is the summary.',
    output_chars: 20,
    output_truncated: false,
    ...over
  })

  const page = (branch?: DagNodeBranch, over: Partial<DagNodeDetail> = {}) =>
    formatDagNodeDetail(detail(over), branch).split('\n')

  // The longest quote a judge may give: the panel's one line has to clip it, the
  // page does not.
  const EVIDENCE = `${'word '.repeat(99)}words`

  it('pages a node no branch touched exactly as it always did', () => {
    // Captured from the formatter before branches existed.
    const before: [Partial<DagNodeDetail>, string][] = [
      [{}, '── summarize @ dag-1 (20 chars) ──\nprompt:\nSummarise the two reports.\noutput:\nHere is the summary.'],
      [
        { output_chars: 98_000, output_truncated: true },
        '── summarize @ dag-1 (98000 chars, truncated) ──\nprompt:\nSummarise the two reports.\noutput:\nHere is the summary.'
      ],
      [
        { output: undefined, output_chars: 0, prompt: undefined },
        '── summarize @ dag-1 ──\nprompt:\n  (no prompt — the node never ran)\noutput:\n  (no output)'
      ],
      [
        {
          messages: [
            {
              role: 'assistant',
              text: 'looking',
              tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }]
            },
            { role: 'tool', text: '12 lines', tool_call_id: 'c' }
          ]
        },
        '── summarize @ dag-1 (20 chars) ──\nprompt:\nSummarise the two reports.\ntrace:\n  looking\n  > Read File("a.ts")\n    12 lines\noutput:\nHere is the summary.'
      ]
    ]

    for (const [over, expected] of before) {
      expect(formatDagNodeDetail(detail(over))).toBe(expected)
      expect(formatDagNodeDetail(detail(over), undefined)).toBe(expected)

      for (const status of OLD_STATUSES) {
        expect(formatDagNodeDetail(detail(over), { status }), status).toBe(expected)
      }
    }
  })

  it('opens a decision node with what it selected and the evidence in full', () => {
    const out = page({ routeEvidence: EVIDENCE, selectedRoutes: ['docs'], status: 'completed' }, { node: 'triage' })

    expect(EVIDENCE).toHaveLength(500)
    expect(out[0]).toBe('── triage @ dag-1 (20 chars) ──')
    expect(out[1]).toBe('selected: docs')
    expect(out[2]).toBe(`evidence: "${EVIDENCE}"`)
    expect(out[3]).toBe('prompt:')
  })

  it('names every route a decision node selected', () => {
    expect(page({ selectedRoutes: ['docs', 'bug'], status: 'completed' })[1]).toBe('selected: docs, bug')
  })

  it('says an empty selection selected nothing, and still gives its evidence', () => {
    const out = page({ routeEvidence: 'no route applies', selectedRoutes: [], status: 'completed' })

    expect(out.slice(1, 3)).toEqual(['selected: (none)', 'evidence: "no route applies"'])
  })

  it('says nothing of a selection a node has not made', () => {
    expect(page({ status: 'running' }).join('\n')).not.toContain('selected')
  })

  it("shows a guarded node's guard", () => {
    expect(page({ status: 'completed', when: 'triage.bug and not triage.docs' })[1]).toBe(
      'when triage.bug and not triage.docs'
    )
  })

  it('takes blanks for no guard and no evidence', () => {
    const plain = formatDagNodeDetail(detail())

    expect(formatDagNodeDetail(detail(), { routeEvidence: '  \n ', status: 'completed', when: '   ' })).toBe(plain)
  })

  it.each([
    ['when_false', 'not executed: not selected'],
    ['when_unknown', 'not executed: decision missing'],
    ['upstream_not_executed', 'not executed: upstream not executed']
  ] as const)('says why a node was left out: %s', (reason, line) => {
    expect(page({ reason, status: 'not_executed' })[1]).toBe(line)
  })

  it('says only that a node was not executed when no reason reached the client', () => {
    // A run reloaded before it finished reads reasons off registry rows, and a write there can be dropped.
    expect(page({ status: 'not_executed' })[1]).toBe('not executed')
    expect(page({ reason: 'made_up' as never, status: 'not_executed' })[1]).toBe('not executed')
  })

  it('puts the guard first, then what a branch did, then the evidence, all under the title', () => {
    const out = page({
      reason: 'when_false',
      routeEvidence: 'a quote',
      status: 'not_executed',
      when: 'triage.bug'
    })

    expect(out.slice(0, 5)).toEqual([
      '── summarize @ dag-1 (20 chars) ──',
      'when triage.bug',
      'not executed: not selected',
      'evidence: "a quote"',
      'prompt:'
    ])
  })

  it('only adds lines: the rest of the page is as it was', () => {
    const out = page({ routeEvidence: EVIDENCE, selectedRoutes: ['docs'], status: 'completed', when: 'triage.docs' })
    const before = formatDagNodeDetail(detail()).split('\n')

    expect(out).toHaveLength(before.length + 3)
    expect([out[0], ...out.slice(4)]).toEqual(before)
  })
})
