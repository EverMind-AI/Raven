// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Stored rows folded into the transcript's episode shape.
//
// Two callers reach this: a direct chat's instance log, and the main session's
// resume payload. They arrive in different wire shapes and mean the same thing,
// so each adapts to `FoldRow` and the folding itself happens once. A second
// implementation would drift, and the drift would show up as a resumed
// transcript that folds or expands differently from the live one -- which is
// the whole property this module exists to hold.

import type { Episode, EpisodeTool, Msg } from '../types.js'

export interface FoldCall {
  arguments: string
  id: string
  name: string
}

export interface FoldRow {
  /** Wall clock, when the source has one. Used only to derive a duration. */
  atMs?: number
  calls?: readonly FoldCall[]
  /** Known call duration. Preferred over deriving one from `atMs`. */
  durationMs?: number
  /** The id a fold keys on, seeded from the first call of the turn. */
  foldSeed?: string
  /** A `tool` row's own verb, for when no earlier row announced its call. */
  name?: string
  ok?: boolean
  /** Pushed verbatim in place of `{ role, text }`, for a caller that already
   *  built the Msg itself (e.g. a turn-artifact summary). */
  passthrough?: Msg
  reasoning?: string
  /** Wall time spent thinking before this row's first call or its reply. */
  reasoningMs?: number
  role: 'assistant' | 'system' | 'tool' | 'user'
  /** A `tool` row's own subject, for that same unclaimed case. */
  summary?: string
  text: string
  toolCallId?: string
}

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

// The runtime marks a failed call by prefixing its result -- see
// raven/agent/subagent/backends/turn_rows.py. Exported so `directEpisodes.ts`
// reads the same literal instead of keeping a second copy that could drift.
export const FAILED_MARKER = '[failed]'

// Written in place of a result when a turn is interrupted mid-call -- see
// raven/agent/loop/main.py's open-call-interrupt handler. Live never shows
// this: a `tool.complete` event can only carry a call that actually
// completed, so only a stored/resumed row ever has it.
const INTERRUPTED_MARKER = '[interrupted]'

// Matches raven/agent/loop/main.py's `_TOOL_PREVIEW_MAX_CHARS`: live clamps a
// tool result to this many characters before a client ever sees it. Exported
// so the resume adapter -- the one caller handed the full, unclamped stored
// text -- can match that same limit instead of picking its own.
export const TOOL_PREVIEW_MAX_CHARS = 4_000

// The exact suffix chatStream.ts's onToolComplete appends when live reports a
// cut preview. Exported so a resumed row's clamp reads the same literal
// instead of a second copy that could drift from it.
export const TOOL_PREVIEW_TRUNCATED_SUFFIX = ' (truncated)'

/**
 * Clamp a tool result to live's own preview limit, marked the same way live
 * marks one it cut.
 *
 * Only the resume adapter calls this. A direct-chat row is never clamped
 * upstream to this limit (see raven/agent/subagent/backends/turn_rows.py and
 * its callers), so applying this inside the shared fold would cut a
 * direct-chat preview no live path ever cuts.
 */
export const clampToolPreview = (text: string): string =>
  text.length > TOOL_PREVIEW_MAX_CHARS
    ? `${text.slice(0, TOOL_PREVIEW_MAX_CHARS)}${TOOL_PREVIEW_TRUNCATED_SUFFIX}`
    : text

/**
 * A tool row's status and displayed text, from either an explicit `ok` or a
 * runtime-written marker.
 *
 * The direct-chat adapter always sets `ok` itself, so the marker check below
 * never fires for it -- one function serves both without direct-chat having
 * to know this exists.
 */
const deriveResult = (row: FoldRow): { ok: boolean; text: string } => {
  if (row.ok !== undefined) {
    return { ok: row.ok, text: row.text }
  }

  for (const marker of [FAILED_MARKER, INTERRUPTED_MARKER]) {
    if (row.text.startsWith(marker)) {
      return { ok: false, text: row.text.slice(marker.length).trim() }
    }
  }

  return { ok: true, text: row.text }
}

