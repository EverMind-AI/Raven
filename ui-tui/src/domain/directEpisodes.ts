// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One instance's stored conversation, folded into the transcript shape the main
// conversation uses.
//
// A direct chat used to map each stored row to one flat `Msg`, which put a
// tool's whole output in a bordered one-line box and the model's thought in a
// tool trail -- two renderers the main transcript does not use for this. The
// rows carry everything an `Episode` needs, so building them here means a direct
// chat folds, expands and reads exactly like a Raven turn, with no second
// renderer to keep in step.
//
// The runtime hands over each call under its own transport's name: raven's own
// tools keep raven's vocabulary, and codex and claude_code keep theirs, so a
// direct chat reads as a conversation with that agent. `ruleFor` in
// `episodeSummary.ts` consults one verb table per vocabulary, and humanises a
// name that no table claims.

import type { DirectTurn } from '../rpc/generated.js'
import type { Msg } from '../types.js'
import type { FoldRow } from './episodeFold.js'

import { FAILED_MARKER, foldRowsIntoEpisodes } from './episodeFold.js'
import { failedTurnLine } from './messages.js'

export { callSubject } from './episodeFold.js'

/**
 * The line a turn that died with its session reads by -- the same sentence the
 * main lane and a live direct turn say for a turn that ended without an
 * answer, rather than a fifth spelling of it. It carries no reason because
 * nothing recorded one: the process the turn was running in is gone. A fresh
 * row per fold is fine: `replaceRows` keeps a row's identity by its content,
 * and the sentence follows the locale on the first read after it changes.
 */
const interruptedNotice = (): Msg => ({ kind: 'slash', role: 'system', text: failedTurnLine('') })

/**
 * Fold an instance's stored rows into transcript messages.
 *
 * The runtime marks a failed call by prefixing its result, which is also how it
 * reaches the record on disk -- there is no separate status field on the row.
 * The shared core now strips this same marker for a resume row with no
 * explicit `ok` (see `deriveResult` in `episodeFold.ts`), but a direct-chat
 * row always sets `ok` itself below, which bypasses that check -- so the
 * prefix still has to be stripped here too, against the same exported literal
 * rather than a second copy that could drift from it.
 */
export const foldDirectTurns = (turns: DirectTurn[]): Msg[] => {
  const msgs = foldRowsIntoEpisodes(
    turns.map((turn): FoldRow => {
      const failed = turn.role === 'tool' && turn.content.startsWith(FAILED_MARKER)

      return {
        atMs: turn.at_ms,
        role: turn.role,
        text: failed ? turn.content.slice(FAILED_MARKER.length).trim() : turn.content,
        ...(turn.reasoning_content ? { reasoning: turn.reasoning_content } : {}),
        ...(turn.steer ? { steer: true } : {}),
        ...(turn.tool_calls ? { calls: turn.tool_calls } : {}),
        ...(turn.call_id ? { foldSeed: turn.call_id } : {}),
        ...(turn.tool_call_id ? { toolCallId: turn.tool_call_id } : {}),
        ...(turn.role === 'tool' ? { ok: !failed } : {})
      }
    })
  )

  // The server marks a trailing prompt whose turn died with its session --
  // nothing running now, no reply on record. One muted line says so, where a
  // bare unanswered question read as the view having lost the reply.
  return turns.some(t => t.interrupted) ? [...msgs, interruptedNotice()] : msgs
}
