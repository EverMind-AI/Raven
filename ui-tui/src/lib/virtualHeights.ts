// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import type { Msg } from '../types.js'

import { foldedPreviewRows, segmentTurn } from '../domain/episodeSummary.js'
import { transcriptBodyWidth } from './inputMetrics.js'
import { hasMeaningfulReasoning } from './reasoning.js'
import { boundedHistoryRenderText } from './text.js'

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
  const epSig =
    msg.episodes
      ?.map(ep => `${ep.narration?.length ?? 0}:${ep.tools.map(tool => foldedPreviewRows(tool) + 1).join(',')}`)
      .join('\u0001') ?? ''

  return [
    msg.role,
    msg.kind ?? '',
    hashText([msg.text, msg.thinking ?? '', msg.tools?.join('\n') ?? '', todoSig, panelSig, introSig, epSig].join('\0'))
  ].join(':')
}

export const wrappedLines = (text: string, width: number) => {
  const w = Math.max(1, width)

  return text.split('\n').reduce((n, line) => n + Math.max(1, Math.ceil(line.length / w)), 0)
}

export const estimatedMsgHeight = (
  msg: Msg,
  cols: number,
  {
    compact,
    details,
    limitHistory = false,
    userPrompt = '',
    withSeparator = false
  }: {
    compact: boolean
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
        h++
        continue
      }

      const narration = (seg.episode.narration ?? '').trim()
      const reasoning = (seg.episode.reasoning ?? '').trim()

      // reasoning row (+ its own blank line above the prose)
      h += hasMeaningfulReasoning(reasoning) ? 2 : 0
      h += narration ? wrappedLines(narration, bodyWidth) : 0
    }

    if (msg.text) {
      h += 1 + wrappedLines(msg.text, bodyWidth)
    }

    return Math.max(1, h)
  }
  const text = msg.role === 'assistant' && limitHistory ? boundedHistoryRenderText(msg.text) : msg.text
  let h = wrappedLines(text || ' ', bodyWidth)

  if (!compact && msg.role === 'assistant') {
    h += Math.min(6, (text.match(/\n\s*\n/g) ?? []).length)
  }

  if (details) {
    h += (msg.tools?.length ?? 0) + wrappedLines(msg.thinking ?? '', bodyWidth)
  }

  if (msg.role === 'user' || msg.kind === 'diff') {
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
