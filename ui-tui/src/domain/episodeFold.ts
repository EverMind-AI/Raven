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
  /** A fold key for a turn whose rows carry no call of their own. Weaker than
   *  the call ids below: a source that mints it per read (the instance reader's
   *  `live-<n>` / `log-<n>` row ordinals) holds a fold across polls but not
   *  across the settle, so it is used only when the turn called nothing. */
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
  /** A `user` row that was a steer: words merged into the turn already running.
   *  It does not open a turn; it is drawn inside the one it landed in. */
  steer?: boolean
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

/** The model's own description of a call, when its arguments carry one.
 *
 * claude-agent-acp sends this both in `_meta.claudeCode.title` and in the call's
 * arguments; the arguments are the copy that already reaches a client, so that
 * is the one read here.
 */
export const callIntent = (argumentsJson: string): string => {
  let parsed: unknown

  try {
    parsed = JSON.parse(argumentsJson)
  } catch {
    return ''
  }

  if (parsed === null || typeof parsed !== 'object') {
    return ''
  }

  const value = (parsed as Record<string, unknown>).description

  return typeof value === 'string' ? value.trim() : ''
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

// A closing punctuation mark or a CJK character continues the sentence with no
// space; anything else was a new word, and the stripped rows lost the space
// between them.
const CONTINUES_DIRECTLY = /^[,.;:!?，。；：、）)\]】」』…\p{Script=Han}]/u

/** Rejoin prose a steer row split, restoring the space the stripped rows lost. */
export const joinProse = (before: string, after: string): string =>
  before.endsWith(' ') || CONTINUES_DIRECTLY.test(after) ? `${before}${after}` : `${before} ${after}`

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
  //
  // Two candidates, and the turn's first call id wins. That id is the
  // transport's own and is the same string in a live read and in the settled
  // one, which is what a fold opened mid-turn needs to survive the settle. A
  // `foldSeed` can be weaker than that -- see its note on `FoldRow` -- so it
  // stands in only for a turn that called nothing.
  let foldCall: string | undefined
  let foldSeed: string | undefined
  const pending = new Map<string, EpisodeTool>()
  // A steer waiting for its place. It is not drawn where its row sits -- that
  // is mid-sentence, wherever the stream happened to be -- but at the end of
  // the paragraph the agent was writing when it landed, so the prose reads
  // whole and the steer still sits between what came before and what it changed.
  let pendingSteer: { atMs?: number; text: string } | null = null

  const placeSteer = () => {
    if (pendingSteer === null) {
      return
    }

    episodes.push({
      index: episodes.length,
      steer: pendingSteer.text,
      tools: [],
      ...(pendingSteer.atMs ? { steerAtMs: pendingSteer.atMs } : {})
    })
    pendingSteer = null
  }

  // The prose so far becomes an episode of its own, so a steer (or the prose
  // that continues it) can follow it in order. Without this the answer text is
  // drawn after every episode, which would put the steer above words said
  // before it.
  const settleAnswer = () => {
    if (answer) {
      episodes.push({ index: episodes.length, narration: answer, tools: [] })
      answer = ''
    }
  }

  const flush = () => {
    placeSteer()

    if (episodes.length || answer) {
      msgs.push({ episodes, foldId: foldCall ?? foldSeed, kind: 'episodes', role: 'assistant', text: answer })
    }

    episodes = []
    answer = ''
    foldCall = undefined
    foldSeed = undefined
    pending.clear()
  }

  for (const [rowIndex, row] of rows.entries()) {
    // A system row is a delivered note, not a turn: the resume path hands one
    // over in place of a runtime-opened user row, or to carry a turn-artifact
    // summary verbatim via `passthrough`, so it closes whatever turn is open
    // and passes through the same way a user row does.
    if (row.role === 'user' && row.steer) {
      placeSteer()
      settleAnswer()
      pendingSteer = { text: row.text.trim(), ...(row.atMs ? { atMs: row.atMs } : {}) }

      continue
    }

    if (row.role === 'user' || row.role === 'system') {
      flush()
      msgs.push(row.passthrough ?? { role: row.role, text: row.text })

      continue
    }

    foldCall ??= row.calls?.[0]?.id
    foldSeed ??= row.foldSeed

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

    if (!calls.length && pendingSteer !== null && row.text.trim()) {
      // The first prose after a steer finishes the paragraph the steer cut
      // into: its head (up to the first paragraph break) rejoins what was
      // being said, the steer follows, and the rest continues below it.
      const text = row.text.trim()
      const cut = text.indexOf('\n\n')
      const head = cut === -1 ? text : text.slice(0, cut)
      const rest = cut === -1 ? '' : text.slice(cut + 2).trim()
      const prev = episodes[episodes.length - 1]

      if (head) {
        if (prev && prev.narration !== undefined && prev.tools.length === 0 && prev.steer === undefined) {
          prev.narration = joinProse(prev.narration, head)
        } else {
          episodes.push({ index: episodes.length, narration: head, tools: [] })
        }
      }

      placeSteer()

      if (reasoning) {
        episodes.push({
          index: episodes.length,
          reasoning,
          tools: [],
          ...(row.reasoningMs != null ? { reasoningMs: row.reasoningMs } : {})
        })
      }

      if (rest) {
        answer = rest
      }

      continue
    }

    if (!calls.length) {
      // No call to hang an episode on. Prose here is the turn's answer, and a
      // thought still gets an episode of its own whether or not prose came with
      // it: the reply's own reasoning -- often the longest of the turn -- is
      // written on the reply row, and dropping it there hid a minute of thinking
      // behind the answer it produced.
      placeSteer()

      if (reasoning) {
        episodes.push({
          index: episodes.length,
          reasoning,
          tools: [],
          ...(row.reasoningMs != null ? { reasoningMs: row.reasoningMs } : {})
        })
      }

      if (row.text.trim()) {
        answer = answer ? `${answer}\n\n${row.text.trim()}` : row.text.trim()
      }

      continue
    }

    // A steer the agent answered by acting rather than speaking sits before
    // the step it prompted.
    placeSteer()

    const tools = calls.map((call): EpisodeTool => {
      const intent = callIntent(call.arguments)
      const tool: EpisodeTool = {
        done: true,
        id: call.id,
        name: call.name,
        ok: true,
        summary: callSubject(call.arguments),
        ...(intent ? { intent } : {}),
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
