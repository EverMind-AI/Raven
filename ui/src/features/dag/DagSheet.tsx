/* The dag sheet: the graph a `run_subagent_dag` call is orchestrating, docked
 * above the composer on the conversation that asked for it.
 *
 * A tenant of the rack, beside the clarify and approval sheets. It was built
 * imperatively in the live layer, which is why it is arriving here rather than
 * being written here: the geometry, the marks and the summary lines had already
 * moved into features/dag/, and what was left was the assembly and one
 * measure-then-adjust pass over the node labels.
 *
 * Two things the imperative version did by hand that React does structurally.
 * It kept `d.els` -- a map from node id to that node's group, mark and clock
 * element -- so a status change could be written into one box instead of
 * rebuilding the sheet, because rebuilding slid the sheet back in from the
 * bottom, reset the canvas scroll and dropped the reader's focus. Re-rendering
 * here patches the same three places for the same reason, without the map. And
 * it stopped its own clock, from four call sites; the clock is now an effect
 * that stops when nothing is running and when the sheet leaves the rack.
 */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { ds, t } from '../../shell/bridge'
import { CHEVRON_DOWN, CROSS, Glyph } from '../../shell/ico'
import { getState as subState, subscribe as subSubscribe } from '../subagents/store'
import { MARKS, SHEET, gist, layout, summary, took } from './graph'
import { fitLabels } from './labels'
import * as store from './store'

import type { DagNode, DagRun } from './types'
import type { TranscriptSource } from '../transcript/types'
import type { JSX } from 'react'

const { W, H } = SHEET
/* What a label has, once the status mark's gutter and the clock's reserved
   column are taken out of the box. The clock's column is held whether or not a
   time is showing yet: a node that starts running must not need a re-fit. */
const LABEL_ROOM = W - 31 - 40

const anyRunning = (d: DagRun): boolean => [...d.nodes.values()].some((n) => n.status === 'running')

/* Three circles, the same glyph the turn's own row and a sub-agent row wear for
   work in progress -- one alphabet for "this is happening", whatever is doing
   it. The imperative twin is workGlyphSvg in features/composer/mount.tsx, called
   with the same two numbers this is placed at. */
function Working(): JSX.Element {
  return (
    <g className="workv" aria-hidden="true">
      {[0, 4, 8].map((dx) => <circle key={dx} cx={13 + dx} cy={21.5} r={1.5} />)}
    </g>
  )
}

function Mark({ status }: { status: string }): JSX.Element {
  if (status === 'running') return <Working />
  const mark = MARKS[status]
  if (mark) return <path d={mark.d} transform={`translate(17 ${H / 2})`} className={'mk ' + mark.cls} />
  return <circle cx={17} cy={H / 2} r={3.6} className="mk wait" />
}

