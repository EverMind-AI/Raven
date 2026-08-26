// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { stringWidth } from '@hermes/ink'

import type { EpisodeTool, Msg } from '../types.js'

import { foldedPreviewRows, segmentTurn } from '../domain/episodeSummary.js'
import { layoutDagGraph } from './dagGraphLayout.js'
import { dagNodeToggleKey } from './dagOpenNodes.js'
import { dagDetailRoom, dagRowDetail, dagTraceBoxRows } from './dagStatus.js'
import { transcriptBodyWidth } from './inputMetrics.js'
import { hasMeaningfulReasoning } from './reasoning.js'
import { boundedHistoryRenderText } from './text.js'

// One allocation shared by every caller that has no open node, rather than one
// per estimatedMsgHeight call.
const EMPTY_OPEN: ReadonlySet<string> = new Set()

const hashText = (text: string) => {
  let h = 5381

  for (let i = 0; i < text.length; i++) {
    h = ((h << 5) + h) ^ text.charCodeAt(i)
  }

  return (h >>> 0).toString(36)
}

export const messageHeightKey = (msg: Msg) => {
  const todoSig = msg.todos?.map(t => `${t.status}:${t.content}`).join('\u0001') ?? ''

  const panelSig =
    msg.panelData?.sections
      .map(s => `${s.title ?? ''}:${s.text?.length ?? 0}:${s.items?.length ?? 0}:${s.rows?.length ?? 0}`)
      .join('\u0001') ?? ''

  const introSig = msg.kind === 'intro' ? (msg.info?.version ?? '') : ''

  // Episodes drive the height for `kind: 'episodes'`, so they must be part of
  // the cache key — otherwise a stale height survives a step-count change.
  // A DAG call's own graph can change shape (nodes finish, the run completes)
  // without `foldedPreviewRows` noticing, so it gets its own signature too.
  const dagSig = (dag: EpisodeTool['dag']) =>
    dag
      ? `/${dag.nodes.length}.${dag.done ? 1 : 0}.${dag.dir ? 1 : 0}.${dag.nodes.filter(node => node.status === 'running').length}.${dag.nodes.reduce((n, node) => n + (node.error?.length ?? 0), 0)}`
      : ''
  const epSig =
    msg.episodes
      ?.map(
        ep =>
          `${ep.narration?.length ?? 0}:${ep.steer?.length ?? 0}:${ep.tools.map(tool => `${foldedPreviewRows(tool) + 1}${dagSig(tool.dag)}`).join(',')}`
      )
      .join('\u0001') ?? ''

  return [
    msg.role,
    msg.kind ?? '',
    hashText([msg.text, msg.thinking ?? '', msg.tools?.join('\n') ?? '', todoSig, panelSig, introSig, epSig].join('\0'))
  ].join(':')
}

export const wrappedLines = (text: string, width: number) => {
  const w = Math.max(1, width)

  return text.split('\n').reduce((n, line) => n + Math.max(1, Math.ceil(stringWidth(line) / w)), 0)
}

const QUOTE_PREFIX_RE = /^\s*(?:>\s*)+/

/**
 * Wrapped row count for markdown prose, where a quote line does not get the
 * whole body width: `markdown.tsx` draws it inside a box whose left border and
 * padding spend two cells on every row, two more per nesting level. Measuring
 * one at the full width under-counts a quote that wraps -- and a low estimate is
 * the stale-cell symptom the rest of this file is careful about. The `>` markers
 * themselves are not drawn, so they come off the measured text.
 */
const wrappedProseLines = (text: string, width: number) =>
  text.split('\n').reduce((n, line) => {
    const prefix = line.match(QUOTE_PREFIX_RE)?.[0]

    if (prefix === undefined) {
      return n + Math.max(1, Math.ceil(stringWidth(line) / Math.max(1, width)))
    }

    const depth = (prefix.match(/>/g) ?? []).length
    const room = Math.max(1, width - 2 - Math.max(0, depth - 1) * 2)

    return n + Math.max(1, Math.ceil(stringWidth(line.slice(prefix.length)) / room))
  }, 0)

// Mirrors episodeView.tsx's own INDENT/STEP. Kept local rather than shared
// across the lib/component boundary -- this estimator is the only other place
// that needs them.
const INDENT = 2
const STEP = 2

/**
 * Row count for one DAG call's panel: its frame and header, the ascii picture
 * when one fits, a row per node plus the turned-in line for a node that has one,
 * each of those rows' open trace box, an outputs line once the run is done, and
 * the hint under its own rule. Exactly `DagPanel`'s own structure, and it reuses
 * the same layout and detail helpers the panel renders from, so the estimate
 * cannot drift from what it actually draws.
 *
 * The rows are unconditional. They were dropped for a while under a labelled
 * picture, on the grounds that the boxes already named every node -- which
 * stopped being true once a node carried the summary it was dispatched with,
 * something no box has room for.
 */
