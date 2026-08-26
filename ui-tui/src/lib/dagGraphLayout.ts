// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Where a DAG run's boxes and wires go, in character cells.
//
// Columns are dependency depth, so the graph reads left to right; a column's
// nodes stack into bands, and a band is a global row index so boxes line up
// across columns. Everything an edge needs is emitted as orthogonal segments --
// which character draws a corner is the renderer's problem, not this module's.
//
// Two rules keep the picture honest. `keyOf` says two wires may share a
// vertical trunk only when they share an endpoint -- merging any two verticals
// that merely meet would draw an edge the graph does not contain. `findCrossings`
// covers the other way two unrelated edges land on one cell: a horizontal run
// reaching its own trunk may still have to pass another edge's trunk on the way,
// which is a crossing, not a joint, and must neither render as one nor let
// `drawn` credit either edge with a dependency the picture cannot actually show.

import { stringWidth } from '@hermes/ink'

import type { DagRunNode } from '../domain/dagRun.js'

import { layoutDag } from './dagLayout.js'
import { DAG_STATUS_GLYPH } from './dagStatus.js'
import { padToWidth } from './text.js'

/** Rows one band of boxes occupies: top border, label, bottom border. */
export const BOX_BAND_HEIGHT = 3

/** Blank cells between a box and the nearest trunk, on each side of a gutter. */
export const GUTTER_PAD = 2

export interface DagGraphBox {
  nodeId: string
  /** Cell column of the box's left border. */
  x: number
  /** Cell row of the box's top border; the label sits at `y + 1`. */
  y: number
  width: number
  /** Label already laid out to `width - 2` cells: one space in from the left
   * border, then padded out to the right. */
  label: string
}

/** One orthogonal run. Horizontal when `y0 === y1`, vertical when `x0 === x1`. */
export interface DagGraphSegment {
  x0: number
  y0: number
  x1: number
  y1: number
  /** Endpoints of the edge this run belongs to, so a renderer can tell a
   * crossing -- two runs that merely meet -- from a junction, where they join. */
  source: string
  target: string
}

export interface DagGraphGeometry {
  boxes: DagGraphBox[]
  segments: DagGraphSegment[]
  /** Cells taking an arrowhead: one left of every box with an incoming edge. */
  arrows: { x: number; y: number }[]
  /** `${dep}>${node}` for every edge the picture draws. */
  drawn: ReadonlySet<string>
  width: number
  height: number
  style: DagGraphLabelStyle
}

export type DagGraphLabelStyle = 'compact' | 'full'

interface Edge {
  source: string
  target: string
  /** Row a multi-column edge detours through, below every box. */
  fly?: number
}

/** One edge's vertical in one gutter, before a trunk has been picked for it. */
interface Stub {
  gutter: number
  rows: number[]
  /** Endpoint this stub may share a trunk with, when that endpoint fans out. */
  source?: string
  /** Endpoint this stub may share a trunk with, when that endpoint joins. */
  target?: string
  /** Identity when neither endpoint fans, so the stub keeps a trunk to itself. */
  own: string
  /** Cell column of the trunk, once assigned. Absent when no vertical is needed. */
  x?: number
}

const labelOf = (node: DagRunNode, ordinal: number, ordinalWidth: number, style: DagGraphLabelStyle) => {
  const head = `${String(ordinal).padStart(ordinalWidth)} ${DAG_STATUS_GLYPH[node.status].glyph}`

  return style === 'compact' ? head : `${head} ${node.subagent.replace(/^raven-/, '')}`
}

/**
 * A label placed in its box: one space in from the left border, padded out.
 *
 * Left, not centred. Centring gave every box a different offset -- the widest
 * agent name decided the box width and the rest floated inside it -- so the
 * ordinals, the status glyphs and the agent names each stepped in and out down
 * the column, and nothing in the picture could be read as a column at all.
 */
const place = (text: string, room: number) => padToWidth(` ${text}`, room)

