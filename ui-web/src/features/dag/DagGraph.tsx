/* The one SVG renderer for a DAG, shared by the composer sheet and the
 * transcript tool-call card. The surfaces choose their scale and lifecycle;
 * node geometry, label fitting, status marks, edges and interaction stay one
 * implementation.
 */

import { t } from '../../i18n/t'
import { MARKS, depths, layout, took } from './graph'
import { trimShared } from './labels'

import type { Dims, Flow } from './graph'
import type { DagNode, NodeStatus } from './types'
import type { JSX } from 'react'

export type DagSurface = 'card' | 'sheet'

/* A caller's own box for a node, in place of the default one below. Given the
   node and the same clock the default box's own duration reads, and laid out
   inside a `foreignObject` sized to the node's box -- edges, layout,
   selection and click/keyboard handling all stay DagGraph's; only what a node
   looks like changes. */
export type CardRenderer = (node: DagNode, now: number) => JSX.Element

interface DagGraphProps {
  dims: Dims
  nodes: DagNode[]
  now: number
  onPick: (node: DagNode) => void
  selectedId?: string | null
  stopPropagation?: boolean
  surface: DagSurface
  /* Left to right unless the surface says otherwise. The task board reads top
     to bottom; see `Flow` in ./graph. */
  flow?: Flow
  /* Unset on every caller but the tasks board: default is today's SVG box
     (rect, mark, two-line label, tooltip), byte-for-byte. */
  renderNode?: CardRenderer
}

const CARD_LAYERS = 5

/* What a node's box is titled by. The summary is what the model was required to
   write about this step -- "research the current hot topics and pick two" -- and
   the id is a key: a playbook namespaces it, so a graph's ids share their first
   twenty characters and differ in the tail the box has least room for. The id is
   still reachable, one row down in the node panel, where the fields a dependency
   or a run dir is keyed by belong. */
const nodeLabel = (n: DagNode): string => n.node_summary || n.id

export function visibleLayers(nodes: DagNode[], limit = CARD_LAYERS): {
  hiddenLayers: number
  nodes: DagNode[]
} {
  const at = depths(nodes)
  const count = Math.max(...nodes.map((n) => at.get(n.id) || 0), -1) + 1
  if (count <= limit) return { hiddenLayers: 0, nodes }
  return {
    hiddenLayers: count - limit,
    nodes: nodes.filter((n) => (at.get(n.id) || 0) < limit),
  }
}

function Working({ x, y }: { x: number; y: number }): JSX.Element {
  return (
    <g className="workv" aria-hidden="true">
      {[0, 4, 8].map((dx) => <circle key={dx} cx={x - 4 + dx} cy={y} r={1.5} />)}
    </g>
  )
}

function Mark({ status, x, y }: { status: NodeStatus | string; x: number; y: number }): JSX.Element {
  if (status === 'running') return <Working x={x} y={y} />
  const mark = MARKS[status]
  if (mark) return <path d={mark.d} transform={`translate(${x} ${y})`} className={'mk ' + mark.cls} />
  return <circle cx={x} cy={y} r={3.6} className="mk wait" />
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
  stopPropagation = false,
  surface,
  flow = 'across',
  renderNode,
}: DagGraphProps): JSX.Element {
  const visible = surface === 'card' ? visibleLayers(nodes) : { hiddenLayers: 0, nodes }
  const shown = visible.nodes
  const { at, width, height } = layout(shown, dims, flow)
  const card = surface === 'card'
  const markX = card ? 16 : 17
  const labelX = card ? 29 : 31
  const pad = card ? 9 : 11
  /* The room the text has, which is the box minus the mark beside it. It is
     given to the label as a width and the box cuts what does not fit; the
     number below is read for one thing only, whether these labels are long
     enough to be worth stripping a shared namespace from. */
  const room = Math.max(0, dims.W - labelX - pad)
  const labels = trimShared(shown.map(nodeLabel), room, card ? 11 : 12)

  const held = new Map<string, number>()
  nodes.forEach((n) => { if (n.instance) held.set(n.instance, (held.get(n.instance) || 0) + 1) })

  return (
    <div className="canvas daggraph" data-flow={flow} data-surface={surface} data-hidden-layers={visible.hiddenLayers || undefined}>
      {visible.hiddenLayers
        ? <div className="dagcap">{t('gui.dag.more_layers', { n: visible.hiddenLayers })}</div>
        : null}
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <Edges dims={dims} nodes={shown} at={at} down={flow === 'down'} />
        {shown.map((n, i) => {
          const p = at.get(n.id)
          if (!p) return null
          const handle = n.instance && (held.get(n.instance) || 0) > 1 ? ' @' + n.instance : ''
          const pick = (): void => onPick(n)
          return (
            <g key={n.id} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0} className="nd"
              data-st={n.status || 'pending'} data-node={n.id}
              {...(selectedId === n.id ? { 'data-sel': '1' } : {})}
              onClick={(e) => { if (stopPropagation) e.stopPropagation(); pick() }}
              onKeyDown={(e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return
                e.preventDefault()
                if (stopPropagation) e.stopPropagation()
                pick()
              }}>
              {renderNode
                ? (
                  <foreignObject x={0} y={0} width={dims.W} height={dims.H}>
                    {renderNode(n, now)}
                  </foreignObject>
                )
                : (
                  <>
                    <rect width={dims.W} height={dims.H} rx={9} />
                    <Mark status={n.status} x={markX} y={dims.H / 2} />
                    {/* Laid out as HTML inside the box rather than as SVG text
                        beside it: a line that is longer than its node then ends
                        where the node ends, at whatever width the box is and
                        whenever the font finally arrives, instead of at a
                        length something measured once and wrote down. */}
                    <foreignObject x={labelX} y={0} width={room} height={dims.H}>
                      <div className="lbl">
                        <div className="ln">
                          <span className={n.node_summary ? 'id prose' : 'id'}>{labels[i]}</span>
                          {card ? null : <span className="tm">{took(n, now)}</span>}
                        </div>
                        <div className="ln">
                          <span className="ag">{n.subagent + handle}</span>
                          {card ? <span className="tm">{n.status === 'running' ? '' : took(n, now)}</span> : null}
                        </div>
                      </div>
                    </foreignObject>
                    {/* Both, because the box shows one of them truncated: the
                        summary is what the step is for, the id is what
                        everything else keys on. */}
                    <title>{[n.node_summary, n.id, n.subagent + (n.instance ? ' @' + n.instance : '')]
                      .filter(Boolean).join(' \u00b7 ')}</title>
                  </>
                )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
