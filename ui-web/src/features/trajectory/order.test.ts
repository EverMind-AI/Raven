import { describe, expect, it } from 'vitest'

import { compareSortKey, indexOf, insertSorted, removeIds } from './order'

import type { TrajectoryEntry } from './types'

const entry = (id: string, key: TrajectoryEntry['sort_key']): TrajectoryEntry => ({
  entry_id: id,
  revision: 1,
  kind: 'tool.output',
  span_name: 'tool.call',
  slot: 'tool.output',
  trace_id: 't',
  span_id: id,
  parent_span_id: null,
  turn_span_id: 'turn',
  turn_number: 1,
  turn_start: false,
  origin: 'main',
  sort_key: key,
  event_time: '2026-01-01T00:00:00Z',
  preview: null,
  operation_status: 'ok',
  status_evidence: [],
  failure_entry: false,
  integrity: [],
  operation_start: null,
  operation_end: null,
  duration_ms: null,
  charged_ms: null,
  timing_basis: 'not_recorded',
  duration_owner: null,
  meta: {},
})

describe('compareSortKey', () => {
  it('orders element by element, numbers numerically and strings lexically', () => {
    expect(compareSortKey(['2026-01-01T00:00:01Z', 0], ['2026-01-01T00:00:02Z', 0])).toBeLessThan(0)
    expect(compareSortKey(['a', 2], ['a', 10])).toBeLessThan(0)
    expect(compareSortKey(['a', 10], ['a', 2])).toBeGreaterThan(0)
    expect(compareSortKey(['a', 1, 'x'], ['a', 1, 'x'])).toBe(0)
  })

  it('ranks null before a number before a string, and a shorter tuple first', () => {
    expect(compareSortKey([null], [0])).toBeLessThan(0)
    expect(compareSortKey([0], ['0'])).toBeLessThan(0)
    expect(compareSortKey(['a'], ['a', 0])).toBeLessThan(0)
  })
})

describe('insertSorted', () => {
  const base = [entry('a', ['t1', 0]), entry('c', ['t3', 0])]

  it('inserts by key and leaves the input untouched', () => {
    const next = insertSorted(base, entry('b', ['t2', 0]))
    expect(next.map((e) => e.entry_id)).toEqual(['a', 'b', 'c'])
    expect(base.map((e) => e.entry_id)).toEqual(['a', 'c'])
  })

  it('replaces the entry carrying the same id, wherever its key now puts it', () => {
    const moved = insertSorted(base, entry('a', ['t9', 0]))
    expect(moved.map((e) => e.entry_id)).toEqual(['c', 'a'])
    expect(moved).toHaveLength(2)
  })

  it('appends after an equal key, so a tie keeps arrival order', () => {
    const next = insertSorted(base, entry('a2', ['t1', 0]))
    expect(next.map((e) => e.entry_id)).toEqual(['a', 'a2', 'c'])
  })
})

describe('removeIds and indexOf', () => {
  it('drops the named ids and indexes what is left', () => {
    const list = [entry('a', ['1']), entry('b', ['2']), entry('c', ['3'])]
    const rest = removeIds(list, ['b', 'nope'])
    expect(rest.map((e) => e.entry_id)).toEqual(['a', 'c'])
    expect(indexOf(rest)).toEqual({ a: 0, c: 1 })
    expect(removeIds(list, [])).not.toBe(list)
  })
})