const dagPanelRows = (run: NonNullable<EpisodeTool['dag']>, width: number, open: ReadonlySet<string>): number => {
  if (run.nodes.length === 0) {
    return 0
  }

  // The panel's border and padding, which is what its contents are laid out in.
  const inner = Math.max(24, width - 4)
  const picture = layoutDagGraph(run.nodes, { width: inner })

  // What `DagNodeSlot` draws under each row: a box when expanded, nothing
  // otherwise. `dagNodeToggleKey` is reused rather than restated because it is
  // also the rule for what a click can open. The box's height is fixed per node
  // -- taller once the node has stopped -- so it is read off the status rather
  // than off the fold this is estimating.
  const slots = run.nodes.reduce((rows, node) => {
    const toggle = dagNodeToggleKey(run.runId, node)

    return rows + (toggle && open.has(toggle) ? dagTraceBoxRows(node.status) : 0)
  }, 0)

  // The line turned in under a node's name, when it has anything to say there.
  const ordinalWidth = String(run.nodes.length).length
  const room = dagDetailRoom(inner, ordinalWidth)
  const details = run.nodes.reduce((rows, node) => {
    const detail = dagRowDetail(node, picture?.drawn ?? null, room)

    return rows + (detail.summary || detail.deps || detail.error ? 1 : 0)
  }, 0)

  // Two border rows, the header, the two rules and the hint.
  const chrome = 6

  return chrome + (picture?.height ?? 0) + run.nodes.length + details + slots + (run.done && run.dir ? 1 : 0)
}

export const estimatedMsgHeight = (
  msg: Msg,
  cols: number,
  {
    compact,
    dagOpen = EMPTY_OPEN,
    details,
    limitHistory = false,
    userPrompt = '',
    withSeparator = false
  }: {
    compact: boolean
    dagOpen?: ReadonlySet<string>
    details: boolean
    limitHistory?: boolean
    userPrompt?: string
    withSeparator?: boolean
  }
) => {
  if (msg.kind === 'intro') {
    return msg.info?.version ? 9 : 5
  }

  if (msg.kind === 'panel') {
    return Math.max(3, (msg.panelData?.sections.length ?? 1) * 2 + 1)
  }

  if (msg.kind === 'trail' && msg.todos?.length) {
    if (msg.todoCollapsedByDefault) {
      return 2
    }

    return Math.max(2, msg.todos.length + 2)
  }

  const bodyWidth = transcriptBodyWidth(cols, msg.role, userPrompt)

  // An `episodes` message renders a whole step stream (reasoning rows, prose,
  // tool rows, result previews) — none of which lives in `msg.text`. Estimating
  // it from the text alone under-counted by a wide margin, which makes the
  // virtualized transcript reserve too few rows and leave stale cells behind.
  if (msg.kind === 'episodes') {
    let h = 0

    // A committed message is never live and never opened at first paint, so
    // every stretch of work is exactly one row and every talk segment is its
    // reasoning row plus its wrapped prose. episodeView separates segments with
    // marginTop={1}; counting no row for that is what keeps the estimate low,
    // and a low estimate is the stale-cell symptom.
    for (const [i, seg] of segmentTurn(msg.episodes ?? []).entries()) {
      h += i > 0 ? 1 : 0

      if (seg.kind === 'work') {
        const dagTools = seg.tools.filter(tool => tool.dag)

        if (dagTools.length === 0) {
          h++
          continue
        }

        // A DAG call's graph renders at every fold depth, including the folded
        // default -- see dagFor/WorkSegment in episodeView.tsx -- so this
        // mirrors that instead of the one-row approximation below. A dag call
        // draws no row of its own: the panel is a titled box that carries the
        // call, so only the other tools in the stretch are counted as rows.
        // depth/width match episodeView's own INDENT/STEP and width formula so
        // the two cannot drift apart.
        const dagWidth = cols ? Math.max(20, cols - 4) : 116

        if (seg.tools.length === 1) {
          h += dagPanelRows(dagTools[0]!.dag!, Math.max(28, dagWidth - INDENT), dagOpen)
        } else {
          h += 1 + (seg.tools.length - dagTools.length)

          for (const tool of dagTools) {
            h += dagPanelRows(tool.dag!, Math.max(28, dagWidth - (INDENT + STEP)), dagOpen)
          }
        }

        continue
      }

      if (seg.episode.steer) {
        h += wrappedLines(seg.episode.steer, bodyWidth)
        continue
      }

      const narration = (seg.episode.narration ?? '').trim()
      const reasoning = (seg.episode.reasoning ?? '').trim()

      // reasoning row (+ its own blank line above the prose)
      h += hasMeaningfulReasoning(reasoning) ? 2 : 0
      h += narration ? wrappedProseLines(narration, bodyWidth) : 0
    }

    if (msg.text) {
      h += 1 + wrappedProseLines(msg.text, bodyWidth)
    }

    return Math.max(1, h)
  }
  const text = msg.role === 'assistant' && limitHistory ? boundedHistoryRenderText(msg.text) : msg.text
  let h = wrappedProseLines(text || ' ', bodyWidth)

  if (!compact && msg.role === 'assistant') {
    h += Math.min(6, (text.match(/\n\s*\n/g) ?? []).length)
  }

  if (details) {
    h += (msg.tools?.length ?? 0) + wrappedLines(msg.thinking ?? '', bodyWidth)
  }

  if (msg.role === 'user') {
    // A blank margin row each side, plus the two padding rows of the filled
    // prompt block messageLine draws around the text.
    h += 4
  } else if (msg.kind === 'diff') {
    h += 2
  } else if (msg.kind === 'slash') {
    h++
  }

  // Inter-turn separator above non-first user messages (1 rule row + 1
  // top-margin row). The render-side gate is in appLayout.tsx; we trust
  // the caller to pass `withSeparator` only when it matches that gate.
  if (withSeparator) {
    h += 2
  }

  return Math.max(1, h)
}
