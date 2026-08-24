// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'
import type { DagGraphGeometry } from '../lib/dagGraphLayout.js'

import { BOX_BAND_HEIGHT, layoutDagGraph } from '../lib/dagGraphLayout.js'

const node = (id: string, dependsOn: string[] = [], status: DagRunNodeStatus = 'completed'): DagRunNode => ({
  id,
  subagent: 'raven-code',
  dependsOn,
  status
})

const WIDE = { width: 200 }

const laid = (nodes: DagRunNode[], width = 200) => layoutDagGraph(nodes, { width })!

const boxOf = (geometry: DagGraphGeometry, id: string) => geometry.boxes.find(box => box.nodeId === id)!

const trunks = (geometry: DagGraphGeometry) =>
  new Set(geometry.segments.filter(segment => segment.x0 === segment.x1).map(segment => segment.x0))

describe('layoutDagGraph', () => {
  it('puts each step of a chain in its own column, all on one band', () => {
    const g = laid([node('a'), node('b', ['a']), node('c', ['b'])])

    expect(g.boxes.map(box => box.y)).toEqual([0, 0, 0])
    expect(boxOf(g, 'a').x).toBeLessThan(boxOf(g, 'b').x)
    expect(boxOf(g, 'b').x).toBeLessThan(boxOf(g, 'c').x)
    expect(g.height).toBe(BOX_BAND_HEIGHT)
  })

  it('stacks independent nodes into bands of one column', () => {
    const g = laid([node('a'), node('b'), node('c')])

    expect(g.boxes.map(box => box.x)).toEqual([0, 0, 0])
    expect(g.boxes.map(box => box.y)).toEqual([0, BOX_BAND_HEIGHT, BOX_BAND_HEIGHT * 2])
  })

  it('gives every box the same width, so the columns line up', () => {
    const g = laid([node('a'), node('bbbbbbbbbb', ['a'])])

    expect(new Set(g.boxes.map(box => box.width)).size).toBe(1)
  })

  it('draws an edge for every in-graph dependency', () => {
    const g = laid([node('a'), node('b'), node('c', ['a', 'b'])])

    expect([...g.drawn].sort()).toEqual(['a>c', 'b>c'])
  })

  it('leaves a dependency outside the run undrawn, so the row can still name it', () => {
    // `depends_on` may name a node an earlier run of the session completed. It
    // has no box, so it has no edge -- and the row has to say so in words.
    const g = laid([node('a', ['ghost'])])

    expect(g.drawn.size).toBe(0)
    expect(g.segments).toEqual([])
  })

  it('puts an arrowhead left of a node that has an incoming edge, and nowhere else', () => {
    const g = laid([node('a'), node('b', ['a'])])
    const b = boxOf(g, 'b')

    expect(g.arrows).toEqual([{ x: b.x - 1, y: b.y + 1 }])
  })

  it('routes a fan-out through one shared trunk', () => {
    // Two edges leaving the same node share a vertical: they genuinely share an
    // endpoint. Two unrelated edges must never share one, or the picture asserts
    // a connection the graph does not contain.
    expect(trunks(laid([node('a'), node('b', ['a']), node('c', ['a'])])).size).toBe(1)
  })

  it('routes a fan-in through one shared trunk', () => {
    expect(trunks(laid([node('a'), node('b'), node('c', ['a', 'b'])])).size).toBe(1)
  })

  it('untangles a crossing by ordering the band instead of routing round it', () => {
    // `a -> q`, `b -> p` cross in submitted order. The barycentre pass swaps the
    // second column, and then both edges run straight across -- no vertical at
    // all. A layout that routed the crossing instead would be drawing work the
    // ordering had already made unnecessary.
    const g = laid([node('a'), node('b'), node('p', ['b']), node('q', ['a'])])

    expect(boxOf(g, 'q').y).toBeLessThan(boxOf(g, 'p').y)
    expect(trunks(g).size).toBe(0)
  })

  it('keeps two overlapping forks on separate trunks', () => {
    // Every node of the first column feeds every node of the second, so no
    // ordering removes the overlap: the two fans must span the same rows. One
    // shared trunk here would merge them into a single wire, asserting nothing
    // and everything at once.
    const g = laid([node('a'), node('b'), node('p', ['a', 'b']), node('q', ['a', 'b'])])

    expect(trunks(g).size).toBe(2)
  })

  it('keeps two trunks apart when their spans merely touch', () => {
    // `a`'s fan reaches down to row 4 and `b -> q` starts there. Sharing a
    // column would put both turns in one cell, and the junction character would
    // then read as `a -> q`, which is not an edge of this graph.
    const g = laid([node('a'), node('b'), node('p', ['a']), node('q', ['b']), node('r', ['a'])])

    expect(trunks(g).size).toBe(2)
  })

  it('spends no trunk on an edge whose ends share a band', () => {
    expect(trunks(laid([node('a'), node('b', ['a'])])).size).toBe(0)
  })

  it('orders a band by the mean band of its dependencies', () => {
    // Without the barycentre pass `y` follows submitted order and the wires
    // cross for no reason.
    const g = laid([node('a'), node('b'), node('x', ['b']), node('y', ['a'])])

    expect(boxOf(g, 'y').y).toBeLessThan(boxOf(g, 'x').y)
  })

  it('keeps the submitted order of the first column', () => {
    // The picture is rebuilt on every progress event; ordering by anything that
    // changes as the run advances would make the boxes jump.
    const g = laid([node('z'), node('m'), node('a')])

    expect(g.boxes.map(box => box.nodeId)).toEqual(['z', 'm', 'a'])
  })

  it('still places every node of a cyclic graph', () => {
    // The backend rejects cycles before running, so only a malformed frame gets
    // here -- but looping forever while drawing is worse than an imperfect draw.
    const g = laid([node('a', ['b']), node('b', ['a'])])

    expect(g.boxes.map(box => box.nodeId).sort()).toEqual(['a', 'b'])
  })

  it('drops an edge that does not point rightwards', () => {
    // Only reachable through the cycle fallback above. Such an edge has no
    // left-to-right route, so it is left to the row text rather than drawn wrong.
    expect(laid([node('a', ['b']), node('b', ['a'])]).drawn.size).toBeLessThanOrEqual(1)
  })

  it('returns null for an empty graph', () => {
    expect(layoutDagGraph([], WIDE)).toBeNull()
  })
})

