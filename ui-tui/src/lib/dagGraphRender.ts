// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Turn a DAG picture's geometry into rows of styled spans.
//
// Wires are rasterised by OR-ing a 4-bit direction mask into every cell each
// segment crosses, then translating the accumulated mask to a character. That is
// what lets forks and joins fall out of one table with no special case: a cell
// reached from above and from the left is a corner, the same cell reached from
// three or four sides is a fork or a join.
//
// A cell two edges reach without sharing an endpoint is different: OR-ing their
// bits would draw a junction for an edge neither of them is. `findCrossings`
// flags those cells so the mask there keeps only its horizontal bits, leaving
// the horizontal run unbroken and the vertical trunk reading as passing behind
// it -- the convention `git log --graph` uses for branch lines that merely cross.
//
// Boxes are drawn after the wires, so a box always wins its own cells.

import type { DagGraphGeometry } from './dagGraphLayout.js'

import { findCrossings } from './dagGraphLayout.js'

const UP = 1
const DOWN = 2
const LEFT = 4
const RIGHT = 8

// Rounded corners so a wire's turn matches the box borders it runs between.
const MASK_CHAR: Record<number, string> = {
  [UP]: '│',
  [DOWN]: '│',
  [LEFT]: '─',
  [RIGHT]: '─',
  [UP | DOWN]: '│',
  [LEFT | RIGHT]: '─',
  [DOWN | RIGHT]: '╭',
  [DOWN | LEFT]: '╮',
  [UP | RIGHT]: '╰',
  [UP | LEFT]: '╯',
  [UP | DOWN | RIGHT]: '├',
  [UP | DOWN | LEFT]: '┤',
  [DOWN | LEFT | RIGHT]: '┬',
  [UP | LEFT | RIGHT]: '┴',
  [UP | DOWN | LEFT | RIGHT]: '┼'
}

const ARROW = '▸'

export type DagPictureSpanKind = 'border' | 'glyph' | 'label' | 'wire'

export interface DagPictureSpan {
  text: string
  kind: DagPictureSpanKind
  /** The node a box span belongs to. Absent on wires. */
  nodeId?: string
}

interface Cell {
  char: string
  kind: DagPictureSpanKind
  nodeId?: string
}

export const renderDagGraph = (geometry: DagGraphGeometry): DagPictureSpan[][] => {
  const { arrows, boxes, height, segments, width } = geometry
  const grid: Cell[][] = Array.from({ length: height }, () =>
    Array.from({ length: width }, () => ({ char: ' ', kind: 'wire' as DagPictureSpanKind }))
  )
  const mask: number[][] = Array.from({ length: height }, () => Array.from({ length: width }, () => 0))

  segments.forEach(({ x0, x1, y0, y1 }) => {
    const [lowX, highX] = x0 <= x1 ? [x0, x1] : [x1, x0]
    const [lowY, highY] = y0 <= y1 ? [y0, y1] : [y1, y0]

    if (y0 === y1) {
      for (let x = lowX; x <= highX; x += 1) {
        mask[y0]![x] |= (x > lowX ? LEFT : 0) | (x < highX ? RIGHT : 0)
      }

      return
    }

    for (let y = lowY; y <= highY; y += 1) {
      mask[y]![x0] |= (y > lowY ? UP : 0) | (y < highY ? DOWN : 0)
    }
  })

  findCrossings(segments).cells.forEach(({ x, y }) => {
    const bits = mask[y]![x]!
    const horizontal = bits & (LEFT | RIGHT)

    mask[y]![x] = horizontal !== 0 ? horizontal : bits & (UP | DOWN)
  })

  boxes.forEach(box => {
    const put = (y: number, x: number, char: string, kind: DagPictureSpanKind) => {
      grid[y]![x] = { char, kind, nodeId: box.nodeId }
    }
    const last = box.width - 1

    for (let offset = 0; offset <= last; offset += 1) {
      put(box.y, box.x + offset, '─', 'border')
      put(box.y + 2, box.x + offset, '─', 'border')
    }

    put(box.y, box.x, '╭', 'border')
    put(box.y, box.x + last, '╮', 'border')
    put(box.y + 2, box.x, '╰', 'border')
    put(box.y + 2, box.x + last, '╯', 'border')
    put(box.y + 1, box.x, '│', 'border')
    put(box.y + 1, box.x + last, '│', 'border')

    // The glyph is the label's second space-separated field. Located by index
    // rather than by searching for the character, which would also match a
    // border or a wire drawn from the same set.
    const glyphAt = box.label.indexOf(' ', box.label.search(/\S/) + 1) + 1

    ;[...box.label].forEach((char, offset) =>
      put(box.y + 1, box.x + 1 + offset, char, offset === glyphAt ? 'glyph' : 'label')
    )
  })

  arrows.forEach(({ x, y }) => {
    grid[y]![x] = { char: ARROW, kind: 'wire' }
  })

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const bits = mask[y]![x]!

      if (bits && grid[y]![x]!.char === ' ') {
        grid[y]![x] = { char: MASK_CHAR[bits]!, kind: 'wire' }
      }
    }
  }

  return grid.map(row =>
    row.reduce<DagPictureSpan[]>((spans, cell) => {
      const last = spans[spans.length - 1]

      if (last && last.kind === cell.kind && last.nodeId === cell.nodeId) {
        last.text += cell.char

        return spans
      }

      spans.push({ kind: cell.kind, text: cell.char, ...(cell.nodeId ? { nodeId: cell.nodeId } : {}) })

      return spans
    }, [])
  )
}