/**
 * Order a column's nodes by the mean band of their dependencies.
 *
 * One pass, left to right, which is enough to keep a fan's branches under their
 * parent and to untangle a plain crossing without routing round it. Ties fall
 * back to the submitted index and the sort is stable, so the ordering is a pure
 * function of the graph -- a run's boxes must not move as its statuses change.
 */
const byBarycentre = (ids: string[], deps: (id: string) => string[], band: Map<string, number>) => {
  const keyed = ids.map((id, index) => {
    const placed = deps(id).filter(dep => band.has(dep))
    const mean = placed.reduce((sum, dep) => sum + band.get(dep)!, 0) / (placed.length || 1)

    return { id, index, key: placed.length > 0 ? mean : index }
  })

  keyed.sort((a, b) => a.key - b.key || a.index - b.index)

  return keyed.map(entry => entry.id)
}

/**
 * First track whose claimed spans all clear `[lo, hi]`, else a new one.
 *
 * `touching` says whether a span may start where another ends. Flyover rows may:
 * their spans are column indices, and two flyovers meeting at a column are still
 * a whole gutter apart in cells, so their lines never join. Trunks may not: two
 * trunks sharing a column, one ending on the row the other begins, put a turn
 * and a turn in the same cell, and the junction character then claims a
 * connection the graph does not contain.
 */
const firstFree = (tracks: [number, number][][], lo: number, hi: number, touching: boolean) => {
  const clear = ([a, b]: [number, number]) => (touching ? hi <= a || lo >= b : hi < a || lo > b)
  const free = tracks.findIndex(taken => taken.every(clear))
  const track = free >= 0 ? free : tracks.length

  ;(tracks[track] ??= []).push([lo, hi])

  return track
}

/**
 * Cells where a horizontal run crosses a vertical trunk it has no stake in.
 *
 * Two edges that share no endpoint can still land on the same cell: a
 * horizontal reaching its own trunk may have to pass someone else's on the
 * way, which is unavoidable once trunks are assigned. A renderer OR-ing
 * direction bits per cell cannot tell that crossing from a genuine junction,
 * so this walks every horizontal against every vertical up front and flags
 * the ones whose edges do not share an endpoint -- the only case a shared
 * cell does not already mean a legitimate fork or join.
 */
export const findCrossings = (segments: readonly DagGraphSegment[]) => {
  const sharesEndpoint = (a: DagGraphSegment, b: DagGraphSegment) =>
    a.source === b.source || a.source === b.target || a.target === b.source || a.target === b.target

  const horizontals = segments.filter(segment => segment.y0 === segment.y1)
  const verticals = segments.filter(segment => segment.x0 === segment.x1)
  const cells = new Map<string, { x: number; y: number }>()
  const edges = new Set<string>()

  horizontals.forEach(h => {
    const [hLo, hHi] = h.x0 <= h.x1 ? [h.x0, h.x1] : [h.x1, h.x0]

    verticals.forEach(v => {
      if (sharesEndpoint(h, v)) {
        return
      }

      const [vLo, vHi] = v.y0 <= v.y1 ? [v.y0, v.y1] : [v.y1, v.y0]

      if (v.x0 >= hLo && v.x0 <= hHi && h.y0 >= vLo && h.y0 <= vHi) {
        cells.set(`${v.x0},${h.y0}`, { x: v.x0, y: h.y0 })
        edges.add(`${h.source}>${h.target}`)
        edges.add(`${v.source}>${v.target}`)
      }
    })
  })

  return { cells: [...cells.values()], edges }
}

