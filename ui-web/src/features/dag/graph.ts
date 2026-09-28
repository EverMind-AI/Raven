/* The graph, as geometry and as sentences -- what a caller decides before it
 * draws anything: the layout the desk's task board places its nodes with, and
 * the one-line sentences about a run, of which the transcript's dag card
 * prints the graph's shape.
 *
 * Pure on purpose. Depth-by-longest-path, the edge routing and the
 * sentences carry every edge case in this domain (a cycle the server should
 * never send, an edge that skips a layer, a run whose summary
 * counts more nodes than the graph holds), and none of it was reachable from a
 * test while it lived inside the renderer in the live layer.
 */

import dagre from '@dagrejs/dagre'

import { t } from '../../i18n/t'

import type { DagNode, DagRun, DagLayout, EdgeRoute, NodeAt } from './types'

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

/* Room between two edges that pass beside the same layer, and between such an
   edge and a box. Wider than the gap between two boxes: a line drawn at the
   boxes' own spacing reads as a border of the box it runs along. */
const EDGE_SEP = 24
/* How far apart two edges meet one face of a box. Every edge into a fan-in
   used to land on the face's midpoint, so an edge that came straight down
   behind another -- one that skips a layer, beside one that does not -- was
   drawn exactly under it and could not be seen at all. */
const PORT_GAP = 14

interface Pt {
  u: number
  v: number
}

/* Spread `k` ports across a face `box` long, centred, closer together than the
   face is wide so a fan still reads as converging on its box. */
const portOffset = (i: number, k: number, box: number): number => {
  if (k < 2) return 0
  const span = Math.min(box * 0.6, (k - 1) * PORT_GAP)
  return -span / 2 + (i * span) / (k - 1)
}

/* Layered layout, by dagre: depth-by-longest-path decides the layers (forced
   through each edge's `minlen`, so a root always opens the graph, where a run
   starts it, rather than drifting down beside the one node it feeds), and
   dagre decides the order inside each layer and where an edge that skips a
   layer passes it. That second half is what the hand-rolled layout lacked: it
   centred every layer on the midline, so an edge skipping a layer ran straight
   through the box centred in it.

   Worked in flow space -- `v` along the flow, `u` across it -- and turned to
   x/y once at the end, so both flows share every line of it. */
export function layout(nodes: Placed[], dims: Dims, flow: Flow = 'across'): DagLayout {
  const { W, H, GAP_X, GAP_Y, PAD } = dims
  const down = flow === 'down'
  const bu = down ? W : H
  const bv = down ? H : W
  const step = down ? GAP_Y : GAP_X
  const spread = down ? GAP_X : GAP_Y
  const toXY = (p: Pt): NodeAt => (down ? { x: p.u, y: p.v } : { x: p.v, y: p.u })
  if (!nodes.length) return { at: new Map(), edges: [], width: PAD * 2, height: PAD * 2 }

  const depth = depths(nodes)
  const ids = new Set(nodes.map((n) => n.id))
  const links: Array<[string, string]> = []
  nodes.forEach((n) => {
    new Set(n.depends_on).forEach((p) => {
      if (p !== n.id && ids.has(p)) links.push([p, n.id])
    })
  })

  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: spread - bu, ranksep: step - bv, edgesep: EDGE_SEP, marginx: PAD, marginy: PAD })
  g.setDefaultEdgeLabel(() => ({}))
  nodes.forEach((n) => g.setNode(n.id, { width: bu, height: bv }))
  links.forEach(([p, c]) => g.setEdge(p, c, { minlen: Math.max(1, (depth.get(c) || 0) - (depth.get(p) || 0)) }))
  dagre.layout(g)

  const lv = (id: string): number => PAD + (depth.get(id) || 0) * step
  const pos = new Map<string, Pt>(nodes.map((n) => [n.id, { u: g.node(n.id).x - bu / 2, v: lv(n.id) }]))
  /* Where an edge passes each layer it skips: dagre's bend nearest that
     layer's middle, held for the layer's full depth. */
  const bends = links.map(([p, c]) => {
    const raw: Array<{ x: number; y: number }> = g.edge(p, c)?.points || []
    const out: Pt[] = []
    for (let d = (depth.get(p) || 0) + 1; d < (depth.get(c) || 0); d++) {
      const mid = PAD + d * step + bv / 2
      const near = raw.reduce<{ x: number; y: number } | null>(
        (best, q) => (!best || Math.abs(q.y - mid) < Math.abs(best.y - mid) ? q : best), null)
      const u = near ? near.x : (pos.get(p)!.u + pos.get(c)!.u) / 2 + bu / 2
      out.push({ u, v: PAD + d * step }, { u, v: PAD + d * step + bv })
    }
    return out
  })
  const width = g.graph().width || PAD * 2 + bu
  const extent = PAD * 2 + (Math.max(0, ...depth.values()) * step) + bv

  /* dagre's order is its own, and a fan-out often comes back mirrored: the
     server's first node on the far side. Mirroring changes no crossing, so the
     picture is flipped whenever that reads closer to the order the graph was
     written in. */
  const index = new Map(nodes.map((n, i) => [n.id, i]))
  let agree = 0
  nodes.forEach((a) => nodes.forEach((b) => {
    if (depth.get(a.id) !== depth.get(b.id) || index.get(a.id)! >= index.get(b.id)!) return
    agree += Math.sign(pos.get(b.id)!.u - pos.get(a.id)!.u)
  }))
  if (agree < 0) {
    pos.forEach((p) => { p.u = width - p.u - bu })
    bends.forEach((b) => b.forEach((p) => { p.u = width - p.u }))
  }

  /* Ports: the edges leaving one face, or entering it, spread across it in
     the order they head off in, so two of them never cross at the box. */
  const centre = (id: string): number => pos.get(id)!.u + bu / 2
  const outPort = new Map<number, number>()
  const inPort = new Map<number, number>()
  const assign = (side: Map<number, number>, key: (i: number) => string, heading: (i: number) => number): void => {
    const by = new Map<string, number[]>()
    links.forEach((_, i) => {
      const list = by.get(key(i))
      if (list) list.push(i)
      else by.set(key(i), [i])
    })
    by.forEach((list, id) => {
      list.sort((a, b) => heading(a) - heading(b))
      list.forEach((e, i) => side.set(e, centre(id) + portOffset(i, list.length, bu)))
    })
  }
  assign(outPort, (i) => links[i]![0], (i) => bends[i]![0]?.u ?? centre(links[i]![1]))
  assign(inPort, (i) => links[i]![1], (i) => bends[i]!.at(-1)?.u ?? centre(links[i]![0]))

  const edges: EdgeRoute[] = links.map(([p, c], i) => ({
    from: p,
    to: c,
    points: [
      { u: outPort.get(i)!, v: pos.get(p)!.v + bv },
      ...bends[i]!,
      { u: inPort.get(i)!, v: pos.get(c)!.v },
    ].map(toXY),
  }))
  const at = new Map<string, NodeAt>([...pos].map(([id, p]) => [id, toXY(p)]))
  return { at, edges, width: down ? width : extent, height: down ? extent : width }
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
