/* The graph, as geometry and as sentences -- what a caller decides before it
 * draws anything: the layout the desk's task board places its nodes with, and
 * the one-line sentences about a run, of which the transcript's dag card
 * prints the graph's shape.
 *
 * Pure on purpose. Depth-by-longest-path, the column centring and the
 * sentences carry every edge case in this domain (a cycle the server should
 * never send, a fan-out that has to read as a diamond, a run whose summary
 * counts more nodes than the graph holds), and none of it was reachable from a
 * test while it lived inside the renderer in the live layer.
 */

import { t } from '../../i18n/t'

import type { DagNode, DagRun, DagLayout } from './types'

/* Everything the layout reads of a node: which one it is, and what it waits
   for. Declared structurally rather than as `DagNode`, so the layout can only
   ever depend on these two fields. */
export interface Placed {
  id: string
  depends_on: string[]
}

export interface Dims {
  W: number
  H: number
  GAP_X: number
  GAP_Y: number
  PAD: number
}

/* Depth by longest path, which is what puts a node in the column after the last
   thing it waits for. Memoised, and guarded against a cycle it should never
   see: the server rejects a cyclic graph before running it, but a panel that
   hangs is a worse way to find that out than a panel that draws something odd. */
export function depths(nodes: Placed[]): Map<string, number> {
  const by = new Map<string, Placed>(nodes.map((n) => [n.id, n]))
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

/* Which way the run reads. The task board runs top to bottom: it is read in a
   docked pane whose height is the dimension it has to spare, and a fan-out laid
   sideways there is a graph zoomed to a third of life size before the reader
   has done anything. Across is the layout's other axis, kept as the default.
   One layout either way. The two differ only in which axis the depth counts
   along, and a second implementation of depth-by-longest-path is how a second
   caller would start disagreeing with this one about what a diamond looks
   like. */
export type Flow = 'across' | 'down'

/* Layer along the flow, spread across it, and the sizes that follow. Each layer
   is centred on the graph's own midline rather than stacked from the edge: a
   fan-out into three and a fan-in back to one then reads as the diamond it is,
   instead of a staircase whose single nodes sit against the wall with their
   edges cutting diagonally across. */
export function layout(nodes: Placed[], dims: Dims, flow: Flow = 'across'): DagLayout {
  const { W, H, GAP_X, GAP_Y, PAD } = dims
  const down = flow === 'down'
  const depth = depths(nodes)
  const layers = new Map<number, Placed[]>()
  nodes.forEach((n) => {
    const d = depth.get(n.id) || 0
    const layer = layers.get(d)
    if (layer) layer.push(n)
    else layers.set(d, [n])
  })
  /* `step` separates one layer from the next, `spread` one member of a layer
     from its sibling, and `box` is the node's size on the axis it spreads on. */
  const step = down ? GAP_Y : GAP_X
  const spread = down ? GAP_X : GAP_Y
  const box = down ? W : H
  const widest = Math.max(...[...layers.values()].map((l) => l.length), 1)
  const across = PAD * 2 + widest * box + (widest - 1) * (spread - box)
  const along = PAD * 2 + (layers.size - 1) * step + (down ? H : W)
  const at = new Map<string, { x: number; y: number }>()
  layers.forEach((layer, d) => {
    const span = layer.length * box + (layer.length - 1) * (spread - box)
    const head = (across - span) / 2
    layer.forEach((n, i) => {
      const onFlow = PAD + d * step
      const onLayer = head + i * spread
      at.set(n.id, down ? { x: onLayer, y: onFlow } : { x: onFlow, y: onLayer })
    })
  })
  return { at, width: down ? across : along, height: down ? along : across }
}

/* One row per layer, deepest last: what the card's own sentence counts and what
   a caller needs to know a graph is a chain rather than a fan-out. */
export function layers(nodes: Placed[]): number[] {
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
