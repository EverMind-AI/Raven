/* The tasks store's half of the Task Board: reading it for the conversation on
   screen, the live event, ordering, and which Tasks a step or nothing places. */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { setCurrent } from '../../lib/session'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { TaskBoard, TaskGroup, TaskRow, TasksBoardResult, TasksSource } from './types'

const group = (id: string, created: number, status: TaskGroup['status'] = 'active', tasks: TaskGroup['steps'][number]['tasks'] = []): TaskGroup => ({
  id, title: `Matter ${id}`, status, created_at: created, updated_at: created,
  steps: [{ id: `s_${id}`, turn_at: '2026-10-29T10:02:13', turn_id: null, text: `step of ${id}`, tasks }],
})

const board = (groups: TaskGroup[]): TaskBoard => ({ version: 1, groups, unassigned: [] })

const row = (kind: TaskRow['kind'], id: string): TaskRow => ({
  id, kind, task_summary: id, status: 'running', started_at: null, ended_at: null, agent: null, handle: null,
  counts: { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0, not_executed: 0 },
  nodes: [],
})

let answers: Record<string, TasksBoardResult> = {}
let asked: string[] = []

const source: TasksSource = {
  list: async () => [],
  one: async () => null,
  stop: async () => true,
  node: async () => ({ dispatch: null, steps: [], answer: null, outputTruncated: false }),
  roster: async () => [],
  board: async (key) => { asked.push(key); return answers[key] ?? { board: board([]), lineage: {} } },
}

beforeEach(() => {
  answers = {}
  asked = []
  store._resetForTests()
  source.board = async (key) => { asked.push(key); return answers[key] ?? { board: board([]), lineage: {} } }
  setSources({ tasks: source })
  setCurrent('s1')
})

afterEach(() => {
  resetSources()
  setCurrent(null)
})

describe('refreshBoard', () => {
  it('reads the board of the conversation on screen', async () => {
    answers.s1 = { board: board([group('g_1', 1)]), lineage: { RUN_B: 'RUN_A' } }
    await store.refreshBoard()
    expect(store.get().board?.groups.map((g) => g.id)).toEqual(['g_1'])
    expect(store.get().lineage).toEqual({ RUN_B: 'RUN_A' })
    expect(store.get().boardLoaded).toBe(true)
  })

  it('drops a board answer that lands after the reader switched conversation', async () => {
    let release: (v: TasksBoardResult) => void = () => {}
    source.board = () => new Promise((resolve) => { release = resolve })
    const pending = store.refreshBoard()
    setCurrent('s2')
    release({ board: board([group('g_old', 1)]), lineage: {} })
    await pending
    expect(store.get().board).toBeNull()
  })
})

describe('onBoardUpdated', () => {
  it('replaces the board for the open conversation only', () => {
    store.onBoardUpdated({ session_key: 's2', board: board([group('g_other', 1)]), lineage: {} })
    expect(store.get().board).toBeNull()
    store.onBoardUpdated({ session_key: 's1', board: board([group('g_mine', 1)]), lineage: {} })
    expect(store.get().board?.groups[0]?.id).toBe('g_mine')
  })
})

describe('ordering', () => {
  it('orders open and completed groups by created_at, newest first', () => {
    const b = board([group('a', 1), group('b', 3, 'completed'), group('c', 2, 'waiting'), group('d', 4, 'completed')])
    expect(store.openGroups(b).map((g) => g.id)).toEqual(['c', 'a'])
    expect(store.completedGroups(b).map((g) => g.id)).toEqual(['d', 'b'])
  })
})

describe('placement', () => {
  const placed = board([group('g_1', 1, 'active', [{ kind: 'dag', id: 'RUN_A', summary: 'Review' }])])

  it('links a step to its rows, each descendant after the run it continues', () => {
    const rows = [row('dag', 'RUN_B'), row('dag', 'RUN_A'), row('spawn', 'other')]
    const linked = store.linkedRows(placed.groups[0]!.steps[0]!, rows, { RUN_B: 'RUN_A' })
    expect(linked.map((l) => [l.link.id, l.row?.id ?? null])).toEqual([['RUN_A', 'RUN_A'], ['RUN_B', 'RUN_B']])
  })

  it('keeps a link whose row is gone, with no row', () => {
    const linked = store.linkedRows(placed.groups[0]!.steps[0]!, [], {})
    expect(linked).toEqual([{ link: { kind: 'dag', id: 'RUN_A', summary: 'Review' }, row: null }])
  })

  it('counts a lineage-placed row as placed and leaves the rest unplaced', () => {
    const rows = [row('dag', 'RUN_A'), row('dag', 'RUN_B'), row('spawn', 'loose')]
    expect(store.unplacedRows(rows, placed, { RUN_B: 'RUN_A' }).map((r) => r.id)).toEqual(['loose'])
  })

  it('treats every row as unplaced when there is no board', () => {
    const rows = [row('dag', 'RUN_A'), row('spawn', 'loose')]
    expect(store.unplacedRows(rows, null, {}).map((r) => r.id)).toEqual(['RUN_A', 'loose'])
  })
})

describe('a new run nothing places', () => {
  it('re-reads the board once when the tasks list gains a dag row nothing places', async () => {
    answers.s1 = { board: placed(), lineage: {} }
    await store.refreshBoard()
    asked = []
    store.set({ ...store.get(), rows: [row('dag', 'RUN_A'), row('dag', 'RUN_NEW')] })
    await store.noticeUnplaced()
    await store.noticeUnplaced()
    expect(asked).toEqual(['s1'])
  })

  function placed(): TaskBoard {
    return board([group('g_1', 1, 'active', [{ kind: 'dag', id: 'RUN_A', summary: 'Review' }])])
  }
})
