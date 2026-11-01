/* The replay's clock, as properties: where it starts, how long its first pass
 * lasts, what counts as history and what as news, and what it plays after
 * that. The page only calls these once a frame; every rule about the playhead
 * is stated here. */

import { describe, expect, it } from 'vitest'

import { TRAIL_PAD, createAxis } from './axis'
import { entryEnd, entryStart } from './model'
import { CATCH_UP_RATE, REPLAY_MS, advance, entriesAt, startReplay } from './replay'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis } from './axis'
import type { Replay } from './replay'

const T0 = Date.parse('2026-10-28T10:00:00.000Z')
const s = (seconds: number): number => T0 + seconds * 1000
const iso = (seconds: number): string => new Date(s(seconds)).toISOString()

/** A step that ran from `from` to `to` seconds in. */
const step = (id: string, from: number, to: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id,
  revision: 1,
  kind: 'llm.output',
  span_name: 'llm.call',
  slot: 'llm.output',
  trace_id: 't',
  span_id: id,
  parent_span_id: null,
  turn_span_id: 'T1',
  turn_number: 1,
  turn_start: false,
  origin: 'main',
  subagent_scope: null,
  opens_subagent_scope: null,
  dispatched_by_subagent_scope: null,
  sort_key: [],
  event_time: iso(from),
  preview: null,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: iso(from),
  operation_end: iso(to),
  duration_ms: (to - from) * 1000,
  charged_ms: null,
  timing_basis: 'span_full',
  duration_owner: null,
  meta: {},
  ...over,
})

/** The axis the page draws these steps on. */
const axisOf = (entries: readonly TrajectoryEntry[], now: number): TimeAxis =>
  createAxis(entries.map((entry) => ({ start: entryStart(entry), end: entryEnd(entry, now) })), { pad: TRAIL_PAD })

/** Frames of `frame` ms until the replay ends: the wall time it took. */
function playOut(replay: Replay, entries: readonly TrajectoryEntry[], now: number, frame = 16): number {
  const axis = axisOf(entries, now)
  let current: Replay | null = replay
  let wall = 0
  while (current !== null && wall < 120_000) {
    current = advance(current, axis, frame, entries, now)
    wall += frame
  }
  return wall
}

describe('startReplay', () => {
  it('plays a session no longer than the budget in its own time, from its first instant', () => {
    const steps = [step('a', 0, 4), step('b', 6, 10)]
    expect(startReplay(axisOf(steps, s(100)), steps, s(100), s(100))).toEqual({ at: s(0), pressed: s(100), played: 0, duration: 10_000 })
  })

  it('holds a longer session to the budget', () => {
    const steps = [step('a', 0, 60)]
    expect(startReplay(axisOf(steps, s(100)), steps, s(100), s(100))!.duration).toBe(REPLAY_MS)
    expect(REPLAY_MS).toBe(15_000)
  })

  it('measures the session on the folded axis: an idle night costs its fold, not its hours', () => {
    /* Five seconds, two idle hours, five seconds. The fold is drawn a second
       wide, so the work replays in its own time and the night passes in one
       second rather than squeezing the work into a blink. */
    const steps = [step('a', 0, 5), step('b', 7205, 7210)]
    const axis = axisOf(steps, s(8000))
    expect(axis.gaps).toHaveLength(1)
    expect(startReplay(axis, steps, s(8000), s(8000))!.duration).toBe(11_000)
  })

  it('counts everything already on the page as history, even stamped after the page’s own clock', () => {
    const steps = [step('a', 0, 60)]
    expect(startReplay(axisOf(steps, s(30)), steps, s(30), s(30))!.pressed).toBe(s(60))
  })

  it('has nothing to replay when no step took any time', () => {
    const instant = [step('a', 3, 3)]
    expect(startReplay(axisOf(instant, s(10)), instant, s(10), s(10))).toBeNull()
    expect(startReplay(axisOf([], s(10)), [], s(10), s(10))).toBeNull()
  })
})

