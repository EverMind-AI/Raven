/* The board's renderer: layout, edges, selection, and click and keyboard on
 * each node. The caller draws every node's own box, handed to it through
 * `renderNode` and laid out inside a `foreignObject` sized to `dims`.
 */

import { layout } from './graph'

import type { Dims, Flow } from './graph'
import type { DagNode } from './types'
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

/* One edge, drawn along whichever way the graph runs: out of the downstream
   face of the upstream box and into the upstream face of the next one, with the
   curve's control points on the same axis so a fan-out leaves as a fan rather
   than as a sheaf of diagonals. */
function edgePath(from: { x: number; y: number }, to: { x: number; y: number }, dims: Dims, down: boolean): {
  d: string
  tip: string
} {
  if (down) {
    const x1 = from.x + dims.W / 2
    const y1 = from.y + dims.H
    const x2 = to.x + dims.W / 2
    const y2 = to.y - 5
    const mid = (y1 + y2) / 2
    return {
      d: `M${x1} ${y1} C${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`,
      tip: `M${x2 - 3} ${y2 - 3.5}L${x2} ${y2 + 1}l3 -4.5`,
    }
  }
  const x1 = from.x + dims.W
  const y1 = from.y + dims.H / 2
  const x2 = to.x - 5
  const y2 = to.y + dims.H / 2
  const mid = (x1 + x2) / 2
  return {
    d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`,
    tip: `M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`,
  }
}

function Edges({ dims, nodes, at, down }: {
  dims: Dims
  nodes: DagNode[]
  at: Map<string, { x: number; y: number }>
  down: boolean
}): JSX.Element {
  const done = new Set(nodes.filter((n) => n.status === 'completed').map((n) => n.id))
  const out: JSX.Element[] = []
  nodes.forEach((n) => {
    n.depends_on.forEach((pid) => {
      const a = at.get(pid)
      const b = at.get(n.id)
      if (!a || !b) return
      const { d, tip } = edgePath(a, b, dims, down)
      const flowed = done.has(pid) ? ' flowed' : ''
      out.push(<path key={`e${pid}-${n.id}`} d={d} data-from={pid} className={'edge' + flowed} />)
      out.push(<path key={`t${pid}-${n.id}`} d={tip} data-from={pid} className={'tip' + flowed} />)
    })
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
  const { at, width, height } = layout(nodes, dims, flow)

  return (
    <div className="daggraph">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <Edges dims={dims} nodes={nodes} at={at} down={flow === 'down'} />
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