function Edges({ d, at }: { d: DagRun; at: Map<string, { x: number; y: number }> }): JSX.Element {
  const out: JSX.Element[] = []
  d.order.forEach((id) => {
    const n = d.nodes.get(id)
    if (!n) return
    ;(n.depends_on || []).forEach((pid) => {
      const a = at.get(pid)
      const b = at.get(n.id)
      if (!a || !b) return
      const x1 = a.x + W
      const y1 = a.y + H / 2
      const x2 = b.x - 5
      const y2 = b.y + H / 2
      const mid = (x1 + x2) / 2
      /* Faint until the node it leaves has finished: what has actually flowed
         through the graph so far is the thing a reader is looking for. A class
         on a path, so it can change without a relayout. */
      const flowed = (d.nodes.get(pid) || {}).status === 'completed' ? ' flowed' : ''
      out.push(
        <path key={`e${pid}-${id}`} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`}
          data-from={pid} className={'edge' + flowed} />,
      )
      /* The head is its own path: a marker-end inherits the line's stroke width
         and ends up heavier than the line it caps. */
      out.push(
        <path key={`t${pid}-${id}`} d={`M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`}
          data-from={pid} className={'tip' + flowed} />,
      )
    })
  })
  return <>{out}</>
}

/* The `.dsheet` element itself is the host the rack files under a conversation,
   so this renders its children rather than the sheet: the rack writes
   `data-sess` on what it is handed, and a wrapper div around the sheet would put
   that -- and the flex item `.dock .sheets > *` styles -- on the wrapper instead.
   The two attributes React cannot own on a container it did not create are set
   with it (mount.tsx) and kept in step below.

   `onClose` is a prop rather than a store action because closing destroys that
   host, and the host belongs to mount.tsx. Folding is a store action for the
   opposite reason: the flag rides on the run, so it survives the reader
   switching conversations and coming back. */
export function Sheet({ sess, host, onClose }: { sess: string; host: HTMLElement; onClose: () => void }): JSX.Element | null {
  useSyncExternalStore(store.subscribe, store.version)
  const sub = useSyncExternalStore(subSubscribe, subState)
  const d = store.run(sess)
  const ids = useRef(new Map<string, SVGTextElement>())
  const [fitted, setFitted] = useState<Map<string, string> | null>(null)
  const [, tick] = useState(0)

  /* Folding is a class-free attribute on the sheet's own element, which React
     does not own: it is the container this tree was rendered into. */
  useLayoutEffect(() => { host.dataset.fold = String(!!(d && d.folded)) })

  /* A new run is a new set of labels: whatever the last one measured says
     nothing about this one. */
  const runId = d ? d.run_id : ''
  useLayoutEffect(() => { setFitted(null) }, [runId])

  /* The fit, which is the one thing here that has to happen after layout:
     `getComputedTextLength` reads real glyph widths, and a detached sheet -- one
     filed under a conversation the reader is not looking at -- measures every
     label as zero. So it waits until the rack has mounted it, and the sync the
     live layer calls on a session change is what brings it back here. */
  useLayoutEffect(() => {
    if (!d || fitted) return
    const els = d.order.map((id) => ids.current.get(id)).filter(Boolean) as SVGTextElement[]
    if (els.length !== d.order.length || !els[0]?.isConnected) return
    /* SVG text measurement is the one thing here that needs a real layout
       engine. Where there is none the labels stay as their author wrote them,
       which is the same thing that happens to a label that fits. */
    if (typeof els[0].getComputedTextLength !== 'function') return
    const raw = els.map((el) => el.textContent || '')
    const out = fitLabels(raw, LABEL_ROOM, (label, i) => {
      const el = els[i] as SVGTextElement
      el.textContent = label
      return el.getComputedTextLength()
    })
    setFitted(new Map(d.order.map((id, i) => [id, out[i] as string])))
  })

  /* Only two events ever arrive for a node: it started, and it ended. Between
     them nothing is sent, so a number drawn once sat frozen for exactly the
     interval a reader is watching it for -- a node that took four minutes read
     "1.0s" for all four and then jumped. */
  const running = !!d && host.isConnected && anyRunning(d)
  useEffect(() => {
    if (!running) return undefined
    const h = setInterval(() => tick((v) => v + 1), 1000)
    return () => clearInterval(h)
  }, [running])

  if (!d) return null
  const nodes = d.order.map((id) => d.nodes.get(id)).filter(Boolean) as DagNode[]
  const { at, width, height } = layout(nodes)
  const sel = sub.open && sub.open.kind === 'dag' ? sub.open : null
  /* A handle earns its place in the box only when it is shared, which is when it
     means "these steps continue one session". A handle held by a single node is
     minted per node and reads as a mangled copy of the id above it. */
  const held = new Map<string, number>()
  nodes.forEach((n) => { if (n.instance) held.set(n.instance, (held.get(n.instance) || 0) + 1) })
  const foldLabel = t(d.folded ? 'gui.dag.unfold' : 'gui.dag.fold')

  return (
    <>
      <div className="hd">
        <span className="ttl">{t('gui.dag.title')}</span>
        <div className={d.done ? 'sum' : 'gist'}>{d.done ? summary(d) : gist(d)}</div>
        <button className="ic tipdn" data-tip={foldLabel} aria-label={foldLabel}
          onClick={() => store.fold(sess, !d.folded)}>
          <Glyph d={CHEVRON_DOWN} cls="cv" />
        </button>
        <button className="ic tipdn" data-tip={t('gui.dag.close')} aria-label={t('gui.dag.close')}
          onClick={onClose}>
          <Glyph d={CROSS} />
        </button>
      </div>
      <div className="canvas">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
          <Edges d={d} at={at} />
          {nodes.map((n) => {
            const p = at.get(n.id)
            if (!p) return null
            /* A node opens where a sub-agent's work already lives rather than
               growing a second transcript inside the sheet: the sheet stays the
               map rather than becoming the territory. Out through the same door
               the trail's dag card already uses -- one operation, one seam, and
               the panel chrome that goes with it stays on the live side. */
            const open = (): void => {
              ds<TranscriptSource>('transcript').openDagNode?.(d.run_id, n.id)
            }
            const label = (fitted && fitted.get(n.id)) ?? n.id
            const handle = n.instance && (held.get(n.instance) || 0) > 1 ? ' @' + n.instance : ''
            return (
              <g key={n.id} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0} className="nd"
                data-st={n.status || 'pending'} data-node={n.id}
                {...(sel && sel.run_id === d.run_id && sel.node === n.id ? { 'data-sel': '1' } : {})}
                onClick={open}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() }
                }}>
                <rect width={W} height={H} rx={9} />
                <Mark status={n.status} />
                <text x={31} y={19} className="id" ref={(el) => {
                  if (el) ids.current.set(n.id, el)
                  else ids.current.delete(n.id)
                }}>{label}</text>
                <text x={31} y={32} className="ag">{n.subagent + handle}</text>
                {/* Always present, even while empty: the clock writes into it
                    every second, and a node that starts running must not have to
                    be redrawn to grow somewhere to put its time. */}
                <text x={W - 11} y={19} textAnchor="end" className="tm">{took(n, Date.now())}</text>
                {/* The graph is a map, not a table: what a node is and who is
                    running it are in the box, and how long is on the title for
                    the one being pointed at. */}
                <title>{`${n.id} · ${n.subagent}${n.instance ? ' @' + n.instance : ''}`}</title>
              </g>
            )
          })}
        </svg>
      </div>
    </>
  )
}
