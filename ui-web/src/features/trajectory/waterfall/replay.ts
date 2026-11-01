/* A replay of the session: the waterfall drawn again from its first instant,
 * as it stood at each moment, on the axis the whole session is drawn on.
 *
 * The press divides the session in two. What had started by then is history,
 * however late it reaches the page -- a cold index hands a session over a page
 * at a time -- and the first pass plays all of it: in its own time when that is
 * no longer than REPLAY_MS, squeezed into exactly REPLAY_MS otherwise. The pass
 * is measured again every frame, so history that lands mid-way stretches what
 * it covers and never how long it takes. What starts after the press is new,
 * and is played after the first pass at CATCH_UP_RATE, from where that work
 * starts, until the playhead reaches the present.
 *
 * Lengths are taken on the axis's projected positions rather than real time,
 * so an idle stretch the axis folds is crossed at its folded width: a session
 * that slept overnight replays its work, not its night. The playhead itself is
 * kept in real time: a live session's axis changes under the replay, and an
 * instant stays the same instant on every axis the session draws.
 */

import { entryEnd, entryStart } from './model'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis } from './axis'

/** The longest the first pass takes, in wall-clock milliseconds. */
export const REPLAY_MS = 15_000
/** How fast what starts after the press is played, once the first pass is through. */
export const CATCH_UP_RATE = 2

export interface Replay {
  /** The playhead, in real time. */
  at: number
  /** Where the press divides the session, in real time: what had started by
   *  then is history. Null once the replay is catching up with what is new. */
  pressed: number | null
  /** Wall-clock milliseconds the first pass has played. */
  played: number
  /** Wall-clock milliseconds the first pass lasts, as of the history on the page. */
  duration: number
}

/** Where the session ends now: its last step's end, a running one's at `now`. */
function sessionEnd(entries: readonly TrajectoryEntry[], now: number): number {
  let end = -Infinity
  for (const entry of entries) end = Math.max(end, entryEnd(entry, now))
  return end
}

/** Where the session stood at the press, by the history on the page now, and
 *  how far that is along the axis -- which a playhead cannot leave. */
function historyAt(axis: TimeAxis, entries: readonly TrajectoryEntry[], now: number, pressed: number): { until: number; length: number } {
  let until = -Infinity
  for (const entry of entries) {
    if (entryStart(entry) <= pressed) until = Math.max(until, Math.min(entryEnd(entry, now), pressed))
  }
  until = Math.min(until, axis.end)
  return { until, length: Number.isFinite(until) ? axis.project(until) - axis.project(axis.start) : 0 }
}

/** Where the work that came after `after` begins: there, when a step was
 *  still running at that instant; else the first start past it; null when
 *  nothing came after it at all. */
function resumeAt(entries: readonly TrajectoryEntry[], after: number, now: number): number | null {
  let next = Infinity
  for (const entry of entries) {
    const start = entryStart(entry)
    if (start > after) next = Math.min(next, start)
    else if (entryEnd(entry, now) > after) return after
  }
  return Number.isFinite(next) ? next : null
}

/** A replay of the session from its first instant, pressed at `clock`; null
 *  when no step took any time, so there is nothing to play. Everything on the
 *  page is history, even a step stamped after the page's own clock. */
export function startReplay(axis: TimeAxis, entries: readonly TrajectoryEntry[], now: number, clock: number): Replay | null {
  const pressed = Math.max(clock, sessionEnd(entries, now))
  const { length } = historyAt(axis, entries, now, pressed)
  if (!(axis.end > axis.start) || !(length > 0)) return null
  return { at: axis.start, pressed, played: 0, duration: Math.min(length, REPLAY_MS) }
}

/** The replay `elapsed` wall-clock milliseconds on; null once it has played
 *  the history and caught up with whatever was new. */
export function advance(
  replay: Replay, axis: TimeAxis, elapsed: number, entries: readonly TrajectoryEntry[], now: number,
): Replay | null {
  const step = Math.max(0, elapsed)
  if (replay.pressed !== null) {
    const played = replay.played + step
    const { until, length } = historyAt(axis, entries, now, replay.pressed)
    const duration = Math.min(Math.max(0, length), REPLAY_MS)
    if (played < duration) {
      const at = Math.max(replay.at, axis.unproject(axis.project(axis.start) + (played * length) / duration))
      return { ...replay, at, played, duration }
    }
    /* The first pass is through: what started after the press, or ran past it, is new. */
    const resume = resumeAt(entries, Number.isFinite(until) ? until : replay.at, now)
    return resume === null ? null : { at: resume, pressed: null, played, duration }
  }
  const limit = Math.min(sessionEnd(entries, now), axis.end)
  const moved = axis.unproject(axis.project(replay.at) + step * CATCH_UP_RATE)
  const at = Math.max(replay.at, Math.min(limit, moved))
  return at < limit ? { ...replay, at } : null
}

/** One step as it stood at `at`: none before it started, and cut there,
 *  running, while it still ran -- a failure it ended in had not happened yet.
 *  A finished step is passed through as it is. */
export function entryAt(entry: TrajectoryEntry, at: number, now: number): TrajectoryEntry | null {
  if (entryStart(entry) > at) return null
  return entryEnd(entry, now) <= at ? entry : { ...entry, operation_status: 'running', operation_end: null, failure_entry: false }
}

/** The session as it stood at `at`, step by step (`entryAt`), in the index's order. */
export function entriesAt(entries: readonly TrajectoryEntry[], at: number, now: number): TrajectoryEntry[] {
  const shown: TrajectoryEntry[] = []
  for (const entry of entries) {
    const seen = entryAt(entry, at, now)
    if (seen !== null) shown.push(seen)
  }
  return shown
}
