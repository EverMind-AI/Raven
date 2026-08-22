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
// The runtime hands over calls already named in Raven's own vocabulary (`exec`,
// `read_file`, ...) -- see `raven/agent/subagent/tool_vocabulary.py`, which maps
// them on the way out; the record underneath keeps the transport's own name --
// so the verb table in `episodeSummary.ts` applies unchanged. A name it has no
// entry for arrives as sent, and `ruleFor` humanises it.

import type { DirectTurn } from '../rpc/generated.js'
import type { Episode, EpisodeTool, Msg } from '../types.js'

// The runtime writes the subject of a call under its tool's own parameter name
// and puts it first. Reading known names before falling back to insertion order
// is what keeps a row correct for a log written by something else -- the main
// session log stores real Raven calls with these same names.
const SUBJECT_KEYS = ['command', 'path', 'pattern', 'query', 'url', 'file_path']

/** The one string a tool row shows beside its verb. */
export const callSubject = (argumentsJson: string): string => {
  let parsed: unknown

  try {
    parsed = JSON.parse(argumentsJson)
  } catch {
    // Not JSON at all: some transports pass the argument through as a bare
    // string, and showing it beats showing nothing.
    return argumentsJson.trim()
  }

  if (typeof parsed === 'string') {
    return parsed.trim()
  }

  if (parsed === null || typeof parsed !== 'object') {
    return ''
  }

  const fields = parsed as Record<string, unknown>

  for (const key of SUBJECT_KEYS) {
    const value = fields[key]

    if (typeof value === 'string' && value.trim()) {
      return value.trim()
    }
  }

  for (const value of Object.values(fields)) {
    if (typeof value === 'string' && value.trim()) {
      return value.trim()
    }
  }

  return ''
}

// The runtime marks a failed call by prefixing its result, which is also how it
// reaches the record on disk -- there is no separate status field on the row.
const FAILED = '[failed]'

const readResult = (content: string): { ok: boolean; text: string } =>
  content.startsWith(FAILED) ? { ok: false, text: content.slice(FAILED.length).trim() } : { ok: true, text: content }

/**
 * Fold stored rows into transcript messages.
 *
 * A user row opens a turn and the assistant rows until the next one close it,
 * which is what makes the result one `kind: 'episodes'` message per turn rather
 * than per row: the fold in `segmentTurn` spans episode boundaries, so a run of
 * six reads collapses to "read 6 files" only if they arrive in one message.
 */
export const foldDirectTurns = (turns: DirectTurn[]): Msg[] => {
  const msgs: Msg[] = []
  let episodes: Episode[] = []
  let answer = ''
  // The id of the row this message started from, carried onto the message. It is
  // what keys a fold: a turn still running is re-read every few hundred
  // milliseconds and handed over as a fresh object each time, so anything keyed
  // on the object closes what the reader opened. The row ids hold still for as
  // long as the turn does.
  let foldId: string | undefined
  const pending = new Map<string, EpisodeTool>()

  const flush = () => {
    if (episodes.length || answer) {
      msgs.push({ episodes, foldId, kind: 'episodes', role: 'assistant', text: answer })
    }

    episodes = []
    answer = ''
    foldId = undefined
    pending.clear()
  }

  for (const turn of turns) {
    if (turn.role === 'user') {
      flush()
      msgs.push({ role: 'user', text: turn.content })
      continue
    }

    foldId ??= turn.call_id

    if (turn.role === 'tool') {
      const tool = turn.tool_call_id ? pending.get(turn.tool_call_id) : undefined

      if (tool) {
        const { ok, text } = readResult(turn.content)
        tool.resultPreview = text
        tool.ok = ok
        // Both ends are real wall clocks from the record, so a folded stretch
        // can show what it actually cost instead of omitting the time.
        if (turn.at_ms && tool.startedAt) {
          tool.durationMs = Math.max(0, turn.at_ms - tool.startedAt)
        }
      }

      continue
    }

    const reasoning = turn.reasoning_content?.trim() ?? ''
    const calls = turn.tool_calls ?? []

    if (!calls.length) {
      // No call to hang an episode on. Prose here is the turn's answer; a row
      // carrying only a thought still gets an episode, or the last thought
      // before the reply would vanish.
      if (turn.content.trim()) {
        answer = answer ? `${answer}\n\n${turn.content.trim()}` : turn.content.trim()
      } else if (reasoning) {
        episodes.push({ index: episodes.length, reasoning, tools: [] })
      }

      continue
    }

    const tools = calls.map((call): EpisodeTool => {
      const tool: EpisodeTool = {
        id: call.id,
        name: call.name,
        summary: callSubject(call.arguments),
        ok: true,
        done: true,
        ...(turn.at_ms ? { startedAt: turn.at_ms } : {})
      }
      pending.set(call.id, tool)

      return tool
    })

    episodes.push({
      index: episodes.length,
      reasoning,
      // The runtime opens a calling row with empty content, so narration is
      // only set when the agent really did say something before acting.
      ...(turn.content.trim() ? { narration: turn.content.trim() } : {}),
      tools
    })
  }

  flush()

  return msgs
}
