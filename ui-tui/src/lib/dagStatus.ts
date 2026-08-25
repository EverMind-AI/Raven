// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// How a DAG node and a run's progress read on screen. Split from the panel so
// both are testable without a render, and so the transcript's folded row and the
// expanded graph can never disagree about a run's tally.

import type { DagRunNode, DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail } from '../rpc/index.js'
import type { Theme } from '../theme.js'

import { callSubject } from '../domain/episodeFold.js'
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
  interrupted: { color: t => t.color.warn, glyph: '■' }
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
