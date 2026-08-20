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

import { clipToWidth } from './text.js'

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
 * The first line of the prompt template that says something: a DAG prompt opens
 * with its instruction and elaborates below it, so that line is the query and
 * the rest is detail. Blank lines, bare markdown markers, lone placeholders and
 * section labels are stepped over -- each would summarise to nothing, or to
 * punctuation, and leave the row unable to say what its node is for.
 *
 * Empty when no template reached the client -- an older run dir wrote none, and
 * a host that does not correlate progress with a tool row supplies no call args.
 * The row then falls back to the node id, the only other thing that names it.
 */
export const dagNodeSummary = (promptTemplate: string | undefined, room: number): string => {
  const first = (promptTemplate ?? '')
    .split('\n')
    .map(line => line.trim().replace(LINE_FURNITURE_RE, '').replace(PLACEHOLDER_RE, '…').trim())
    .find(line => line && line !== '…' && !SECTION_LABEL_RE.test(line))

  return first ? clipToWidth(first, room) : ''
}

/**
 * The two trailing parts of a node's row, as the exact strings it renders.
 *
 * Returned together with the row's own arithmetic in mind: the summary gets
 * whatever cells these leave, so a caller measuring them separately from what it
 * draws would clip against the wrong budget.
 */
export const dagNodeNames = (node: Pick<DagRunNode, 'dependsOn' | 'instance' | 'subagent'>) => ({
  deps: node.dependsOn.length > 0 ? ` \u2190 ${node.dependsOn.join(', ')}` : '',
  parens: `(${node.subagent}${node.instance ? `@${node.instance}` : ''})`
})

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

/**
 * One node's rendered prompt and output, as a transcript block.
 *
 * The *rendered* prompt is the point: it is the text the sub-agent actually
 * received, with the upstream nodes' outputs already substituted in, which is
 * what makes a surprising result explainable. Neither field reaches the client
 * any other way -- the manifest inlines only the leaf nodes' text.
 */
export const formatDagNodeDetail = (detail: DagNodeDetail): string => {
  const size = detail.output_truncated
    ? ` (${detail.output_chars} chars, truncated)`
    : detail.output_chars > 0
      ? ` (${detail.output_chars} chars)`
      : ''

  return [
    `── ${detail.node} @ ${detail.run_id}${size} ──`,
    'prompt:',
    detail.prompt ?? '  (no prompt — the node never ran)',
    'output:',
    detail.output ?? '  (no output)'
  ].join('\n')
}