/**
 * Fold rows into transcript messages.
 *
 * A user row opens a turn and the assistant rows until the next one close it,
 * so the result is one `kind: 'episodes'` message per turn rather than per row:
 * `segmentTurn`'s fold spans episode boundaries, and a run of six reads only
 * collapses to "read 6 files" when they arrive in one message.
 */
export const foldRowsIntoEpisodes = (rows: readonly FoldRow[]): Msg[] => {
  const msgs: Msg[] = []
  let episodes: Episode[] = []
  let answer = ''
  // Carried onto the message and used as its fold key. A turn still running is
  // handed over as a fresh object every few hundred milliseconds, so a fold
  // keyed on the object closes itself; the call ids hold still.
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

  for (const [rowIndex, row] of rows.entries()) {
    // A system row is a delivered note, not a turn: the resume path hands one
    // over in place of a runtime-opened user row, or to carry a turn-artifact
    // summary verbatim via `passthrough`, so it closes whatever turn is open
    // and passes through the same way a user row does.
    if (row.role === 'user' || row.role === 'system') {
      flush()
      msgs.push(row.passthrough ?? { role: row.role, text: row.text })

      continue
    }

    foldId ??= row.foldSeed

    if (row.role === 'tool') {
      const tool = row.toolCallId ? pending.get(row.toolCallId) : undefined

      if (tool) {
        const { ok, text } = deriveResult(row)

        tool.resultPreview = text
        tool.ok = ok

        const derived = row.atMs && tool.startedAt ? Math.max(0, row.atMs - tool.startedAt) : undefined
        const durationMs = row.durationMs ?? derived

        if (durationMs !== undefined) {
          tool.durationMs = durationMs
        }
      } else if (row.name) {
        // No earlier row registered this call -- a stale resume payload can drop
        // the announcement while keeping its result. Standing the result up as
        // its own episode beats losing a tool the agent actually ran.
        const { ok, text } = deriveResult(row)

        episodes.push({
          index: episodes.length,
          reasoning: '',
          tools: [
            {
              done: true,
              id: row.toolCallId ?? `orphan-${rowIndex}`,
              name: row.name,
              ok,
              resultPreview: text,
              summary: row.summary ?? '',
              ...(row.durationMs != null ? { durationMs: row.durationMs } : {})
            }
          ]
        })
      }

      continue
    }

    const reasoning = row.reasoning?.trim() ?? ''
    const calls = row.calls ?? []

    if (!calls.length) {
      // No call to hang an episode on. Prose here is the turn's answer; a row
      // carrying only a thought still gets an episode, or the last thought
      // before the reply would vanish.
      if (row.text.trim()) {
        answer = answer ? `${answer}\n\n${row.text.trim()}` : row.text.trim()
      } else if (reasoning) {
        episodes.push({
          index: episodes.length,
          reasoning,
          tools: [],
          ...(row.reasoningMs != null ? { reasoningMs: row.reasoningMs } : {})
        })
      }

      continue
    }

    const tools = calls.map((call): EpisodeTool => {
      const tool: EpisodeTool = {
        done: true,
        id: call.id,
        name: call.name,
        ok: true,
        summary: callSubject(call.arguments),
        ...(row.atMs ? { startedAt: row.atMs } : {})
      }

      pending.set(call.id, tool)

      return tool
    })

    episodes.push({
      index: episodes.length,
      reasoning,
      // The runtime opens a calling row with empty content, so narration is
      // only set when the agent really did say something before acting.
      ...(row.text.trim() ? { narration: row.text.trim() } : {}),
      ...(row.reasoningMs != null ? { reasoningMs: row.reasoningMs } : {}),
      tools
    })
  }

  flush()

  return msgs
}
