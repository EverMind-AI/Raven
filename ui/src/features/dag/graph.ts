/* The graph, as geometry and as sentences -- everything the dag panel decides
 * before it draws anything.
 *
 * Pure on purpose. Depth-by-longest-path, the column centring and the two
 * one-line summaries carry every edge case in this domain (a cycle the server
 * should never send, a fan-out that has to read as a diamond, a run whose
 * summary counts more nodes than the graph holds), and none of it was reachable
 * from a test while it lived inside the renderer in the live layer.
 */

import { t } from '../../shell/bridge'
import { formatDuration } from '../../shell/duration'
import { settled } from './nodes'

import type { DagNode, DagRun, DagLayout } from './types'

/* Wider and taller than the first pass: the box carries a status mark as well
   as the node's name, the agent under it and a clock, and 108x38 had them
   touching each other. GAP_X leaves 46px of edge between columns, which is
   enough for a curve to read as a curve rather than as a kink.

   Widened again when the name became the node's summary rather than its id. A
   summary is a sentence -- "research the current hot topics and pick two" --
   and the 65px the label had left over from a 136px box cut every one of them
   to four characters, which says less than the id it replaced. */
export const GAP_X = 282
export const GAP_Y = 60
export const W = 236
export const H = 44
export const PAD = 10

export interface Dims {
  W: number
  H: number
  GAP_X: number
  GAP_Y: number
  PAD: number
}

/* The sheet above the composer has the width of the chat column to spend. */
export const SHEET: Dims = { W, H, GAP_X, GAP_Y, PAD }

/* The card inside the transcript does not, so the same graph is drawn smaller.
   A second set of constants rather than a scale factor: the box holds text at a
   fixed size, so what has to change is how much room the text gets, not how big
   everything is. */
export const CARD: Dims = { W: 212, H: 38, GAP_X: 246, GAP_Y: 50, PAD: 10 }

/* Depth by longest path, which is what puts a node in the column after the last
   thing it waits for. Memoised, and guarded against a cycle it should never
   see: the server rejects a cyclic graph before running it, but a panel that
   hangs is a worse way to find that out than a panel that draws something odd. */
export function depths(nodes: DagNode[]): Map<string, number> {
  const by = new Map(nodes.map((n) => [n.id, n]))
  const depth = new Map<string, number>()
  const walking = new Set<string>()
  const of = (id: string): number => {
    const seen = depth.get(id)
    if (seen !== undefined) return seen
    const n = by.get(id)
    const deps = n?.depends_on || []
    if (!n || !deps.length || walking.has(id)) {
      depth.set(id, 0)
      return 0
    }
    walking.add(id)
    let d = 0
    deps.forEach((p) => {
      if (by.has(p)) d = Math.max(d, of(p) + 1)
    })
    walking.delete(id)
    depth.set(id, d)
    return d
  }
  nodes.forEach((n) => of(n.id))
  return depth
}

/* Column x, row y, and the sizes that follow from them. Each column is centred
   on the graph's own midline rather than stacked from the top: a fan-out into
   three and a fan-in back to one then reads as the diamond it is, instead of a
   staircase whose single nodes sit against the ceiling with their edges cutting
   diagonally down. */
export function layout(nodes: DagNode[], dims: Dims = SHEET): DagLayout {
  const { W, H, GAP_X, GAP_Y, PAD } = dims
  const depth = depths(nodes)
  const cols = new Map<number, DagNode[]>()
  nodes.forEach((n) => {
    const c = depth.get(n.id) || 0
    const col = cols.get(c)
    if (col) col.push(n)
    else cols.set(c, [n])
  })
  const tallest = Math.max(...[...cols.values()].map((c) => c.length), 1)
  const height = PAD * 2 + tallest * H + (tallest - 1) * (GAP_Y - H)
  const width = PAD * 2 + (cols.size - 1) * GAP_X + W
  const at = new Map<string, { x: number; y: number }>()
  cols.forEach((column, c) => {
    const span = column.length * H + (column.length - 1) * (GAP_Y - H)
    const top = (height - span) / 2
    column.forEach((n, i) => {
      at.set(n.id, { x: PAD + c * GAP_X, y: top + i * GAP_Y })
    })
  })
  return { at, width, height }
}

