import type { DirectTurn, InstanceCtx } from './types'

/* An instance's status words, which are NOT the run list's. The registry writes
   `idle | running | completed | failed | cancelled`, plus `pending` and
   `interrupted` from reconciliation; the run marker knows `run | queued | error`
   and treats everything else as settled. Handing one vocabulary to the other is
   how a running instance and a failed one both came to wear the green
   finished dot. */
const LIVE = new Set(['running', 'pending'])
const BAD = new Set(['failed', 'cancelled', 'interrupted', 'aborted'])

export type InstanceState = 'live' | 'bad' | 'idle' | 'done'

export function instanceState(status?: string): InstanceState {
  if (!status) return 'done'
  if (LIVE.has(status)) return 'live'
  if (BAD.has(status)) return 'bad'
  if (status === 'idle') return 'idle'
  return 'done'
}

/* The same state in the marker's vocabulary, so one glyph component serves both
   lists. `idle` maps to `queued` because an idle instance is one nothing has
   asked anything of yet -- the closest thing the marker can say. */
export function instanceMark(status?: string): string {
  const st = instanceState(status)
  if (st === 'live') return 'run'
  if (st === 'bad') return 'error'
  if (st === 'idle') return 'queued'
  return 'ok'
}

/* The status the renderer is handed for a row. Shared by the paint and by the
   poll that compares against what was painted: computing it twice is how the
   two come to disagree and reset the stage on every heartbeat. */
export function instanceCtxStatus(status?: string): string | undefined {
  return LIVE.has(status || '') ? 'run' : status
}

/* Adapt one instance's turns into the shape the transcript renderer takes.

   `subagents.instance.history` answers `{ turns }` and a turn's text is
   `content`; the renderer reads `messages` and `text`. Adapting here keeps the
   renderer the single place transcript rules live, and keeps a future mismatch
   a compile error rather than a panel that silently draws nothing.

   The renderer's `run` status is not decoration: it holds back a trailing
   assistant message while a turn is in flight, so the next read replaces that
   row instead of appending a second copy of it. A `live` turn is exactly that
   state, and the instance's own status is the fallback for a poll landing
   between the record being written and the turn being marked done. */
export function toInstanceCtx(turns: DirectTurn[] | undefined, status?: string): InstanceCtx {
  const rows = turns || []
  const live = rows.some((r) => r.live)
  return {
    status: live ? 'run' : instanceCtxStatus(status),
    messages: rows.map((r) => ({
      role: r.role,
      text: r.content,
      /* Only a clock that exists. `at_ms` is 0 for a row nothing stamped -- a
         step read off a transport's own transcript, or the question of a turn
         still running -- and 0 is a perfectly valid instant, so handing it over
         printed "1970-01-01 08:00" under the message. Left out, `stamp` gets
         undefined, reads it as an invalid date and prints nothing, which is
         what "nobody recorded when" should look like. */
      ...(r.at_ms > 0 ? { timestamp: r.at_ms } : {}),
      ...(r.tool_call_id ? { tool_call_id: r.tool_call_id } : {}),
      ...(r.reasoning_content ? { reasoning_content: r.reasoning_content } : {}),
      ...(r.tool_calls
        ? { tool_calls: r.tool_calls.map((c) => ({ id: c.id, name: c.name, arguments: c.arguments })) }
        : {}),
    })),
  }
}
