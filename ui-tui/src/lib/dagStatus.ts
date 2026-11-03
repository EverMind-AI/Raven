// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// How a DAG node and a run's progress read on screen. Split from the panel so
// both are testable without a render, and so the transcript's folded row and the
// expanded graph can never disagree about a run's tally.

import { stringWidth } from '@hermes/ink'

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail, DagNotExecutedReason } from '../rpc/index.js'
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
  not_executed: { color: t => t.color.muted, glyph: '◌' },
  cancelled: { color: t => t.color.error, glyph: '⊗' },
  interrupted: { color: t => t.color.warn, glyph: '■' },
  exception: { color: t => t.color.warn, glyph: '⚠' }
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`

// Why a branch left a node out, in the words a row has room for.
const REASON_WORDS: Record<DagNotExecutedReason, string> = {
  upstream_not_executed: 'upstream not executed',
  when_false: 'not selected',
  when_unknown: 'decision missing'
}

// Empty for a reason this client has no words for, which reads as no reason. `hasOwn`
// rather than a truthy lookup: `constructor` and `__proto__` are not reasons, and the
// prototype's values for them are not text a row can print.
const reasonWords = (reason: DagNotExecutedReason | undefined): string =>
  (reason && Object.hasOwn(REASON_WORDS, reason) && REASON_WORDS[reason]) || ''

// `[]` is a decision that chose nothing, and is said so.
const routesText = (routes: readonly string[]): string => (routes.length > 0 ? routes.join(', ') : '(none)')

/**
 * What a branch did to a node, as the short text that opens its detail line:
 * why it was left out, or what a decision node selected.
 *
 * A not-executed node whose reason never reached the client says only that. A
 * run reloaded before it finished reads its reasons off registry rows, whose
 * writes are best-effort and can be dropped, and a cause is not something to
 * guess at. `selectedRoutes` is read by presence: `[]` is a decision that chose
 * nothing, `undefined` one not made.
 */
export const dagNodeOutcome = (node: Pick<DagRunNode, 'reason' | 'selectedRoutes' | 'status'>): string => {
  if (node.status === 'not_executed') {
    return reasonWords(node.reason) || 'not executed'
  }

  if (node.selectedRoutes === undefined) {
    return ''
  }

  return `→ ${routesText(node.selectedRoutes)}`
}

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
    ['not_executed', fromManifest('not_executed', run.summary?.not_executed)],
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
 *
 * Empty for a node a branch left out whatever it carries: `dag.get` hands back
 * the stamps the sweep gave it when it decided against it, which date that
 * decision and not a run.
 */
export const dagNodeElapsed = (node: Pick<DagRunNode, 'endedAt' | 'startedAt' | 'status'>, now: number): string => {
  if (node.status === 'pending') {
    return 'queued'
  }

  if (node.status === 'not_executed' || node.status === 'skipped') {
    return ''
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
 *
 * Only the nodes that ran count: one a branch left out or a cascade skipped
 * carries the stamp of the sweep that decided against it, which must not
 * widen the window of the nodes that did.
 */
export const dagRunElapsedMs = (run: DagRunState, now: number): number | undefined => {
  const ran = run.nodes.filter(node => node.status !== 'not_executed' && node.status !== 'skipped')
  const starts = ran.map(node => node.startedAt).filter((ms): ms is number => ms !== undefined)

  if (starts.length === 0) {
    return undefined
  }

  const started = Math.min(...starts)
  const live = run.nodes.some(node => node.status === 'running' || node.status === 'pending')
  const ends = ran.map(node => node.endedAt).filter((ms): ms is number => ms !== undefined)

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

export interface DagRowDetail {
  deps: string
  error: string
  evidence: string
  guard: string
  outcome: string
  summary: string
}

/** Whether a node's row has anything to say under its name at all. */
export const hasDagRowDetail = (detail: DagRowDetail): boolean => Object.values(detail).some(Boolean)

/**
 * What a node's row says under its name: what a branch did to it, the line it
 * was dispatched with, what it is still waiting on, the guard it runs under,
 * the evidence behind its selection, and how it failed.
 *
 * One place, because the panel draws it and the transcript's height model has
 * to know whether it exists. Clipped in cells here rather than left to ink's
 * `truncate-end`, which is a no-op on the nested Texts this is drawn as.
 *
 * The parts share `room` in the order a reader needs them: the error first,
 * then what a branch did (why the node was left out, or what it selected), the
 * dependency list, the guard, the evidence, and the summary with what is left.
 * With no branch part present the error, the dependencies and the summary share
 * `room` exactly as they would alone.
 */
export const dagRowDetail = (
  node: Pick<
    DagRunNode,
    | 'dependsOn'
    | 'error'
    | 'id'
    | 'nodeSummary'
    | 'promptTemplate'
    | 'reason'
    | 'routeEvidence'
    | 'selectedRoutes'
    | 'status'
    | 'when'
  >,
  drawn: ReadonlySet<string> | null,
  room: number
): DagRowDetail => {
  const error = node.error ? clipToWidth(node.error, Math.max(0, room)) : ''
  const outcomeText = dagNodeOutcome(node)
  const guardText = node.when?.trim() ? `when ${node.when.trim()}` : ''
  const evidenceText = node.routeEvidence?.trim() ?? ''
  // The error's own two-cell gap is not part of the budgets below. Beside a
  // branch's parts it is taken here, or a full line runs past its frame.
  const afterError =
    room - (error ? stringWidth(error) : 0) - (error && (outcomeText || guardText || evidenceText) ? 2 : 0)
  const outcome = outcomeText && afterError > 4 ? clipToWidth(outcomeText, afterError - 2) : ''
  const afterOutcome = afterError - (outcome ? stringWidth(outcome) + 2 : 0)
  const wanted = dagNodeDeps(node, drawn).trim()
  const deps = wanted && afterOutcome > 4 ? clipToWidth(wanted, afterOutcome - 2) : ''
  const afterDeps = afterOutcome - (deps ? stringWidth(deps) + 2 : 0)
  const guard = guardText && afterDeps > 4 ? clipToWidth(guardText, afterDeps - 2) : ''
  const afterGuard = afterDeps - (guard ? stringWidth(guard) + 2 : 0)
  const evidence = evidenceText && afterGuard > 8 ? `"${clipToWidth(evidenceText, afterGuard - 4)}"` : ''
  const left = afterGuard - (evidence ? stringWidth(evidence) + 2 : 0)

  return {
    deps,
    error,
    evidence,
    guard,
    outcome,
    summary: left > 4 ? dagNodeSummary(node.nodeSummary, node.promptTemplate, left) : ''
  }
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
  const notExecuted = run.done ? (run.summary?.not_executed ?? counted('not_executed')) : counted('not_executed')
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

  if (notExecuted > 0) {
    parts.push(`${notExecuted} not executed`)
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

/** What a node's paged view reads off the run the command found it in: what a
 *  branch made of it, and who tried it. `dag.node` carries none of this. */
export type DagNodeBranch = Pick<
  DagRunNode,
  'attemptAgents' | 'reason' | 'routeEvidence' | 'selectedRoutes' | 'status' | 'when'
>

/**
 * The agents that tried a node, in order, with an uninterrupted run of the same
 * agent collapsed into a count: `alpha x2 -> beta -> alpha`.
 *
 * Only *consecutive* repeats are collapsed. An agent that came back later is a
 * separate attempt, and folding it into its first run would misstate the order
 * the node actually moved in.
 */
export const formatAttemptAgents = (agents: readonly string[]): string => {
  const runs: { agent: string; count: number }[] = []

  for (const agent of agents) {
    const last = runs[runs.length - 1]

    if (last && last.agent === agent) {
      last.count += 1
    } else {
      runs.push({ agent, count: 1 })
    }
  }

  return runs.map(({ agent, count }) => (count > 1 ? `${agent} x${count}` : agent)).join(' -> ')
}

// The lines under a paged node's title that say what a branch made of it, each
// only when it did. The evidence is whole here, where the panel's one line has to
// clip it: the page scrolls, and the quote is how a reader checks the judge's
// reading against the node's own words.
const branchLines = (node: DagNodeBranch | undefined): string[] => {
  const guard = node?.when?.trim()
  const evidence = node?.routeEvidence?.trim()
  const reason = reasonWords(node?.reason)
  const attempts = node?.attemptAgents?.length ? formatAttemptAgents(node.attemptAgents) : ''

  const outcome =
    node?.status === 'not_executed'
      ? `not executed${reason ? `: ${reason}` : ''}`
      : node?.selectedRoutes === undefined
        ? ''
        : `selected: ${routesText(node.selectedRoutes)}`

  return [
    ...(guard ? [`when ${guard}`] : []),
    ...(outcome ? [outcome] : []),
    ...(evidence ? [`evidence: "${evidence}"`] : []),
    ...(attempts ? [`attempts: ${attempts}`] : [])
  ]
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
 *
 * Under the title, when the node has them: its guard, what a branch did to it
 * (why it was left out, or what it selected) and the evidence behind a
 * selection. A node with none of these pages as it always did.
 */
export const formatDagNodeDetail = (detail: DagNodeDetail, branch?: DagNodeBranch): string => {
  const size = detail.output_truncated
    ? ` (${detail.output_chars} chars, truncated)`
    : detail.output_chars > 0
      ? ` (${detail.output_chars} chars)`
      : ''
  const trace = traceLines(detail)

  return [
    `── ${detail.node} @ ${detail.run_id}${size} ──`,
    ...branchLines(branch),
    'prompt:',
    detail.prompt ?? '  (no prompt — the node never ran)',
    ...(trace.length > 0 ? ['trace:', ...trace] : []),
    'output:',
    detail.output ?? '  (no output)'
  ].join('\n')
}
