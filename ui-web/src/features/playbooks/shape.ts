/* The card's concept diagram, as geometry.
 *
 * A library card is a fixed rectangle, so the drawing area is fixed too and the
 * graph has to fit it however long or wide it is. Three rules, in order:
 *
 * 1. Step boxes are the SAME SIZE on every card. Scaling a ten-step graph down
 *    to fit would draw its boxes as slivers beside a two-step card's fat ones,
 *    and the one thing a wall of cards has to do is let the eye compare shapes.
 * 2. Two metric tiers only. A graph needing more than four columns (or a layer
 *    wider than four) switches to the tight tier, which fits six. Two tiers keep
 *    the wall consistent; a continuous scale would give every card its own.
 * 3. What does not fit is clipped and COUNTED, never squeezed -- the drawing
 *    ends in a "+N" marker and the caption states the real totals. A clipped
 *    diagram that lies about its size is worse than one that admits it.
 *
 * Short graphs are not scaled up: one step draws one box in a mostly empty area,
 * which is the truth about that playbook.
 */

import { depths } from '../dag/graph'

import type { Placed } from '../dag/graph'

export interface Metric {
  W: number
  H: number
  GX: number
  GY: number
}

/* Fits 4 columns / 4 rows in the box below. */
export const ROOMY: Metric = { W: 44, H: 15, GX: 20, GY: 8 }
/* Fits 6 columns / 5 rows. */
export const TIGHT: Metric = { W: 30, H: 11, GX: 13, GY: 6 }
/* The card's drawing area, in the diagram's own coordinates. */
export const CARD_BOX = { W: 268, H: 84 }

export interface Cell {
  id: string
  x: number
  y: number
}

export interface RowOverflow {
  x: number
  y: number
  n: number
}

export interface CardPlan {
  metric: Metric
  cells: Cell[]
  /* Steps not drawn at all because the graph ran off the right edge. */
  hiddenSteps: number
  /* Per-column "+k" markers for layers taller than the box. */
  rowOverflow: RowOverflow[]
  /* Where the trailing "+N" marker goes, when there is one. */
  clipAt: { x: number; y: number } | null
  width: number
  height: number
  steps: number
  columns: number
  parallel: boolean
}

/* One list per layer, in file order within the layer. */
export function columns(nodes: Placed[]): Placed[][] {
  const depth = depths(nodes)
  const cols: Placed[][] = []
  nodes.forEach((n) => {
    const k = depth.get(n.id) || 0
    const col = cols[k]
    if (col) col.push(n)
    else cols[k] = [n]
  })
  /* `depths` can leave a gap only if a node names a dependency that is not in
     the graph, which validation rejects -- but a page must not render `undefined`
     if one ever arrives. */
  return cols.filter(Boolean)
}

export function cardPlan(nodes: Placed[], box = CARD_BOX): CardPlan {
  const cols = columns(nodes)
  const widest = Math.max(...cols.map((c) => c.length), 1)
  const metric = cols.length > 4 || widest > 4 ? TIGHT : ROOMY
  const maxCols = Math.max(1, Math.floor((box.W + metric.GX) / (metric.W + metric.GX)))
  const maxRows = Math.max(1, Math.floor((box.H + metric.GY) / (metric.H + metric.GY)))

  const clipped = cols.length > maxCols
  const shown = clipped ? cols.slice(0, maxCols - 1) : cols
  /* Per column: what is drawn, and how many that column hides. */
  const drawn = shown.map((c) => (c.length > maxRows ? c.slice(0, maxRows - 1) : c))
  const hiddenPerCol = shown.map((c, i) => c.length - (drawn[i] as Placed[]).length)
  const slots = drawn.map((c, i) => c.length + ((hiddenPerCol[i] as number) > 0 ? 1 : 0))
  const rowsUsed = Math.max(...slots, 1)
  const height = rowsUsed * metric.H + (rowsUsed - 1) * metric.GY
  const width =
    shown.length * metric.W + (shown.length - 1) * metric.GX + (clipped ? metric.GX + 24 : 0)

  const colTop = (i: number): number => {
    const span = (slots[i] as number) * metric.H + ((slots[i] as number) - 1) * metric.GY
    return (height - span) / 2
  }
  const cells: Cell[] = []
  drawn.forEach((col, i) => {
    col.forEach((n, j) => {
      cells.push({ id: n.id, x: i * (metric.W + metric.GX), y: colTop(i) + j * (metric.H + metric.GY) })
    })
  })
  const rowOverflow: RowOverflow[] = []
  hiddenPerCol.forEach((k, i) => {
    if (!k) return
    const last = (slots[i] as number) - 1
    rowOverflow.push({ x: i * (metric.W + metric.GX), y: colTop(i) + last * (metric.H + metric.GY), n: k })
  })

  const drawnSteps = cells.length
  return {
    metric,
    cells,
    hiddenSteps: nodes.length - drawnSteps,
    rowOverflow,
    clipAt: clipped
      ? { x: (shown.length - 1) * (metric.W + metric.GX) + metric.W + metric.GX, y: height / 2 }
      : null,
    width,
    height,
    steps: nodes.length,
    columns: cols.length,
    parallel: cols.some((c) => c.length > 1)
  }
}

/* A cubic between two box edges, the same curve the run graph draws. */
export function edge(x1: number, y1: number, x2: number, y2: number): string {
  const m = (x1 + x2) / 2
  return `M${x1} ${y1} C${m} ${y1} ${m} ${y2} ${x2} ${y2}`
}
