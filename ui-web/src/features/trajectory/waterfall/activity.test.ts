/* The activity strip's arithmetic: calls bucketed in real time over the
 * folded axis, one count per call at its start. */

import { describe, expect, it } from 'vitest'

import { activity, bucketWidth, FOLD_PX, stripAxis } from './activity'
import { createAxis } from './axis'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis } from './axis'

const T0 = Date.parse('2026-10-28T10:00:00.000Z')
const at = (seconds: number): string => new Date(T0 + seconds * 1000).toISOString()

const E = (id: string, start: number, over: Partial<TrajectoryEntry> = {}): TrajectoryEntry => ({
  entry_id: id, revision: 1, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output', trace_id: 't', span_id: id,
  parent_span_id: null, turn_span_id: 'T1', turn_number: 1, turn_start: false, origin: 'main', subagent_scope: null,
  opens_subagent_scope: null, dispatched_by_subagent_scope: null, sort_key: [id, 0], event_time: at(start), preview: id,
  operation_status: 'ok', status_evidence: [], failure_entry: false, integrity: [], operation_start: at(start),
  operation_end: at(start + 5), duration_ms: 5000, charged_ms: 5000, timing_basis: 'span_full', duration_owner: id, meta: {},
  ...over,
})

describe('the activity buckets', () => {
  const axis = createAxis([{ start: T0, end: T0 + 3_600_000 }])

  it('counts a call once, in the bucket its first entry starts in', () => {
    const buckets = activity([
      E('ask', 10, { kind: 'llm.input', span_id: 'call' }), E('say', 70, { span_id: 'call' }),
      E('next', 200),
    ], axis, 60_000)
    expect(buckets.map((bucket) => [bucket.start - T0, bucket.llm])).toEqual([[0, 1], [180_000, 1]])
  })

  it('keeps model and tool calls apart, and marks a bucket that holds a failure', () => {
    const buckets = activity([
      E('m', 10), E('t', 20, { kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output' }),
      E('bad', 30, { kind: 'error', span_name: 'tool.call', slot: 'error', failure_entry: true }),
    ], axis, 60_000)
    expect(buckets).toEqual([{ start: T0, end: T0 + 60_000, llm: 1, tool: 1, failed: true }])
  })

  it('counts the calls of a row that belongs to no turn', () => {
    expect(activity([E('loose', 10, { turn_span_id: null })], axis, 60_000)[0]!.llm).toBe(1)
  })

  it('widens its buckets as the session grows, so a bar keeps a few pixels', () => {
    const hour = createAxis([{ start: T0, end: T0 + 3_600_000 }])
    const day = createAxis([{ start: T0, end: T0 + 86_400_000 }])
    expect(bucketWidth(hour, 1200)).toBe(30_000)
    expect(bucketWidth(day, 1200)).toBe(600_000)
  })
})

/* A session of busy minutes ten minutes apart: the rows' axis folds the idle
   stretch between each two. */
const rowsWith = (folds: number): TimeAxis =>
  createAxis(Array.from({ length: folds + 1 }, (_, i) => ({ start: T0 + i * 660_000, end: T0 + i * 660_000 + 60_000 })))

/* The share of the strip the session's working time is drawn on. */
const working = (axis: TimeAxis): number => 1 - axis.gaps.reduce((sum, gap) => sum + gap.to - gap.from, 0) / axis.length

describe('the strip\'s own axis', () => {
  it('gives each fold room for its length while the folds fit', () => {
    const axis = stripAxis(rowsWith(5), 1200)
    expect(axis.gaps).toHaveLength(5)
    for (const gap of axis.gaps) expect(((gap.to - gap.from) / axis.length) * 1200).toBeCloseTo(FOLD_PX, 6)
  })

  /* A strip just wide enough to give 27 folds their 36px each drew a
     46-hour session's working time on what was left, a few dozen pixels. */
  it('keeps the working time on two thirds of the strip or more, however many folds and whatever its width', () => {
    for (const folds of [1, 5, 10, 27, 60]) {
      expect(rowsWith(folds).gaps).toHaveLength(folds)
      for (const room of [600, 840, 1120, 1600]) {
        expect(working(stripAxis(rowsWith(folds), room)), `${folds} folds at ${room}px`).toBeGreaterThanOrEqual(2 / 3 - 1e-9)
      }
    }
  })

  it('never gives the working time less of the strip as the strip widens', () => {
    const rows = rowsWith(27)
    let last = 0
    for (let room = 600; room <= 1600; room += 10) {
      const px = working(stripAxis(rows, room)) * room
      expect(px, `${room}px`).toBeGreaterThanOrEqual(last - 1e-6)
      last = px
    }
  })
})

