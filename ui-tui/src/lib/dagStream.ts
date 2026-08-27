// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A DAG node's live transcript, reduced to what will fit in the box its row
// opens into.
//
// Pure, and takes its budget as an argument: the box's fixed row count is the
// only thing that decides how much of a run is legible, and a reduction that
// read that from a store could not be tested against a known budget.

import type { TranscriptMessage } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { DAG_TRACE_FIT_MAX_ROWS } from '../config/limits.js'
import { callSubject } from '../domain/episodeFold.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { clipToWidth, clipToWidthFromEnd, formatToolCall, hasAnsi, stripAnsi } from './text.js'
import { estimatedMsgHeight } from './virtualHeights.js'

// What the box's own rendering does, so the fit is measured against the same
// thing it draws: a `MessageLine` with no details expanded, dense -- the
// breathing rows between segments are blank lines a fixed-height box cannot
// afford, so the boxes render without them and this measures without them.
const TRACE_ESTIMATE = { compact: false, dense: true, details: false }

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
/**
 * One message cut down to `rows`, oldest lines first.
 *
 * The box has a fixed height and this fork does not truncate a child that
 * overflows it -- it squeezes the column, dropping scattered lines and painting
 * the last one over the footer, which reads as a corrupted box rather than a
 * full one. So nothing oversized may be handed to it: a single message taller
 * than the whole box is cut here instead, from its head, since the tail is the
 * part a reader opened the node to see. The `…` says so.
 */
const trimToRows = (msg: Msg, rows: number, cols: number): Msg => {
  const height = (text: string) => estimatedMsgHeight({ ...msg, text }, cols, TRACE_ESTIMATE)
  const lines = (msg.text ?? '').split('\n')

  let from = 0

  while (from < lines.length - 1 && height(lines.slice(from).join('\n')) > rows) {
    from += 1
  }

  let text = lines.slice(from).join('\n')

  // One line long enough to wrap past the box on its own: keep its tail.
  while (text.length > cols && height(text) > rows) {
    text = text.slice(Math.max(1, Math.ceil(cols / 2)))
  }

  return { ...msg, text: from > 0 || text !== msg.text ? `\u2026${text}` : text }
}

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

  const height = (msg: Msg) => estimatedMsgHeight(msg, cols, TRACE_ESTIMATE)

  // Only the one-message case can reach here oversized: the loop above keeps the
  // last slice that fit, and its first iteration is taken unconditionally.
  if (shown.reduce((total, msg) => total + height(msg), 0) > rows) {
    shown = [trimToRows(shown[shown.length - 1]!, rows, cols)]
  }

  return { hidden: messages.length - taken, shown }
}

// The last line of a block that says something. A step's newest words are at
// its end, and the blank line a model leaves before a heading would otherwise
// win the row.
const lastLine = (raw: string): string => {
  const lines = (hasAnsi(raw) ? stripAnsi(raw) : raw).split('\n')

  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]!

    if (line.trim()) {
      return line
    }
  }

  return ''
}

// A live line is clipped from its left: the stream grows at the right, so
// keeping the head would freeze the row on words the run has already left
// behind, and keeping the tail is what makes it read as moving. A settled run
// has no edge to follow, and its head is the part that says what happened.
const follow = (text: string, cols: number, live: boolean) =>
  live ? clipToWidthFromEnd(text, cols) : clipToWidth(text, cols)

/**
 * The newest thing a run said, as one line no wider than `cols`.
 *
 * What a folded trace box shows in place of the box: the tail of the run is the
 * only thing a single row can carry that a reader could not already get from
 * the header, and a row that moves says "still working" without spending a
 * spinner on it.
 *
 * Read off the wire messages rather than through `fitTraceTail`, which folds --
 * a fold collapses a whole assistant/tool run into one episode whose text is
 * empty, so the one row it yields for a working run is often blank.
 *
 * Newest first, inside one entry as well as across them: a step is dispatched
 * after the words that introduce it, and those after the thought that reached
 * them. Empty when the run has said nothing yet.
 */
export const traceTailLine = (messages: readonly TranscriptMessage[], cols: number, live = false): string => {
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]!

    // A turn the runtime opened: the model reads its text, a reader must not --
    // it carries an untrusted fence, an instance handle and an instruction not
    // to repeat either (see `TranscriptMessage.origin`).
    if (msg.role === 'user' && msg.origin) {
      continue
    }

    const calls = msg.tool_calls ?? []
    const call = calls[calls.length - 1]

    if (call) {
      return clipToWidth(formatToolCall(call.name, callSubject(call.arguments)), cols)
    }

    const text = lastLine(msg.text ?? '')

    if (text) {
      return follow(text, cols, live)
    }

    const thought = lastLine(msg.reasoning_content ?? '')

    if (thought) {
      return follow(thought, cols, live)
    }
  }

  return ''
}
