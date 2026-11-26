/* Which running subagents the tail row draws as orbs, in what order, wearing
   which mark. */
import { describe, expect, it } from 'vitest'

import { orbsOf } from './subagentOrbs'

import type { SubagentRow } from '../features/subagents/types'
import type { TaskRow } from '../features/tasks/types'

const node = (node_id: string, agent: string, status: string, started_at: number | null = null) =>
  ({ node_id, agent, status, started_at, depends_on: [], files: [] })

const spawn = (id: string, agent: string, status: string, started_at: number | null = 5): TaskRow =>
  ({ kind: 'spawn', id, status: 'running', agent, counts: {}, nodes: [node('n', agent, status, started_at)] }) as unknown as TaskRow

const dag = (id: string, nodes: ReturnType<typeof node>[]): TaskRow =>
  ({ kind: 'dag', id, status: 'running', agent: null, counts: {}, nodes }) as unknown as TaskRow

describe('the orbs of the running subagents', () => {
  it('gives a spawn one orb once its node runs', () => {
    expect(orbsOf([spawn('c1', 'Codex', 'running')], [])).toEqual([
      { key: 'c1', name: 'Codex', preset: null, own: false, startedAt: 5 },
    ])
    expect(orbsOf([spawn('c1', 'Codex', 'pending')], [])).toEqual([])
  })

  it('gives a graph one orb per running node', () => {
    const run = dag('r', [node('a', 'Codex', 'running', 1), node('b', 'Codex', 'pending'), node('c', 'DSH', 'running', 2)])
    expect(orbsOf([run], []).map((o) => o.key)).toEqual(['r:a', 'r:c'])
  })

  it('orders by start time, unknown last', () => {
    const run = dag('r', [node('a', 'A', 'running', 9), node('b', 'B', 'running', null), node('c', 'C', 'running', 3)])
    expect(orbsOf([run], []).map((o) => o.startedAt)).toEqual([3, 9, null])
  })

  it('takes the brand from the roster', () => {
    const roster = [
      { name: 'Codex', preset: 'codex' },
      { name: 'Raven-Research', builtin: false, vendored: true },
    ] as SubagentRow[]
    const run = dag('r', [
      node('a', 'Codex', 'running', 1), node('b', 'Raven-Research', 'running', 2),
      node('c', 'Raven-Code', 'running', 3), node('d', 'my-bot', 'running', 4),
    ])
    expect(orbsOf([run], roster).map(({ name, preset, own }) => ({ name, preset, own }))).toEqual([
      { name: 'Codex', preset: 'codex', own: false },
      { name: 'Raven-Research', preset: null, own: true },
      { name: 'Raven-Code', preset: null, own: true },
      { name: 'my-bot', preset: null, own: false },
    ])
  })
})