/* The one mark a node wears, as geometry rather than as a drawn element: the
   sheet builds SVG imperatively and the card builds it through React, and the
   two used to keep their own copies of these paths. `running` is deliberately
   absent -- it is the same three-bar glyph the turn's own row wears, which each
   surface already has, and duplicating it here would be a second answer to what
   "work in progress" looks like.

   Centre-relative so a caller places it without knowing the box. */
export const MARKS: Record<string, { d: string; cls: string }> = {
  completed: { d: 'M-5 0l3.6 3.8 6.4 -7.6', cls: 'ok' },
  failed: { d: 'M-4 -4l8 8M4 -4l-8 8', cls: 'bad' },
  skipped: { d: 'M-4.5 0h9', cls: 'skip' },
  interrupted: { d: 'M-4.5 0h9', cls: 'skip' },
}

/* One row per layer, deepest last: what the card's own sentence counts and what
   a caller needs to know a graph is a chain rather than a fan-out. */
export function layers(nodes: DagNode[]): number[] {
  const depth = depths(nodes)
  const per = new Map<number, number>()
  nodes.forEach((n) => {
    const k = depth.get(n.id) || 0
    per.set(k, (per.get(k) || 0) + 1)
  })
  return [...per.keys()].sort((a, b) => a - b).map((k) => per.get(k) as number)
}

/* The nodes of a run in the server's order, skipping ids the map does not hold
   -- `order` and `nodes` are written together, but a reader of a run restored
   from disk should not crash on a mismatch. */
export const ordered = (d: DagRun): DagNode[] => d.order.map((id) => d.nodes.get(id)).filter(Boolean) as DagNode[]

/* How much work, how deep, and whether anything actually runs side by side --
   the three facts that tell a chain from a fan-out. The transcript's card says
   this in its `scale` field, where a reader who wants it can look; the sheet
   above the composer draws the graph itself and said it in words as well, on
   every run, which is the one thing the picture says better. */
export function shape(nodes: DagNode[]): string {
  const per = layers(nodes)
  const widest = Math.max(...per, 1)
  const bits = [t('gui.dag.count', { n: nodes.length, d: per.length })]
  bits.push(widest > 1 ? t('gui.dag.parallel', { n: widest }) : t('gui.dag.serial'))
  return bits.join(' · ')
}

/* What the run reported when it finished. `total` is the server's count, which
   is why it wins over the graph's own length: an interrupted run says how many
   nodes it meant to run, and the graph only holds the ones it heard about. */
export function summary(d: DagRun): string {
  const s = d.summary || {}
  const bits = [t('gui.dag.done', { n: s.completed || 0, t: s.total || d.order.length })]
  if (s.failed) bits.push(t('gui.dag.failed', { n: s.failed }))
  if (s.skipped) bits.push(t('gui.dag.skipped', { n: s.skipped }))
  return bits.join(' · ')
}

/* How long a node has been at it. A node that has not started shows nothing;
   one still running is measured against now, which is what the panel's clock
   re-reads every second. Floored at a second so a node that starts and ends
   inside one tick does not report 0.0s.

   A node that has stopped without saying when shows nothing either, rather than
   being measured against now. Two endings arrive that way -- a cancel carries
   neither stamp, and a run's completion reports each node's final status without
   repeating its clock -- and measuring those against now is a number that grows
   for as long as the page stays open. It is the same rule the trail's card
   already applies to the graph's own span: blank beats a made-up number, and one
   reload reads the manifest, which carries the real stamps. */
export function took(n: DagNode, now: number): string {
  if (!n.started_at) return ''
  if (!n.ended_at && settled(String(n.status))) return ''
  const end = n.ended_at || now
  return formatDuration(Math.max(end - n.started_at, 1000))
}
