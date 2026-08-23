/* The one SVG renderer for a DAG, shared by the composer sheet and the
 * transcript tool-call card. The surfaces choose their scale and lifecycle;
 * node geometry, label fitting, status marks, edges and interaction stay one
 * implementation.
 */

import { useLayoutEffect, useRef, useState } from 'react'

import { t } from '../../shell/bridge'
import { MARKS, depths, layout, took } from './graph'
import { fitLabels } from './labels'

import type { DagNode, NodeStatus } from './types'
import type { Dims } from './graph'
import type { JSX } from 'react'

export type DagSurface = 'card' | 'sheet'

interface DagGraphProps {
  active?: boolean
  dims: Dims
  nodes: DagNode[]
  now: number
  onPick: (node: DagNode) => void
  selectedId?: string | null
  stopPropagation?: boolean
  surface: DagSurface
}

interface FittedLabels {
  key: string
  values: Map<string, string>
}

const CARD_LAYERS = 5

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

function Edges({ dims, nodes, at }: {
  dims: Dims
  nodes: DagNode[]
  at: Map<string, { x: number; y: number }>
}): JSX.Element {
  const done = new Set(nodes.filter((n) => n.status === 'completed').map((n) => n.id))
  const out: JSX.Element[] = []
  nodes.forEach((n) => {
    n.depends_on.forEach((pid) => {
      const a = at.get(pid)
      const b = at.get(n.id)
      if (!a || !b) return
      const x1 = a.x + dims.W
      const y1 = a.y + dims.H / 2
      const x2 = b.x - 5
      const y2 = b.y + dims.H / 2
      const mid = (x1 + x2) / 2
      const flowed = done.has(pid) ? ' flowed' : ''
      out.push(
        <path key={`e${pid}-${n.id}`} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`}
          data-from={pid} className={'edge' + flowed} />,
      )
      out.push(
        <path key={`t${pid}-${n.id}`} d={`M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`}
          data-from={pid} className={'tip' + flowed} />,
      )
    })
  })
  return <>{out}</>
}

export function DagGraph({
  active = true,
  dims,
  nodes,
  now,
  onPick,
  selectedId = null,
  stopPropagation = false,
  surface,
}: DagGraphProps): JSX.Element {
  const visible = surface === 'card' ? visibleLayers(nodes) : { hiddenLayers: 0, nodes }
  const shown = visible.nodes
  const { at, width, height } = layout(shown, dims)
  const refs = useRef(new Map<string, SVGTextElement>())
  const [fitted, setFitted] = useState<FittedLabels | null>(null)
  const card = surface === 'card'
  const markX = card ? 16 : 17
  const labelX = card ? 29 : 31
  const idY = card ? 17 : 19
  const metaY = card ? 28 : 32
  const clockY = card ? 28 : 19
  const labelRoom = dims.W - labelX - (card ? 9 : 40)
  const fitKey = JSON.stringify([dims.W, surface, shown.map((n) => n.id)])
  const fit = fitted?.key === fitKey ? fitted.values : null

  useLayoutEffect(() => {
    if (!active || fit || !shown.length) return
    const els = shown.map((n) => refs.current.get(n.id)).filter(Boolean) as SVGTextElement[]
    if (els.length !== shown.length) return
    const first = els[0]
    if (!first || typeof first.getComputedTextLength !== 'function') return
    const raw = shown.map((n) => n.id)
    const values = fitLabels(raw, labelRoom, (label, i) => {
      const el = els[i] as SVGTextElement
      el.textContent = label
      return el.getComputedTextLength()
    })
    setFitted({ key: fitKey, values: new Map(shown.map((n, i) => [n.id, values[i] as string])) })
  }, [active, fit, fitKey, labelRoom, shown])

  const held = new Map<string, number>()
  nodes.forEach((n) => { if (n.instance) held.set(n.instance, (held.get(n.instance) || 0) + 1) })

  return (
    <div className="canvas daggraph" data-surface={surface} data-hidden-layers={visible.hiddenLayers || undefined}>
      {visible.hiddenLayers
        ? <div className="dagcap">{t('gui.dag.more_layers', { n: visible.hiddenLayers })}</div>
        : null}
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <Edges dims={dims} nodes={shown} at={at} />
        {shown.map((n) => {
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
              <rect width={dims.W} height={dims.H} rx={9} />
              <Mark status={n.status} x={markX} y={dims.H / 2} />
              <text x={labelX} y={idY} className="id" ref={(el) => {
                if (el) refs.current.set(n.id, el)
                else refs.current.delete(n.id)
              }}>{fit?.get(n.id) ?? n.id}</text>
              <text x={labelX} y={metaY} className="ag">{n.subagent + handle}</text>
              <text x={dims.W - (card ? 9 : 11)} y={clockY} textAnchor="end" className="tm">
                {card && n.status === 'running' ? '' : took(n, now)}
              </text>
              <title>{`${n.id} \u00b7 ${n.subagent}${n.instance ? ' @' + n.instance : ''}`}</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
