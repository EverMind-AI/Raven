/* The board's renderer: layout, edges, selection, and click and keyboard on
 * each node. The caller draws every node's own box, handed to it through
 * `renderNode` and laid out inside a `foreignObject` sized to `dims`.
 */

import { layout } from './graph'

import type { Dims, Flow } from './graph'
import type { DagNode, EdgeRoute, NodeAt } from './types'
import type { JSX } from 'react'

/* A caller's own box for a node. Given the node and the clock a running
   node's own duration is measured against, and laid out inside a
   `foreignObject` sized to the node's box -- edges, layout, selection and
   click/keyboard handling all stay DagGraph's; only what a node looks like
   changes. */
export type CardRenderer = (node: DagNode, now: number) => JSX.Element

interface DagGraphProps {
  dims: Dims
  nodes: DagNode[]
  now: number
  onPick: (node: DagNode) => void
  selectedId?: string | null
  /* Left to right unless the flow says otherwise. The task board reads top
     to bottom; see `Flow` in ./graph. */
  flow?: Flow
  renderNode: CardRenderer
}

/* One edge, drawn through its route along whichever way the graph runs. Each
   hop is a curve whose control points sit on the flow axis, so the line leaves
   and enters every face square to it and a fan-out leaves as a fan rather than
   as a sheaf of diagonals; a hop that keeps its place across the flow is the
   straight run beside a skipped layer. The last point is pulled back off the
   face to leave room for the arrowhead. */
function edgePath(points: NodeAt[], down: boolean): { d: string; tip: string } {
  const pts = points.map((p) => ({ ...p }))
  const end = pts[pts.length - 1]!
  if (down) end.y -= 5
  else end.x -= 5
  let d = `M${pts[0]!.x} ${pts[0]!.y}`
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1]!
    const b = pts[i]!
    if (down ? a.x === b.x : a.y === b.y) {
      d += `L${b.x} ${b.y}`
    } else if (down) {
      const mid = (a.y + b.y) / 2
      d += `C${a.x} ${mid} ${b.x} ${mid} ${b.x} ${b.y}`
    } else {
      const mid = (a.x + b.x) / 2
      d += `C${mid} ${a.y} ${mid} ${b.y} ${b.x} ${b.y}`
    }
  }
  const tip = down
    ? `M${end.x - 3} ${end.y - 3.5}L${end.x} ${end.y + 1}l3 -4.5`
    : `M${end.x - 3.5} ${end.y - 3}L${end.x + 1} ${end.y}l-4.5 3`
  return { d, tip }
}

function Edges({ nodes, edges, down }: {
  nodes: DagNode[]
  edges: EdgeRoute[]
  down: boolean
}): JSX.Element {
  const done = new Set(nodes.filter((n) => n.status === 'completed').map((n) => n.id))
  const out: JSX.Element[] = []
  edges.forEach(({ from, to, points }) => {
    const { d, tip } = edgePath(points, down)
    const flowed = done.has(from) ? ' flowed' : ''
    out.push(<path key={`e${from}-${to}`} d={d} data-from={from} className={'edge' + flowed} />)
    out.push(<path key={`t${from}-${to}`} d={tip} data-from={from} className={'tip' + flowed} />)
  })
  return <>{out}</>
}

export function DagGraph({
  dims,
  nodes,
  now,
  onPick,
  selectedId = null,
  flow = 'across',
  renderNode,
}: DagGraphProps): JSX.Element {
  const { at, edges, width, height } = layout(nodes, dims, flow)

  return (
    <div className="daggraph">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <Edges nodes={nodes} edges={edges} down={flow === 'down'} />
        {nodes.map((n) => {
          const p = at.get(n.id)
          if (!p) return null
          const pick = (): void => onPick(n)
          /* The node's place is written on the box rather than as a
             `transform` on the group. WebKit paints a `foreignObject` without
             its ancestor group's transform -- the box is hit-tested where the
             layout put it but drawn at the SVG's origin -- so on Safari every
             card in the task board landed on the first node's spot and the
             arrows pointed at empty canvas. Absolute x/y on the box itself is
             honoured by every engine. */
          return (
            <g key={n.id} role="button" tabIndex={0} className="nd"
              data-st={n.status || 'pending'} data-node={n.id}
              {...(selectedId === n.id ? { 'data-sel': '1' } : {})}
              onClick={pick}
              onKeyDown={(e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return
                e.preventDefault()
                pick()
              }}>
              <foreignObject x={p.x} y={p.y} width={dims.W} height={dims.H}>
                {renderNode(n, now)}
              </foreignObject>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