const bandRows = (geometry: DagGraphGeometry) =>
  Math.max(...geometry.boxes.map(box => box.y)) + BOX_BAND_HEIGHT

describe('layoutDagGraph long edges', () => {
  const SKIPPER = [node('a'), node('b', ['a']), node('c', ['b', 'a'])]

  it('draws an edge that spans more than one column', () => {
    // `a>c` is the long one: `a` is two columns left of `c`.
    expect([...laid(SKIPPER).drawn].sort()).toEqual(['a>b', 'a>c', 'b>c'])
  })

  it('routes it on a row below every box rather than through a box band', () => {
    // A three-row Sugiyama dummy in the intermediate column would cost a whole
    // band; a one-row channel under the picture costs one row and reads as a
    // bypass.
    const g = laid(SKIPPER)

    expect(g.height).toBe(bandRows(g) + 1)
    expect(g.segments.some(s => s.y0 === s.y1 && s.y0 === bandRows(g))).toBe(true)
  })

  it('never runs a flyover through a box', () => {
    const g = laid(SKIPPER)
    const covered = new Set(
      g.boxes.flatMap(box =>
        [box.y, box.y + 1, box.y + 2].flatMap(y =>
          Array.from({ length: box.width }, (_, i) => `${box.x + i},${y}`)
        )
      )
    )

    g.segments
      .filter(s => s.y0 === s.y1 && s.y0 >= bandRows(g))
      .forEach(s => {
        for (let x = Math.min(s.x0, s.x1); x <= Math.max(s.x0, s.x1); x += 1) {
          expect(covered.has(`${x},${s.y0}`)).toBe(false)
        }
      })
  })

  it('packs two long edges whose column spans do not overlap onto one row', () => {
    const g = laid([node('a'), node('b', ['a']), node('c', ['b', 'a']), node('d', ['c']), node('e', ['d', 'c'])])

    expect(g.height).toBe(bandRows(g) + 1)
  })

  it('gives two overlapping long edges a row each', () => {
    const g = laid([node('a'), node('b'), node('c', ['a']), node('d', ['c', 'a']), node('e', ['c', 'b'])])

    expect(g.height).toBe(bandRows(g) + 2)
  })

  it('puts no arrowhead on a flyover, only at the box it finally reaches', () => {
    const g = laid(SKIPPER)

    expect(g.arrows).toEqual([
      { x: boxOf(g, 'b').x - 1, y: boxOf(g, 'b').y + 1 },
      { x: boxOf(g, 'c').x - 1, y: boxOf(g, 'c').y + 1 }
    ])
  })
})

describe('layoutDagGraph width degradation', () => {
  const CHAIN = [node('a'), node('b', ['a']), node('c', ['b']), node('d', ['c'])]

  it('names the agent when there is room', () => {
    const g = laid(CHAIN)

    expect(g.style).toBe('full')
    expect(g.boxes[0]!.label).toContain('code')
  })

  it('drops the agent name rather than the picture when it does not fit', () => {
    const full = laid(CHAIN)
    const tight = laid(CHAIN, full.width - 1)

    expect(tight.style).toBe('compact')
    expect(tight.boxes[0]!.label).not.toContain('code')
    expect(tight.width).toBeLessThanOrEqual(full.width - 1)
  })

  it('keeps the ordinal and the status glyph in the compact label', () => {
    const tight = laid(CHAIN, laid(CHAIN).width - 1)

    expect(tight.boxes[0]!.label.trim()).toBe('1 ✓')
  })

  it('gives up rather than overflow the row', () => {
    expect(layoutDagGraph(CHAIN, { width: 10 })).toBeNull()
  })

  it('never reports a width above the budget', () => {
    // The panel hands down whatever the terminal left it; a picture that ignored
    // the budget would wrap and shear the whole grid.
    for (let width = 8; width <= 120; width += 1) {
      const g = layoutDagGraph(CHAIN, { width })

      if (g) {
        expect(g.width).toBeLessThanOrEqual(width)
      }
    }
  })
})
