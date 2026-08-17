// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// How a DAG node's status and a run's progress read on screen. Split from the
// panel so both are testable without a render, and so the transcript's folded
// row and the expanded graph can never disagree about a run's tally.

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail } from '../rpc/index.js'
import type { Theme } from '../theme.js'

/** Glyph + tone per status. A `Record` over the union rather than a lookup with
 * a fallback, so adding a status without styling it is a type error instead of a
 * blank column. Mirrors `agentsOverlay`'s table so the two read alike. */
export const DAG_STATUS_GLYPH: Record<DagRunNodeStatus, { color: (t: Theme) => string; glyph: string }> = {
  pending: { color: t => t.color.muted, glyph: '○' },
  running: { color: t => t.color.accent, glyph: '●' },
  completed: { color: t => t.color.statusGood, glyph: '✓' },
  failed: { color: t => t.color.error, glyph: '✗' },
  skipped: { color: t => t.color.muted, glyph: '⊘' },
  interrupted: { color: t => t.color.warn, glyph: '■' }
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`

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
