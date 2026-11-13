/* The Lane Window: which stretch of a lane's turns is mounted.
 *
 * The store keeps every segment of a conversation; the stage draws only the
 * stretch between the segments named by the lane's `winFrom` and `winTo`, so a
 * long conversation costs the stage what a screenful of turns costs. The stretch
 * is counted in turns and always cut at a segment that opens one, because
 * `stageRows` gathers what lies between two openings into one card and a cut
 * anywhere else would split it. Both ends are ids rather than indexes, so turns
 * appended at the tail move neither; `from` 0 is the start of the lane, leading
 * notes included, and `to` 0 is its tail, the newest turn included.
 */

import type { AskData, DeliveredData, Seg } from './types'

/* What a freshly opened conversation mounts (and a jump, from the turn it lands
   on), how many more an edge brings in, and how far past the first a window may
   grow before the tail takes it back. */
export const TAIL_TURNS = 20
export const WIDEN_TURNS = 20
export const TRIM_SLACK = 20

/* `to` is exclusive: the opening of the first turn left out. */
export interface Win { from: number; to: number }

/** Where a turn begins: a question someone asked, or a delegated result
 *  re-entering, which opens one with no question typed. A message merged into
 *  the running turn opens nothing. */
export const opens = (seg: Seg): seg is AskData | DeliveredData =>
  (seg.kind === 'ask' && !seg.midTurn) || seg.kind === 'sdlv'

/** The index the mounted stretch starts at. An id the lane no longer holds
 *  mounts from the start rather than hiding what the window cannot place. */
export function startIndex(segs: readonly Seg[], from: number): number {
  if (!from) return 0
  return Math.max(0, segs.findIndex((seg) => seg.id === from))
}

/** The index the mounted stretch stops before; the lane's length for the tail,
 *  and for an id the lane no longer holds. */
export function endIndex(segs: readonly Seg[], to: number): number {
  if (!to) return segs.length
  const at = segs.findIndex((seg) => seg.id === to)
  return at < 0 ? segs.length : at
}

function openings(segs: readonly Seg[]): number[] {
  const at: number[] = []
  segs.forEach((seg, i) => { if (opens(seg)) at.push(i) })
  return at
}

/** The window holding the last `turns` turns, or the whole lane when it
 *  holds no more than that. */
export function tailFrom(segs: readonly Seg[], turns: number): number {
  const at = openings(segs)
  return at.length > turns ? segs[at[at.length - turns]!]!.id : 0
}

/** The start `turns` turns earlier than `from`. */
export function widenFrom(segs: readonly Seg[], from: number, turns: number): number {
  const start = startIndex(segs, from)
  const at = openings(segs).filter((i) => i < start)
  return at.length > turns ? segs[at[at.length - turns]!]!.id : 0
}

/** The end `turns` turns later than `to`. */
export function widenTo(segs: readonly Seg[], to: number, turns: number): number {
  if (!to) return 0
  const end = endIndex(segs, to)
  const at = openings(segs).filter((i) => i >= end)
  return at.length > turns ? segs[at[turns]!]!.id : 0
}

/** The window that mounts segment `id`: unchanged when it already does. Else
 *  `turns` turns from the one holding it -- a segment before the first opening
 *  counts as the first turn's -- joined to the window when the two meet, so a
 *  jump a few turns past either end grows the window rather than replacing it. */
export function revealWin(segs: readonly Seg[], win: Win, id: number, turns: number): Win {
  const idx = segs.findIndex((seg) => seg.id === id)
  const start = startIndex(segs, win.from)
  const end = endIndex(segs, win.to)
  if (idx < 0 || (idx >= start && idx < end)) return win
  const at = openings(segs)
  const k = Math.max(0, at.filter((i) => i <= idx).length - 1)
  const from = at.length && at[k]! <= idx ? segs[at[k]!]!.id : 0
  const to = k + turns < at.length ? segs[at[k + turns]!]!.id : 0
  if (idx < start) return endIndex(segs, to) >= start ? { from, to: win.to } : { from, to }
  return startIndex(segs, from) <= end ? { from: win.from, to } : { from, to }
}

/** How many turns the window mounts. */
export function mountedTurns(segs: readonly Seg[], win: Win): number {
  const start = startIndex(segs, win.from)
  const end = endIndex(segs, win.to)
  return openings(segs).filter((i) => i >= start && i < end).length
}
