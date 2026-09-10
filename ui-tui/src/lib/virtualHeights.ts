// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { stringWidth } from '@hermes/ink'

import type { EpisodeTool, Msg } from '../types.js'

import { EMPTY_CALL_FOLDS, type CallFolds } from '../app/foldStore.js'
import { cardDefaultOpen, detailBlockShape, foldedPreviewRows, segmentTurn } from '../domain/episodeSummary.js'
import { layoutDagGraph } from './dagGraphLayout.js'
import { dagNodeToggleKey } from './dagOpenNodes.js'
import { dagDetailRoom, dagRowDetail, dagTraceBoxRows, dagTraceRows } from './dagStatus.js'
import { transcriptBodyWidth } from './inputMetrics.js'
import { hasMeaningfulReasoning } from './reasoning.js'
import { spawnTraceOpen } from './spawnOpen.js'
import { boundedHistoryRenderText } from './text.js'

// One allocation shared by every caller that has no open node, rather than one
// per estimatedMsgHeight call.
const EMPTY_OPEN: ReadonlySet<string> = new Set()
const EMPTY_OVERRIDES: ReadonlyMap<string, boolean> = new Map()

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
  // A spawn panel's height moves with its run's status (the trace box grows
  // once the run settles, and its default open flips), so the status is part
  // of the key the same way a dag's shape is.
  const spawnSig = (spawn: EpisodeTool['spawn']) => (spawn ? `/s.${spawn.status}.${spawn.callId ? 1 : 0}` : '')
  const epSig =
    msg.episodes
      ?.map(
        ep =>
          `${ep.narration?.length ?? 0}:${ep.steer?.length ?? 0}:${ep.tools.map(tool => `${foldedPreviewRows(tool) + 1}${dagSig(tool.dag)}${spawnSig(tool.spawn)}`).join(',')}`
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
// that needs them. INDENT tracks the transcript gutter (see
// TRANSCRIPT_GUTTER_INSET), which is what episodeView derives its own from.
const INDENT = 3
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

/**
 * Row count for one spawn call's panel. Exactly `SpawnPanel`'s structure,
 * resolved through the same `spawnTraceOpen` and `dagTraceRows` the panel
 * renders from, so the estimate cannot drift from what it draws.
 *
 * Folded: two border rows, the header, its rule, and the hint. Open: the hint
 * row becomes the trace's own footer, so the difference is the meta line, the
 * fixed-height trace, and that footer.
 */
const spawnPanelRows = (run: NonNullable<EpisodeTool['spawn']>, overrides: ReadonlyMap<string, boolean>): number =>
  spawnTraceOpen(run, overrides) ? 6 + dagTraceRows(run.status) : 5

/**
 * Rows an open card spends beyond the call's own row: the blank row `ActivityRow`
 * puts above an opened row, the block's argument and result, its "+N" row, and
 * the block's own padding and margin. Exactly `DetailBlock`'s structure, off the
 * same `detailBlockShape` the view renders from -- a card whose block renders
 * nothing costs nothing, which is why the shape is asked rather than assumed.
 *
 * The result rows are measured wrapped, not counted. `foldedPreviewRows` counts
 * logical lines and the block draws them with `wrap="wrap"`, so a long line is
 * several rows there and one row here -- and a low estimate is the stale-cell
 * symptom. The cap itself still comes from the shape, so it stays single-sourced.
 */
const openCardRows = (tool: EpisodeTool, width: number, indent: number, full: boolean): number => {
  const shape = detailBlockShape(tool, full)

  if (!shape) {
    return 0
  }

  // DetailBlock's own paddingLeft (indent + 1) and paddingRight (1).
  const body = Math.max(8, width - indent - 2)
  const argument = shape.argument ? wrappedLines(shape.argument, body) : 0
  const output = shape.output.reduce((n, line) => n + wrappedLines(line, body), 0)
  // The "+N" row truncates rather than wraps, so it is one row whatever it says.
  const more = shape.more ? 1 : 0

  return 1 + argument + output + more + 2
}

export const estimatedMsgHeight = (
  msg: Msg,
  cols: number,
  {
    cardFolds = EMPTY_CALL_FOLDS,
    compact,
    dagOpen = EMPTY_OPEN,
    dense = false,
    details,
    limitHistory = false,
    spawnOverrides = EMPTY_OVERRIDES,
    userPrompt = '',
    withSeparator = false
  }: {
    /** What the reader has opened or shut, so a card that is not at its default
     *  is measured as it is drawn. Omitted, every card sits at its default --
     *  which is what a trace box and a first paint both want. */
    cardFolds?: CallFolds
    compact: boolean
    dagOpen?: ReadonlySet<string>
    /** Trace-box rendering (see `MessageLine`): segments keep no breathing
     *  rows, so an episodes message measures without them too. */
    dense?: boolean
    details: boolean
    limitHistory?: boolean
    spawnOverrides?: ReadonlyMap<string, boolean>
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

    // episodeView separates segments with marginTop={1}; counting no row for
    // that is what keeps the estimate low, and a low estimate is the stale-cell
    // symptom. A stretch of work is no longer one row by construction: a settled
    // card opens on its own predicate, so its block is measured here the same way
    // it is drawn, and a talk segment is its reasoning row plus its wrapped prose.
    for (const [i, seg] of segmentTurn(msg.episodes ?? []).entries()) {
      h += i > 0 && !dense ? 1 : 0

      if (seg.kind === 'work') {
        // A DAG call's graph -- and a spawn call's panel -- renders at every
        // fold depth, including the folded default; see dagFor/spawnFor and
        // WorkSegment in episodeView.tsx. Neither call draws a row of its own:
        // the panel is a titled box that carries the call, so only the other
        // tools in the stretch are counted as rows. depth/width match
        // episodeView's own INDENT/STEP and width formula so the two cannot
        // drift apart.
        const panelWidth = cols ? Math.max(20, cols - 4) : 116
        const panelTools = seg.tools.filter(tool => tool.dag || tool.spawn)
        const hasPanel = panelTools.length > 0
        const panelRows = (tool: EpisodeTool, width: number) =>
          (tool.dag ? dagPanelRows(tool.dag, width, dagOpen) : 0) +
          (tool.spawn ? spawnPanelRows(tool.spawn, spawnOverrides) : 0)
        // A stretch keys on `seg:` and a card on `call:`, and the two id spaces
        // overlap -- a stretch reuses its first call's id. Reading them apart is
        // what keeps a shut stretch from also shutting the card that shares that
        // id (see callFolds).
        const segIsOpen = (key: string, fallback: boolean) =>
          cardFolds.segOpen.has(key) ? true : cardFolds.segClosed.has(key) ? false : fallback
        const callIsOpen = (id: string, fallback: boolean) =>
          cardFolds.callOpen.has(id) ? true : cardFolds.callClosed.has(id) ? false : fallback

        // Mirrors episodeView's detailDefaultOpen: for one call the stretch's own
        // fold IS the card's, because the row opens straight into the block, and
        // it waits on the call being done rather than on the turn ending. A solo
        // panel call keeps the folded default -- its graph is already drawn
        // above, so opening adds a block rather than revealing one.
        if (seg.tools.length === 1) {
          const tool = seg.tools[0]!

          h += hasPanel ? panelRows(tool, Math.max(28, panelWidth - INDENT)) : 1

          if (segIsOpen(seg.key, !hasPanel && Boolean(tool.done) && cardDefaultOpen(tool))) {
            h += openCardRows(tool, panelWidth, INDENT, cardFolds.full.has(tool.id))
          }

          continue
        }

        h += 1

        // Folded, a multi-call stretch is its summary row; the panels inside it
        // still draw (WorkSegment's own defaultOpen branch).
        if (!segIsOpen(seg.key, hasPanel && !seg.live)) {
          for (const tool of panelTools) {
            h += panelRows(tool, Math.max(28, panelWidth - (INDENT + STEP)))
          }

          continue
        }

        for (const tool of seg.tools) {
          if (tool.dag || tool.spawn) {
            h += panelRows(tool, Math.max(28, panelWidth - (INDENT + STEP)))
            continue
          }

          h += 1

          if (callIsOpen(tool.id, Boolean(tool.done) && cardDefaultOpen(tool))) {
            h += openCardRows(tool, panelWidth, INDENT + STEP, cardFolds.full.has(tool.id))
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

      // reasoning row (+ its own blank line above the prose, kept out of a
      // dense trace box)
      h += hasMeaningfulReasoning(reasoning) ? (dense ? 1 : 2) : 0
      h += narration ? wrappedProseLines(narration, bodyWidth) : 0
    }

    if (msg.text) {
      h += (dense ? 0 : 1) + wrappedProseLines(msg.text, bodyWidth)
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