describe('advance', () => {
  it('moves the playhead through the session in step with the wall clock', () => {
    /* Sixty seconds over fifteen: each wall second is four of the session's. */
    const steps = [step('a', 0, 60)]
    const axis = axisOf(steps, s(100))
    const replay = startReplay(axis, steps, s(100), s(100))!
    expect(advance(replay, axis, 1000, steps, s(100))).toEqual({ at: s(4), pressed: s(100), played: 1000, duration: REPLAY_MS })
  })

  it('crosses a folded idle stretch at its folded width', () => {
    const steps = [step('a', 0, 5), step('b', 7205, 7210)]
    const axis = axisOf(steps, s(8000))
    /* Half the fold's second: half of the two idle hours. */
    const halfway = advance({ at: s(5), pressed: s(8000), played: 5000, duration: 11_000 }, axis, 500, steps, s(8000))
    expect(halfway!.at).toBe(s(3605))
  })

  it('ends with the first pass when nothing started after the press', () => {
    const steps = [step('a', 0, 60)]
    expect(advance({ at: s(59), pressed: s(100), played: 14_500, duration: REPLAY_MS }, axisOf(steps, s(100)), 1000, steps, s(100))).toBeNull()
  })

  it('takes history that lands after the press into the first pass, which still ends on time', () => {
    /* Pressed with the first two minutes on the page; the rest of the hour
       lands a second later. It started before the press, so it is history:
       the pass stretches over it and still takes fifteen seconds in all. */
    const first = [step('a', 0, 120)]
    const all = [...first, step('b', 3000, 3600)]
    const pressed = s(3600)
    const early = advance(startReplay(axisOf(first, pressed), first, pressed, pressed)!, axisOf(first, pressed), 1000, first, pressed)!
    expect(early.pressed).toBe(pressed)
    const wall = 1000 + playOut(early, all, pressed)
    expect(wall).toBeGreaterThanOrEqual(REPLAY_MS)
    expect(wall).toBeLessThanOrEqual(REPLAY_MS + 16)
  })

  it('plays what started after the press at twice real time, from where that work starts', () => {
    const steps = [step('a', 0, 60), step('late', 100, 110)]
    const axis = axisOf(steps, s(120))
    const caught = advance({ at: s(59), pressed: s(70), played: 14_900, duration: REPLAY_MS }, axis, 1000, steps, s(120))
    expect(caught).toMatchObject({ at: s(100), pressed: null })
    expect(CATCH_UP_RATE).toBe(2)
    expect(advance(caught!, axis, 1000, steps, s(120))!.at).toBe(s(102))
    expect(advance({ ...caught!, at: s(108) }, axis, 1000, steps, s(120))).toBeNull()
  })

  it('carries on from the press when a step was still running then', () => {
    /* The session was live: a step running at the press goes on past it, so
       the catch-up starts where the first pass stopped and ends at the present. */
    const now = s(75)
    const steps = [step('a', 0, 60), step('live', 50, 74, { operation_status: 'running' })]
    expect(entryEnd(steps[1]!, now)).toBe(now)
    const axis = axisOf(steps, now)
    const caught = advance({ at: s(61), pressed: s(62), played: 14_990, duration: REPLAY_MS }, axis, 1000, steps, now)
    expect(caught).toMatchObject({ at: s(62), pressed: null })
    expect(advance(caught!, axis, 6000, steps, now)!.at).toBe(s(74))
    expect(advance({ ...caught!, at: s(74) }, axis, 1000, steps, now)).toBeNull()
  })

  it('leaves the idle wait before the press out of the first pass, even once new work arrives', () => {
    /* Ten seconds of work that ended half a minute before the press. A step
       that starts after the press is news: it must not drag that idle half
       minute into the first pass, which stays ten seconds long. */
    const steps = [step('a', 0, 10), step('news', 45, 50)]
    const axis = axisOf(steps, s(60))
    const replay = advance({ at: s(0), pressed: s(40), played: 0, duration: 10_000 }, axis, 1000, steps, s(60))
    expect(replay).toMatchObject({ at: s(1), duration: 10_000 })
  })

  it('never takes the playhead back when steps are taken back mid-way', () => {
    const steps = [step('a', 0, 30)]
    const held = advance({ at: s(29), pressed: s(100), played: 7000, duration: REPLAY_MS }, axisOf(steps, s(100)), 1000, steps, s(100))
    expect(held).toMatchObject({ at: s(29), played: 8000 })
  })

  it('takes the budget frame by frame however long the session ran, and its own time when shorter', () => {
    const hour = [step('a', 0, 3600), step('b', 1800, 1900)]
    const long = startReplay(axisOf(hour, s(4000)), hour, s(4000), s(4000))!
    expect(playOut(long, hour, s(4000))).toBeGreaterThanOrEqual(REPLAY_MS)
    expect(playOut(long, hour, s(4000))).toBeLessThanOrEqual(REPLAY_MS + 16)
    const short = [step('a', 0, 4), step('b', 6, 10)]
    const brief = startReplay(axisOf(short, s(100)), short, s(100), s(100))!
    expect(playOut(brief, short, s(100))).toBeGreaterThanOrEqual(10_000)
    expect(playOut(brief, short, s(100))).toBeLessThanOrEqual(10_016)
  })
})

describe('entriesAt', () => {
  it('leaves out what had not started and keeps what had finished as it is', () => {
    const steps = [step('a', 0, 1), step('b', 5, 6)]
    const shown = entriesAt(steps, s(3), s(100))
    expect(shown).toHaveLength(1)
    expect(shown[0]).toBe(steps[0])
  })

  it('cuts a step still running at the playhead there, as running', () => {
    const shown = entriesAt([step('a', 2, 8)], s(5), s(100))
    expect(shown[0]!.operation_status).toBe('running')
    expect(entryEnd(shown[0]!, s(5))).toBe(s(5))
  })

  it('keeps a failure unsaid until the failed step has ended', () => {
    const failed = step('a', 2, 8, { operation_status: 'error', failure_entry: true })
    expect(entriesAt([failed], s(5), s(100))[0]).toMatchObject({ failure_entry: false, operation_status: 'running' })
    expect(entriesAt([failed], s(8), s(100))[0]).toBe(failed)
  })

  it('keeps the index’s order', () => {
    const steps = [step('b', 2, 3), step('a', 1, 4)]
    expect(entriesAt(steps, s(2.5), s(100)).map((entry) => entry.entry_id)).toEqual(['b', 'a'])
  })
})
