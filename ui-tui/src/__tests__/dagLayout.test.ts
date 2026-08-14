// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNode } from '../domain/dagRun.js'

import { layoutDag } from '../lib/dagLayout.js'

const node = (id: string, dependsOn: string[] = []): DagRunNode => ({
  id,
  subagent: 'echo',
  dependsOn,
  status: 'pending'
})

const ids = (levels: ReturnType<typeof layoutDag>): string[][] => levels.map(level => level.map(n => n.id))

describe('layoutDag', () => {
  it('puts a chain on one level per step', () => {
    expect(ids(layoutDag([node('a'), node('b', ['a']), node('c', ['b'])]))).toEqual([['a'], ['b'], ['c']])
  })

  it('puts independent nodes on the same level', () => {
    expect(ids(layoutDag([node('a'), node('b'), node('c')]))).toEqual([['a', 'b', 'c']])
  })

  it('lays a diamond out as fan-out then join', () => {
    const levels = layoutDag([node('a'), node('b', ['a']), node('c', ['a']), node('d', ['b', 'c'])])

    expect(ids(levels)).toEqual([['a'], ['b', 'c'], ['d']])
  })

  it('places a join below its deepest dependency, not its first', () => {
    // `join` depends on both a one-step and a two-step branch. Placing it one
    // below `a` would draw an edge pointing back up the graph.
    const levels = layoutDag([node('a'), node('mid', ['a']), node('join', ['a', 'mid'])])

    expect(ids(levels)).toEqual([['a'], ['mid'], ['join']])
  })

  it('keeps the submitted order within a level', () => {
    // The graph is re-laid out on every progress event; sorting by anything that
    // changes (status, id) would make rows jump as the run advances.
    expect(ids(layoutDag([node('z'), node('m'), node('a')]))).toEqual([['z', 'm', 'a']])
  })

  it('treats a dependency outside the graph as satisfied', () => {
    // Not only defensive: `depends_on` may name a node an earlier run of the
    // session completed, which by definition is not in this run's node list.
    // Waiting on it would leave the node unplaceable and drop it from the
    // drawing entirely.
    expect(ids(layoutDag([node('a', ['ghost'])]))).toEqual([['a']])
  })

  it('still emits every node of a cyclic graph', () => {
    // The backend rejects cycles before running, so this only guards the client
    // against a malformed frame -- but dropping nodes or looping forever while
    // drawing is worse than drawing an imperfect graph.
    const levels = layoutDag([node('a', ['b']), node('b', ['a'])])

    expect(ids(levels).flat().sort()).toEqual(['a', 'b'])
  })

  it('returns nothing for an empty graph', () => {
    expect(layoutDag([])).toEqual([])
  })
})
