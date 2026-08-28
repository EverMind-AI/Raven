// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// How a DAG node and a run's progress read on screen. Split from the panel so
// both are testable without a render, and so the transcript's folded row and the
// expanded graph can never disagree about a run's tally.

import { stringWidth } from '@hermes/ink'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail } from '../rpc/index.js'
import type { Theme } from '../theme.js'

import {
  DAG_TRACE_BOX_ROWS,
  DAG_TRACE_BOX_ROWS_SETTLED,
  DAG_TRACE_ROWS,
  DAG_TRACE_ROWS_SETTLED
} from '../config/limits.js'
import { callSubject } from '../domain/episodeFold.js'
import { fmtDuration } from '../domain/messages.js'
import { clipToWidth, compactPreview, formatToolCall } from './text.js'

/** Glyph + tone per status. A `Record` over the union rather than a lookup with
 * a fallback, so adding a status without styling it is a type error instead of a
 * blank column. Mirrors `agentsOverlay`'s table so the two read alike. */
export const DAG_STATUS_GLYPH: Record<DagRunNodeStatus, { color: (t: Theme) => string; glyph: string }> = {
  pending: { color: t => t.color.muted, glyph: '○' },
  running: { color: t => t.color.accent, glyph: '●' },
  completed: { color: t => t.color.statusGood, glyph: '✓' },
  failed: { color: t => t.color.error, glyph: '✗' },
  skipped: { color: t => t.color.muted, glyph: '⊘' },
  cancelled: { color: t => t.color.error, glyph: '⊗' },
  interrupted: { color: t => t.color.warn, glyph: '■' },
  exception: { color: t => t.color.warn, glyph: '⚠' }
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`

// Markdown furniture opening a line: a heading marker, a bullet, an ordered-list
// number, or a blockquote. The line reads as prose without it, and a row this
// narrow has no cells to spend on syntax.
//
// Each marker is matched only where whitespace or the line end follows it, so
// `-5 degrees` and `*emphasis*` keep their first character.
const LINE_FURNITURE_RE = /^(?:(?:#{1,6}|[-*+]|\d+[.)])(?=\s|$)\s*|>\s*)+/

// A `{{ <node>.output }}` / `{{ ref:<path> }}` injection point. Collapsed rather
// than shown: the row already spells its dependencies out at the end, so the
// node name inside the placeholder is redundant there, and 40 cells of it crowd
// out the words that say what the node actually does.
const PLACEHOLDER_RE = /\{\{[^}]*\}\}/g

// Section headings a prompt uses as scaffolding. They name where the request is,
// never what it is, so a row showing one says nothing about the node at all --
// which is why a bare label is skipped while `## Task: audit the skills`, whose
// text does state the request, is kept.
const SECTION_LABEL_RE =
  /^(?:tasks?|goals?|objectives?|contexts?|backgrounds?|instructions?|inputs?|requests?|prompts?|roles?|summary|outputs?)\s*:?$/i

/**
 * What a node is being asked, as one line no wider than `room`.
 *
 * `nodeSummary` is the line the node was dispatched with and wins whenever the
 * run carried one -- it was written for a reader, not sliced out of a prompt.
 * The template heuristic below is the fallback for a run whose graph predates
 * the field: the first line of the prompt template that says something, since a
 * DAG prompt opens with its instruction and elaborates below it. Blank lines,
 * bare markdown markers, lone placeholders and section labels are stepped over
 * -- each would summarise to nothing, or to punctuation, and leave the row
 * unable to say what its node is for.
 *
 * Empty when neither reached the client -- an older run dir wrote no template
 * and no summary, and a host that does not correlate progress with a tool row
 * supplies no call args. The row then falls back to the node id, the only
 * other thing that names it.
 */
export const dagNodeSummary = (
  nodeSummary: string | undefined,
  promptTemplate: string | undefined,
  room: number
): string => {
  const given = (nodeSummary ?? '').trim()
  if (given) {
    return clipToWidth(given, room)
  }

  const first = (promptTemplate ?? '')
    .split('\n')
    .map(line => line.trim().replace(LINE_FURNITURE_RE, '').replace(PLACEHOLDER_RE, '…').trim())
    .find(line => line && line !== '…' && !SECTION_LABEL_RE.test(line))

  return first ? clipToWidth(first, room) : ''
}

/**
 * The dependencies a node's row still has to name, as the exact string it draws.
 *
 * `drawn` holds the `${dep}>${node}` keys the picture above the rows already
 * drew as edges, and naming those again here would say twice what the graph
 * already shows. What survives is the dependency the picture *cannot* draw: one
 * naming a node an earlier run of the session completed, which has no box. A
 * `null` means no picture was drawn at all -- too narrow a terminal -- and then
 * the row is the only place the topology exists, so every dependency is named.
 */
export const dagNodeDeps = (node: Pick<DagRunNode, 'dependsOn' | 'id'>, drawn: ReadonlySet<string> | null): string => {
  const named = drawn ? node.dependsOn.filter(dep => !drawn.has(`${dep}>${node.id}`)) : node.dependsOn

  return named.length > 0 ? ` \u2190 ${named.join(', ')}` : ''
}

/**
 * The stateful handle a node ran on: `<subagent>@<instance>`, or empty.
 *
 * Detail, not a row: the subagent half repeats what the row already opens with
 * and the picture's box repeats again, and the instance half is a generated
 * suffix. Forty cells of it crowded out the words a reader is scanning for and
 * wrapped the row anyway, so it belongs in the expanded block beside the node
 * id -- the other thing a reader opens a node to find.
 */
export const dagNodeHandle = (node: Pick<DagRunNode, 'instance' | 'subagent'>): string =>
  node.instance ? `${node.subagent}@${node.instance}` : ''

/**
 * The instance handles more than one node in the run shares.
 *
 * Sharing one is the run's only piece of topology the graph does not draw:
 * those nodes ran in sequence on the same stateful agent whether or not an
 * edge says so. So the rows holding a shared handle -- and only those -- carry
 * a short tag of it, enough to tell two shared handles apart. A handle used
 * once constrains nothing and stays in the expanded block.
 */
export const dagSharedInstances = (nodes: readonly Pick<DagRunNode, 'instance'>[]): ReadonlySet<string> => {
  const counts = new Map<string, number>()

  for (const node of nodes) {
    if (node.instance) {
      counts.set(node.instance, (counts.get(node.instance) ?? 0) + 1)
    }
  }

  return new Set([...counts].filter(([, n]) => n > 1).map(([id]) => id))
}

/**
 * The run's tally as glyph + count, in a fixed status order, zeros dropped.
 *
 * Glyphs rather than words: the panel's header carries this beside the run id
 * and the elapsed time, and `3 nodes - 1 done - 1 running` spent the whole row
 * saying what `1\u2713 1\u25cf 1\u25cb` says in eight cells. Same glyphs the rows and the
 * picture use, so a header count and the row it refers to are read as one.
 *
 * A finished run reports the manifest's tally where it has one, which is
 * authoritative; a live one counts its own nodes.
 */
export const dagRunTally = (run: DagRunState): { count: number; status: DagRunNodeStatus }[] => {
  const counted = (status: DagRunNodeStatus) => run.nodes.filter(node => node.status === status).length
  const fromManifest = (status: DagRunNodeStatus, manifest: number | undefined) =>
    run.done ? (manifest ?? counted(status)) : counted(status)

  const order: [DagRunNodeStatus, number][] = [
    ['completed', fromManifest('completed', run.summary?.completed)],
    ['running', counted('running')],
    ['pending', counted('pending')],
    ['failed', fromManifest('failed', run.summary?.failed)],
    ['skipped', fromManifest('skipped', run.summary?.skipped)],
    ['cancelled', fromManifest('cancelled', run.summary?.cancelled)],
    ['interrupted', counted('interrupted')]
  ]

  return order.filter(([, count]) => count > 0).map(([status, count]) => ({ count, status }))
}

/**
 * How long one node took, or has been going.
 *
 * Empty for a node whose run dir recorded no timings, since a made-up zero
 * would read as a node that did nothing. A node that has not started says what
 * it is waiting as instead -- the column then reads down as time-or-state
 * rather than going blank for the half of the graph that has not run yet.
 */
export const dagNodeElapsed = (node: Pick<DagRunNode, 'endedAt' | 'startedAt' | 'status'>, now: number): string => {
  if (node.status === 'pending') {
    return 'queued'
  }

  if (node.startedAt === undefined) {
    return ''
  }

  return fmtDuration((node.endedAt ?? now) - node.startedAt)
}

/**
 * How long the whole run has taken: its first node's start to its last node's
 * end, or to `now` while any node is still going.
 *
 * Read off the nodes rather than tracked separately because the nodes are the
 * only thing either transport (live frames, `dag.get` snapshot) timestamps.
 * `undefined` when nothing has started, which is also every run whose dir
 * predates the timestamps.
 */
export const dagRunElapsedMs = (run: DagRunState, now: number): number | undefined => {
  const starts = run.nodes.map(node => node.startedAt).filter((ms): ms is number => ms !== undefined)

  if (starts.length === 0) {
    return undefined
  }

  const started = Math.min(...starts)
  const live = run.nodes.some(node => node.status === 'running' || node.status === 'pending')
  const ends = run.nodes.map(node => node.endedAt).filter((ms): ms is number => ms !== undefined)

  if (live || ends.length === 0) {
    return Math.max(0, now - started)
  }

  return Math.max(0, Math.max(...ends) - started)
}

/**
 * How many rows of trace one node's box shows, and how tall that box is.
 *
 * Off the node's own status rather than the trace store's `settled`: the box's
 * height has to be knowable by the transcript's height model, which sees the
 * graph and not the traces.
 */
export const dagTraceRows = (status: DagRunNodeStatus): number =>
  status === 'running' || status === 'pending' ? DAG_TRACE_ROWS : DAG_TRACE_ROWS_SETTLED

export const dagTraceBoxRows = (status: DagRunNodeStatus): number =>
  status === 'running' || status === 'pending' ? DAG_TRACE_BOX_ROWS : DAG_TRACE_BOX_ROWS_SETTLED

/** Cells the turned-in line has, inside a panel `inner` wide. Shared with the
 *  transcript's height model, which has to agree with the panel about whether a
 *  node has a second line at all.
 *
 *  The line starts where the node's name does: its turn-in mark stands in the
 *  ordinal's column, so the mark costs nothing beyond the indent the row above
 *  already spends on the glyph (`2`), the ordinal, and the gap after it (`2`). */
export const dagDetailRoom = (inner: number, ordinalWidth: number): number =>
  Math.max(0, inner - (2 + ordinalWidth + 2))

/**
 * What a node's row says under its name: the line it was dispatched with, what
 * it is still waiting on, and how it failed.
 *
 * One place, because the panel draws it and the transcript's height model has
 * to know whether it exists. Clipped in cells here rather than left to ink's
 * `truncate-end`, which is a no-op on the nested Texts this is drawn as.
 *
 * The three share `room` in the order a reader needs them: the error first,
 * then the dependency list, then the summary with what is left.
 */
export const dagRowDetail = (
  node: Pick<DagRunNode, 'dependsOn' | 'error' | 'id' | 'nodeSummary' | 'promptTemplate'>,
  drawn: ReadonlySet<string> | null,
  room: number
): { deps: string; error: string; summary: string } => {
  const error = node.error ? clipToWidth(node.error, Math.max(0, room)) : ''
  const afterError = room - (error ? stringWidth(error) : 0)
  const wanted = dagNodeDeps(node, drawn).trim()
  const deps = wanted && afterError > 4 ? clipToWidth(wanted, afterError - 2) : ''
  const left = afterError - (deps ? stringWidth(deps) + 2 : 0)

  return { deps, error, summary: left > 4 ? dagNodeSummary(node.nodeSummary, node.promptTemplate, left) : '' }
}

/**
 * One-line progress summary for the run's header row.
 *
 * A finished run reports the manifest's tally, which is authoritative; a live one
 * counts its own nodes. Zero-valued parts are dropped so a clean run reads
 * "4 nodes · 4 done" rather than trailing two zeroes.
 */
export const dagRunHeadline = (run: DagRunState): string => {
  const counted = (status: DagRunNodeStatus) => run.nodes.filter(node => node.status === status).length
  const total = run.summary?.total ?? run.nodes.length
  const completed = run.done ? (run.summary?.completed ?? counted('completed')) : counted('completed')
  const failed = run.done ? (run.summary?.failed ?? counted('failed')) : counted('failed')
  const skipped = run.done ? (run.summary?.skipped ?? counted('skipped')) : counted('skipped')
  const cancelled = run.done ? (run.summary?.cancelled ?? counted('cancelled')) : counted('cancelled')

  const parts = [plural(total, 'node'), `${completed} done`]

  if (!run.done && counted('running') > 0) {
    parts.push(`${counted('running')} running`)
  }

  if (!run.done && counted('exception') > 0) {
    parts.push(plural(counted('exception'), 'exception'))
  }

  if (failed > 0) {
    parts.push(`${failed} failed`)
  }

  if (skipped > 0) {
    parts.push(`${skipped} skipped`)
  }

  if (cancelled > 0) {
    parts.push(`${cancelled} cancelled`)
  }

  if (run.done && counted('interrupted') > 0) {
    parts.push(`${counted('interrupted')} interrupted`)
  }

  return parts.join(' · ')
}

// One line per thing the node did, for the command that is the only way to
// read a trace longer than the panel's box. Flat text on purpose: this goes
// into the transcript as a system message, which has no structure to render
// into.
//
// The transcript's first and last entries are usually the prompt and the
// output themselves -- `_with_messages` in raven/rpc/methods/dag.py brackets a
// node's real turns with them so the ordinary renderer has bubbles either side
// to draw -- and both already print below under their own headings. Skipped
// here by content, not just position, so a transcript that genuinely opens or
// closes with something else keeps every line, and a node with no prompt or no
// output loses nothing.
const traceLines = (detail: DagNodeDetail): string[] => {
  const messages = detail.messages ?? []
  const lastIndex = messages.length - 1
  const expectedOutput = detail.output ?? detail.error
  const echoesPrompt = (i: number) =>
    i === 0 && detail.prompt !== undefined && messages[i].role === 'user' && messages[i].text === detail.prompt
  const echoesOutput = (i: number) =>
    i === lastIndex &&
    expectedOutput !== undefined &&
    messages[i].role === 'assistant' &&
    messages[i].text === expectedOutput
  const out: string[] = []

  messages.forEach((msg, i) => {
    if (echoesPrompt(i) || echoesOutput(i)) {
      return
    }

    if (msg.reasoning_content?.trim()) {
      out.push(`  (thought) ${compactPreview(msg.reasoning_content, 200)}`)
    }

    if (msg.text?.trim() && msg.role !== 'tool') {
      out.push(`  ${compactPreview(msg.text, 300)}`)
    }

    for (const call of msg.tool_calls ?? []) {
      out.push(`  > ${formatToolCall(call.name, callSubject(call.arguments))}`)
    }

    if (msg.role === 'tool' && msg.text?.trim()) {
      out.push(`    ${compactPreview(msg.text, 200)}`)
    }
  })

  return out
}

/**
 * One node's rendered prompt, its trace, and its output, as a transcript block.
 *
 * The *rendered* prompt is the point: it is the text the sub-agent actually
 * received, with the upstream nodes' outputs already substituted in, which is
 * what makes a surprising result explainable. Neither field reaches the client
 * any other way -- the manifest inlines only the leaf nodes' text.
 *
 * Between prompt and output sits the trace: every step the node took to get
 * from one to the other, or nothing at all when it took none.
 */
export const formatDagNodeDetail = (detail: DagNodeDetail): string => {
  const size = detail.output_truncated
    ? ` (${detail.output_chars} chars, truncated)`
    : detail.output_chars > 0
      ? ` (${detail.output_chars} chars)`
      : ''
  const trace = traceLines(detail)

  return [
    `── ${detail.node} @ ${detail.run_id}${size} ──`,
    'prompt:',
    detail.prompt ?? '  (no prompt — the node never ran)',
    ...(trace.length > 0 ? ['trace:', ...trace] : []),
    'output:',
    detail.output ?? '  (no output)'
  ].join('\n')
}
