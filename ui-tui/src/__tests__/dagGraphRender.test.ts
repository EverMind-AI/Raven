// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'

import { findCrossings, layoutDagGraph } from '../lib/dagGraphLayout.js'
import { renderDagGraph } from '../lib/dagGraphRender.js'

const node = (
  id: string,
  dependsOn: string[] = [],
  status: DagRunNodeStatus = 'completed',
  subagent = 'raven-code'
): DagRunNode => ({ id, subagent, dependsOn, status })

const draw = (nodes: DagRunNode[], width = 200) =>
  renderDagGraph(layoutDagGraph(nodes, { width })!)
    .map(row =>
      row
        .map(span => span.text)
        .join('')
        .replace(/\s+$/, '')
    )
    .join('\n')

const DIAMOND = [
  node('survey', [], 'completed', 'raven-research'),
  node('rfc', [], 'completed', 'raven-research'),
  node('bench', ['survey']),
  node('synth', ['survey', 'rfc']),
  node('report', ['bench', 'synth'])
]

// A junction glyph anywhere in a picture that has no genuine fork or join.
const JUNCTION_RE = /[┬┴├┤┼]/

// Deterministic PRNG (mulberry32) so the brute-force crossing sweep below is
// reproducible -- an unseeded generator would make a failure unreproducible.
const mulberry32 = (seed: number) => {
  let state = seed

  return () => {
    state |= 0
    state = (state + 0x6d2b79f5) | 0

    let t = Math.imul(state ^ (state >>> 15), 1 | state)

    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t

    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const randomDag = (rand: () => number, size: number): DagRunNode[] => {
  const ids = Array.from({ length: size }, (_, index) => String.fromCharCode(97 + index))

  return ids.map((id, index) =>
    node(
      id,
      ids.slice(0, index).filter(() => rand() < 0.4)
    )
  )
}

const charAt = (picture: ReturnType<typeof renderDagGraph>, x: number, y: number) =>
  picture[y]!.map(span => span.text).join('')[x]!

describe('renderDagGraph', () => {
  it('draws a diamond', () => {
    expect(draw(DIAMOND)).toBe(
      [
        '╭──────────────╮      ╭──────────────╮      ╭──────────────╮',
        '│ 1 ✓ research │──┬──▸│   3 ✓ code   │──┬──▸│   5 ✓ code   │',
        '╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯',
        '╭──────────────╮  │   ╭──────────────╮  │',
        '│ 2 ✓ research │──┴──▸│   4 ✓ code   │──╯',
        '╰──────────────╯      ╰──────────────╯'
      ].join('\n')
    )
  })

  it('draws a chain with no vertical at all', () => {
    expect(draw([node('a'), node('b', ['a'])])).toBe(
      ['╭──────────╮      ╭──────────╮', '│ 1 ✓ code │─────▸│ 2 ✓ code │', '╰──────────╯      ╰──────────╯'].join(
        '\n'
      )
    )
  })

  it('merges a fan-out and a fan-in into one trunk each', () => {
    const nodes = [
      node('p', [], 'completed', 'raven-research'),
      node('f1', ['p']),
      node('f2', ['p']),
      node('f3', ['p']),
      node('m', ['f1', 'f2', 'f3'])
    ]
    const geometry = layoutDagGraph(nodes, { width: 200 })!
    const out = renderDagGraph(geometry)
      .map(row =>
        row
          .map(span => span.text)
          .join('')
          .replace(/\s+$/, '')
      )
      .join('\n')

    // A branch leaving the fan-out trunk, and a branch entering the fan-in one.
    expect(out).toContain('├──▸')
    expect(out).toContain('──┤')
    // A genuine fan or join shares an endpoint, so it is not a crossing: the
    // picture draws every one of these edges and `drawn` still credits them.
    expect([...geometry.drawn].sort()).toEqual(['f1>m', 'f2>m', 'f3>m', 'p>f1', 'p>f2', 'p>f3'])
  })

  it('draws a crossing as a crossing, not a join, when two edges share no endpoint', () => {
    // `c>d` and `b>e` share no endpoint, yet `b`'s row coincides with `d`'s, so
    // `b>e`'s horizontal must pass `c>d`'s trunk on the way to `e`. OR-ing the
    // two segments' direction bits there used to draw a junction that read as
    // `b>d`, an edge this graph does not have.
    const nodes = [node('a'), node('b'), node('c'), node('d', ['c']), node('e', ['b']), node('f')]
    const geometry = layoutDagGraph(nodes, { width: 200 })!
    const out = renderDagGraph(geometry)
      .map(row => row.map(span => span.text).join(''))
      .join('\n')

    expect(out).not.toMatch(JUNCTION_RE)
    expect(geometry.drawn.has('c>d')).toBe(false)
    expect(geometry.drawn.has('b>e')).toBe(false)
  })

  it('runs a long edge along a row below the boxes', () => {
    const out = draw([node('a'), node('b', ['a']), node('c', ['b', 'a'])]).split('\n')

    expect(out[out.length - 1]).toMatch(/^ *╰─+╯$/)
  })

  it('shows the status glyph of every node', () => {
    const out = draw([
      node('a', [], 'completed'),
      node('b', ['a'], 'running'),
      node('c', ['b'], 'failed'),
      node('d', ['c'], 'skipped')
    ])

    expect(out).toContain('1 ✓')
    expect(out).toContain('2 ●')
    expect(out).toContain('3 ✗')
    expect(out).toContain('4 ⊘')
  })

  it('is byte-identical when rendered twice', () => {
    expect(draw(DIAMOND)).toBe(draw(DIAMOND))
  })

  it('keeps the geometry when only statuses change', () => {
    // This is the promise that the picture does not jump while a run advances.
    // Every status glyph is one cell wide, so a transition cannot move a box.
    const pin = (g: NonNullable<ReturnType<typeof layoutDagGraph>>) => ({
      boxes: g.boxes.map(({ nodeId, width, x, y }) => ({ nodeId, width, x, y })),
      height: g.height,
      segments: g.segments,
      width: g.width
    })
    const before = layoutDagGraph(DIAMOND, { width: 200 })!
    const after = layoutDagGraph(
      DIAMOND.map((item, index) => ({
        ...item,
        status: (index < 2 ? 'completed' : index < 4 ? 'running' : 'pending') as DagRunNodeStatus
      })),
      { width: 200 }
    )!

    expect(pin(after)).toEqual(pin(before))
  })

  it('tags each box span with its node, so the panel can colour and click it', () => {
    const spans = renderDagGraph(layoutDagGraph([node('a')], { width: 200 })!).flat()

    expect(spans.some(span => span.kind === 'border' && span.nodeId === 'a')).toBe(true)
    expect(spans.some(span => span.kind === 'glyph' && span.nodeId === 'a')).toBe(true)
    expect(spans.every(span => span.kind !== 'wire' || span.nodeId === undefined)).toBe(true)
  })

  it('emits the status glyph as its own span, so it alone can carry the status colour', () => {
    const glyphs = renderDagGraph(layoutDagGraph([node('a', [], 'failed')], { width: 200 })!)
      .flat()
      .filter(span => span.kind === 'glyph')

    expect(glyphs.map(span => span.text)).toEqual(['✗'])
  })

  it('emits one row per cell row of the geometry', () => {
    const geometry = layoutDagGraph(DIAMOND, { width: 200 })!

    expect(renderDagGraph(geometry)).toHaveLength(geometry.height)
  })

  it('pads every row to the full width, so a row cannot come out short', () => {
    const geometry = layoutDagGraph(DIAMOND, { width: 200 })!

    renderDagGraph(geometry).forEach(row =>
      expect(row.reduce((sum, span) => sum + span.text.length, 0)).toBe(geometry.width)
    )
  })

  it('never draws a junction at a cell two edges reach without sharing an endpoint', () => {
    const rand = mulberry32(20260823)
    let crossingsSeen = 0

    for (let trial = 0; trial < 300; trial += 1) {
      const size = 4 + Math.floor(rand() * 3)
      const geometry = layoutDagGraph(randomDag(rand, size), { width: 200 })

      if (!geometry) {
        continue
      }

      const { cells, edges } = findCrossings(geometry.segments)
      const picture = renderDagGraph(geometry)

      crossingsSeen += cells.length
      cells.forEach(({ x, y }) => expect(charAt(picture, x, y)).not.toMatch(JUNCTION_RE))
      edges.forEach(key => expect(geometry.drawn.has(key)).toBe(false))
    }

    // A sweep that never produces a crossing would pass vacuously.
    expect(crossingsSeen).toBeGreaterThan(0)
  })
})