const build = (nodes: readonly DagRunNode[], style: DagGraphLabelStyle): DagGraphGeometry => {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const levels = layoutDag([...nodes])
  const column = new Map<string, number>()

  levels.forEach((level, index) => level.forEach(node => column.set(node.id, index)))

  const band = new Map<string, number>()

  levels
    .map(level => level.map(node => node.id))
    .forEach((ids, index) => {
      const ordered = index === 0 ? ids : byBarycentre(ids, id => byId.get(id)!.dependsOn, band)

      ordered.forEach((id, row) => band.set(id, row))
    })

  const bands = Math.max(...levels.map(level => level.length))
  const rowOf = (id: string) => band.get(id)! * BOX_BAND_HEIGHT + 1

  // An edge is drawn only when it points rightwards. Anything else -- a
  // dependency on a node from an earlier run, or the backwards edge a malformed
  // cyclic frame produces -- has no left-to-right route, and is left for the row
  // text to name rather than drawn wrong.
  const edges: Edge[] = nodes.flatMap(node =>
    node.dependsOn
      .filter(dep => byId.has(dep) && column.get(node.id)! - column.get(dep)! > 0)
      .map(dep => ({ source: dep, target: node.id }))
  )

  const spans = (edge: Edge) => column.get(edge.target)! - column.get(edge.source)!

  // ── Flyover rows ─────────────────────────────────────────────────
  // An edge crossing an intermediate column cannot pass through its boxes. A
  // Sugiyama dummy node there would cost a whole three-row band; a one-row
  // channel below every box costs one row and reads as the bypass it is. Two
  // flyovers share a row when their column spans do not overlap.
  const flyRows: [number, number][][] = []

  edges
    .filter(edge => spans(edge) > 1)
    .forEach(edge => {
      edge.fly =
        bands * BOX_BAND_HEIGHT + firstFree(flyRows, column.get(edge.source)!, column.get(edge.target)!, true)
    })

  // ── Gutter trunks ────────────────────────────────────────────────
  const stubsOf = new Map<Edge, Stub[]>()
  const all: Stub[] = []
  const add = (edge: Edge, stub: Stub) => {
    stubsOf.set(edge, [...(stubsOf.get(edge) ?? []), stub])
    all.push(stub)
  }

  edges.forEach(edge => {
    const key = `${edge.source}>${edge.target}`

    if (edge.fly === undefined) {
      add(edge, {
        gutter: column.get(edge.source)!,
        own: `x:${key}`,
        rows: [rowOf(edge.source), rowOf(edge.target)],
        source: edge.source,
        target: edge.target
      })

      return
    }

    // Two verticals, not one: down to the flyover on the source's side, up off
    // it on the target's. Each keeps the endpoint it shares, so a long edge
    // leaving a forking node rides that fork's trunk.
    add(edge, {
      gutter: column.get(edge.source)!,
      own: `d:${key}`,
      rows: [rowOf(edge.source), edge.fly],
      source: edge.source
    })
    add(edge, {
      gutter: column.get(edge.target)! - 1,
      own: `u:${key}`,
      rows: [edge.fly, rowOf(edge.target)],
      target: edge.target
    })
  })

  const gutters = Math.max(0, levels.length - 1)
  const laneCount: number[] = []

  for (let gutter = 0; gutter < gutters; gutter += 1) {
    const here = all.filter(stub => stub.gutter === gutter)
    const forks = new Map<string, number>()
    const joins = new Map<string, number>()

    here.forEach(stub => {
      if (stub.source) {
        forks.set(stub.source, (forks.get(stub.source) ?? 0) + 1)
      }

      if (stub.target) {
        joins.set(stub.target, (joins.get(stub.target) ?? 0) + 1)
      }
    })

    const keyOf = (stub: Stub) =>
      stub.source && (forks.get(stub.source) ?? 0) > 1
        ? `s:${stub.source}`
        : stub.target && (joins.get(stub.target) ?? 0) > 1
          ? `t:${stub.target}`
          : stub.own

    const grouped = new Map<string, Stub[]>()

    here.forEach(stub => grouped.set(keyOf(stub), [...(grouped.get(keyOf(stub)) ?? []), stub]))

    const packed: [number, number][][] = []

    grouped.forEach(group => {
      const rows = group.flatMap(stub => stub.rows)
      const lo = Math.min(...rows)
      const hi = Math.max(...rows)

      // Every stop on one row means no vertical, so no trunk to reserve.
      if (lo === hi) {
        return
      }

      const lane = firstFree(packed, lo, hi, false)

      group.forEach(stub => {
        stub.x = GUTTER_PAD + lane
      })
    })

    laneCount.push(packed.length)
  }

  const gutterWidth = laneCount.map(count => GUTTER_PAD * 2 + Math.max(1, count) + 1)
  // Padded to the run's widest ordinal, so a two-digit run does not step its
  // glyph column sideways at the tenth box.
  const ordinalWidth = String(nodes.length).length
  const boxWidth =
    Math.max(...nodes.map((node, index) => stringWidth(labelOf(node, index + 1, ordinalWidth, style)))) + 4
  const xOf: number[] = []
  let cursor = 0

  for (let index = 0; index < levels.length; index += 1) {
    xOf.push(cursor)
    cursor += boxWidth + (index < gutters ? gutterWidth[index]! : 0)
  }

  // `stub.x` was stored relative to its gutter, which starts where the box to
  // its left ends -- the absolute column is only knowable once box width is.
  const trunkOf = (stub: Stub) => xOf[stub.gutter]! + boxWidth + stub.x!

  // ── Boxes, in submitted order so an ordinal and a box agree ──────
  const boxes: DagGraphBox[] = nodes.map((node, index) => ({
    label: place(labelOf(node, index + 1, ordinalWidth, style), boxWidth - 2),
    nodeId: node.id,
    width: boxWidth,
    x: xOf[column.get(node.id)!]!,
    y: band.get(node.id)! * BOX_BAND_HEIGHT
  }))

  // ── Wires ────────────────────────────────────────────────────────
  const segments: DagGraphSegment[] = []
  const push = (edge: Edge, x0: number, y0: number, x1: number, y1: number) => {
    if (x0 !== x1 || y0 !== y1) {
      segments.push({ source: edge.source, target: edge.target, x0, x1, y0, y1 })
    }
  }

  const leaves = (col: number) => xOf[col]! + boxWidth
  const enters = (col: number) => xOf[col]! - 2

  edges.forEach(edge => {
    const [first, second] = stubsOf.get(edge)!
    const sourceRow = rowOf(edge.source)
    const targetRow = rowOf(edge.target)

    if (edge.fly === undefined) {
      const trunk = first!.x === undefined ? null : trunkOf(first!)

      if (trunk === null) {
        push(edge, leaves(column.get(edge.source)!), sourceRow, enters(column.get(edge.target)!), sourceRow)

        return
      }

      push(edge, leaves(column.get(edge.source)!), sourceRow, trunk, sourceRow)
      push(edge, trunk, sourceRow, trunk, targetRow)
      push(edge, trunk, targetRow, enters(column.get(edge.target)!), targetRow)

      return
    }

    const down = trunkOf(first!)
    const up = trunkOf(second!)

    push(edge, leaves(column.get(edge.source)!), sourceRow, down, sourceRow)
    push(edge, down, sourceRow, down, edge.fly)
    push(edge, down, edge.fly, up, edge.fly)
    push(edge, up, edge.fly, up, targetRow)
    push(edge, up, targetRow, enters(column.get(edge.target)!), targetRow)
  })

  const ambiguous = findCrossings(segments).edges
  const drawn = new Set(edges.map(edge => `${edge.source}>${edge.target}`).filter(key => !ambiguous.has(key)))
  const incoming = new Set(edges.map(edge => edge.target))

  return {
    arrows: nodes
      .filter(node => incoming.has(node.id))
      .map(node => ({ x: xOf[column.get(node.id)!]! - 1, y: rowOf(node.id) })),
    boxes,
    drawn,
    height: bands * BOX_BAND_HEIGHT + flyRows.length,
    segments,
    style,
    width: xOf[levels.length - 1]! + boxWidth
  }
}

/**
 * Lay a run's graph out in character cells, or return `null` if it cannot fit.
 *
 * `opts.width` is the budget the panel has. A picture too wide for it first
 * drops the agent name from every box; if that is still too wide, no picture is
 * drawn at all and the caller falls back to naming dependencies in the rows.
 */
export const layoutDagGraph = (
  nodes: readonly DagRunNode[],
  opts: { width: number }
): DagGraphGeometry | null => {
  if (nodes.length === 0) {
    return null
  }

  const full = build(nodes, 'full')

  if (full.width <= opts.width) {
    return full
  }

  const compact = build(nodes, 'compact')

  return compact.width <= opts.width ? compact : null
}
