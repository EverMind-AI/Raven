// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A DAG node's live transcript, reduced to what will fit on screen.
//
// Two reductions of the same wire messages, because the slot under a node row
// has two sizes: one line of the newest characters while the row is collapsed,
// and a slice of whole messages while it is expanded. Both are pure and take
// their budget as an argument -- the panel's width and its fixed row count are
// the only things that decide how much of a run is legible, and a reduction that
// read either from a store could not be tested against a known budget.

import { stringWidth } from '@hermes/ink'

import type { TranscriptMessage } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { DAG_TRACE_FIT_MAX_ROWS } from '../config/limits.js'
import { callSubject } from '../domain/episodeFold.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { clipToWidthFromEnd, formatToolCall } from './text.js'
import { estimatedMsgHeight } from './virtualHeights.js'

const SEP = ' · '

// A cap on how much of one message is examined. A tool result can be megabytes
// and only the last cells of it can ever be shown, so the work must follow the
// budget rather than the payload. Four chars per cell is well clear of the worst
// real case (a double-width char is two cells for one char). The thought and text
// are budget-capped; tool-call labels are small by design and handled separately.
const CHARS_PER_CELL = 4

/** The last `budget` code units, without orphaning a surrogate at the front. */
const lastChars = (text: string, budget: number): string =>
  text.length > budget ? text.slice(-budget).replace(/^[\uDC00-\uDFFF]/, '') : text

/** One message's contribution to the flattened stream, in wire order. */
const contribution = (msg: TranscriptMessage, budget: number): string => {
  if (msg.role === 'tool') {
    return lastChars(msg.text ?? '', budget).trim()
  }

  const parts = [lastChars(msg.reasoning_content ?? '', budget), lastChars(msg.text ?? '', budget)]

  for (const call of msg.tool_calls ?? []) {
    parts.push(formatToolCall(call.name, callSubject(call.arguments)))
  }

  return parts
    .map(part => part.trim())
    .filter(Boolean)
    .join(SEP)
}

/**
 * The last `width` cells of everything the node has produced.
 *
 * Walked from the newest message backwards and stopped as soon as the budget is
 * covered: the transcript is capped at 400 messages server-side and this is
 * re-derived twice a second, so the cost has to follow the row width rather than
 * the run's length.
 */
export const dagStreamTail = (messages: readonly TranscriptMessage[], width: number): string => {
  if (width <= 0) {
    return ''
  }

  // `dag.node` brackets a node's transcript with its prompt as a leading
  // `role: 'user'` row (raven/rpc/methods/dag.py `_with_messages`). The row
  // above this one already shows that prompt, so a node with nothing of its
  // own would otherwise echo it back forever as if it were new.
  const body = messages.length > 0 && messages[0]!.role === 'user' ? messages.slice(1) : messages

  const budget = width * CHARS_PER_CELL
  let acc = ''

  for (let i = body.length - 1; i >= 0; i--) {
    const part = contribution(body[i]!, budget)

    if (!part) {
      continue
    }

    acc = acc ? `${part}${SEP}${acc}` : part

    if (stringWidth(acc) >= width) {
      break
    }
  }

  return clipToWidthFromEnd(acc, width)
}

// What the box's own rendering does, so the fit is measured against the same
// thing it draws: a `MessageLine` with no details expanded.
const TRACE_ESTIMATE = { compact: false, details: false }

/**
 * The newest wire messages that fit `rows` once folded, and how many wire
 * messages were left out.
 *
 * Grows a trailing slice of the wire messages and folds each candidate slice
 * before measuring it, rather than folding once and slicing the result:
 * folding can collapse a whole assistant/tool run into a single episode, so
 * slicing already-folded messages could only pick whole episodes and would
 * show the head of a run whose folded height alone exceeds the budget.
 *
 * Measured with the transcript's own estimator rather than a second one, so a
 * change to how a message renders cannot make the box overflow the height the
 * virtualizer was told to expect.
 *
 * One folded result taller than the whole budget is still shown. Truncating
 * it would leave the box empty for exactly the run whose output a reader most
 * wants, and the Box clips the overflow either way.
 *
 * The search is bounded to at most `DAG_TRACE_FIT_MAX_ROWS` trailing wire
 * messages: the fold is not monotonic in the slice size, so a trace shaped
 * like a terse sub-agent's could otherwise walk the full transcript on every
 * poll tick without ever overflowing.
 */
export const fitTraceTail = (
  messages: readonly TranscriptMessage[],
  rows: number,
  cols: number
): { hidden: number; shown: Msg[] } => {
  if (messages.length === 0) {
    return { hidden: 0, shown: [] }
  }

  let shown: Msg[] = []
  let taken = 0

  // Grown from the end rather than picked per message: folding collapses an
  // assistant/tool run into one episode, so choosing whole folded messages
  // cannot follow a tail -- it would hand the box a single item taller than
  // itself and show its head. The fold is not monotonic in the slice size:
  // an un-narrated run of tool calls collapses to one row no matter how many
  // calls it holds, and a content-empty or orphaned row folds to nothing, so
  // a longer slice is not guaranteed to overflow first. DAG_TRACE_FIT_MAX_ROWS
  // is the actual bound on this loop, not the overflow check below.
  const limit = Math.min(messages.length, DAG_TRACE_FIT_MAX_ROWS)

  for (let take = 1; take <= limit; take++) {
    // Always `openTurn`: this is a tail slice, so the changes it can see are a
    // fraction of the run's by construction, and the box is six rows -- a shelf
    // naming that fraction would cost a real step its row.
    const folded = toTranscriptMessages(messages.slice(-take), { openTurn: true })
    const used = folded.reduce((total, msg) => total + estimatedMsgHeight(msg, cols, TRACE_ESTIMATE), 0)

    if (used > rows && taken > 0) {
      break
    }

    shown = folded
    taken = take
  }

  return { hidden: messages.length - taken, shown }
}
