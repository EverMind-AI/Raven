/* The playbook library: a wall of cards, and one playbook's graph.
 *
 * A card carries three things -- the name, the file's own one-line
 * `description`, and the shape of the graph. Nothing else: what a reader decides
 * from the wall is only which one to open, and every other field answers a
 * question that comes after that click.
 *
 * The detail view is the graph. A step's own configuration -- who runs it, what
 * it waits for, the prompt it carries, the skills and mcps and session handle
 * the author wrote on it -- sits in a panel beside the canvas, filled by
 * clicking a node. Nothing on this page paraphrases a field: the labels are the
 * field names, and the prose is the author's.
 */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import { layout } from '../dag/graph'
import { cardPlan, edge } from './shape'
import * as store from './store'

import type { Dims } from '../dag/graph'
import type { PlaybookDetail, PlaybookNode, PlaybookRow } from './types'
import type { JSX } from 'react'

/* One geometry, always. The card's diagram has two metrics because a card
   cannot be panned; this canvas can, so the graph never has to be redrawn
   smaller to fit -- the reader moves instead.
   A box holds an id and an executor. Everything a step actually says is prose of
   unknown length, which in a fixed box is a clipped fragment -- so it lives in
   the panel, where it has room. */
const ROOMY: Dims = { W: 200, H: 58, GAP_X: 256, GAP_Y: 82, PAD: 16 }

function Tile({ name }: { name: string }): JSX.Element {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return <span className={'pmtile th' + (h % 8)}>{(name[0] || '?').toUpperCase()}</span>
}

/* ── the card's concept diagram ─────────────────────────────────────── */

function Concept({ row }: { row: PlaybookRow }): JSX.Element {
  if (row.mode === 'prompt') {
    /* No stored graph to draw: the shape is composed per run. Three dashed
       middle boxes say "a fan-out of some width", which is the only true thing
       a picture can say here. */
    return (
      <svg
        className="pbrib"
        width="100%"
        height="84"
        viewBox="0 0 268 84"
        preserveAspectRatio="xMidYMid meet"
        aria-hidden="true"
      >
        <g transform="translate(53 23)">
          {[0, 1, 2].map((i) => {
            const y = i * 23
            return (
              <g key={i}>
                <path className="pbedge" d={edge(44, 23, 64, y + 7.5)} />
                <path className="pbedge" d={edge(108, y + 7.5, 128, 23)} />
                <rect className="pbcell open" x={64} y={y} width={44} height={15} rx={4} />
              </g>
            )
          })}
          <rect className="pbcell" x={0} y={15.5} width={44} height={15} rx={4} />
          <rect className="pbcell" x={128} y={15.5} width={44} height={15} rx={4} />
        </g>
      </svg>
    )
  }
  const plan = cardPlan(row.nodes)
  const at = new Map(plan.cells.map((c) => [c.id, c]))
  const { W, H } = plan.metric
  return (
    <svg
      className="pbrib"
      width="100%"
      height="84"
      viewBox="0 0 268 84"
      preserveAspectRatio="xMidYMid meet"
      aria-hidden="true"
    >
      <g transform={`translate(${Math.max(0, (268 - plan.width) / 2)} ${(84 - plan.height) / 2})`}>
        {row.nodes.map((n) =>
          n.depends_on.map((d) => {
            const a = at.get(d)
            const b = at.get(n.id)
            if (!a || !b) return null
            return <path key={`${d}-${n.id}`} className="pbedge" d={edge(a.x + W, a.y + H / 2, b.x, b.y + H / 2)} />
          })
        )}
        {plan.clipAt
          ? plan.cells
              .filter((c) => c.x + W + plan.metric.GX >= (plan.clipAt as { x: number }).x)
              .map((c) => (
                <path
                  key={'clip' + c.id}
                  className="pbedge cut"
                  d={edge(c.x + W, c.y + H / 2, (plan.clipAt as { x: number }).x - 4, plan.height / 2)}
                />
              ))
          : null}
        {plan.cells.map((c) => (
          <rect key={c.id} className="pbcell" x={c.x} y={c.y} width={W} height={H} rx={4} />
        ))}
        {plan.rowOverflow.map((o) => (
          <text key={'ro' + o.x} className="pbmore" x={o.x + 2} y={o.y + H / 2 + 3.5}>
            {'+' + o.n}
          </text>
        ))}
        {plan.clipAt && plan.hiddenSteps > 0 ? (
          <text className="pbmore" x={plan.clipAt.x} y={plan.clipAt.y + 3.5}>
            {t('gui.pb.more_steps', { n: plan.hiddenSteps })}
          </text>
        ) : null}
      </g>
    </svg>
  )
}

function Card({ row }: { row: PlaybookRow }): JSX.Element {
  const open = (): void => void store.open(row.name)
  return (
    <div
      className={'pbcard' + (row.disabled ? ' off' : '') + (row.error ? ' bad' : '')}
      role="button"
      tabIndex={0}
      onClick={row.error ? undefined : open}
      onKeyDown={(e) => {
        if (row.error) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <div className="pbtop">
        <Tile name={row.name} />
        <span className="pbnm">{row.name}</span>
        {row.disabled ? <span className="pbown">{t('gui.pb.disabled')}</span> : null}
      </div>
      <p className="pbwhen">{row.description}</p>
      {row.error ? (
        <div className="pbshape err">
          <span className="pberr">{t('gui.pb.unreadable', { why: row.error })}</span>
        </div>
      ) : (
        <div className="pbshape">
          <Concept row={row} />
          {/* Only where there is no graph to read: a prompt-mode playbook stores
              none, and the dashed boxes above are otherwise unexplained. A dag
              card says nothing here -- its drawing is the statement. */}
          {row.mode === 'prompt' ? <span className="pbcap">{t('gui.pb.shape_live')}</span> : null}
        </div>
      )}
    </div>
  )
}

function Library(): JSX.Element {
  const s = store.getState()
  const rows = store.visible()
  if (s.rows === null) {
    return (
      <>
        <div className="pmhero">
          <h3>{t('gui.nav.pb')}</h3>
        </div>
        <div className="pbgrid">
          {Array.from({ length: 6 }, (_, i) => (
            <div className="pbcard skel" key={i} aria-hidden="true">
              <div className="pbtop">
                <span className="sk" style={{ width: 34, height: 34, borderRadius: 10, flex: 'none' }} />
                <span className="sk" style={{ width: 96, height: 12 }} />
              </div>
              <span className="sk" style={{ width: '100%', height: 10 }} />
              <span className="sk" style={{ width: '74%', height: 10 }} />
              <div className="pbshape" />
            </div>
          ))}
        </div>
      </>
    )
  }
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.nav.pb')}</h3>
      </div>
      <div className="cbar">
        <div className="cfind">
          <svg
            className="ic"
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="M20 20l-4.3-4.3" />
          </svg>
          <input
            value={s.query}
            placeholder={t('gui.pb.search')}
            aria-label={t('gui.pb.search')}
            onChange={(e) => store.search(e.target.value)}
          />
        </div>
        {/* While a query is on, the count is about the query -- a total nobody
            asked for is the less useful of the two answers. */}
        <span className="pbcount">
          {s.err
            ? ''
            : s.query
              ? t('gui.pb.matched', { n: rows.length })
              : t('gui.pb.count', { n: (s.rows || []).length })}
        </span>
      </div>
      {/* A read that failed and a library that is empty look identical once the
          list is `[]`, and saying both at once tells the reader the wrong one.
          The failure is the only honest answer until a read succeeds. */}
      {s.err ? (
        <div className="pberr">{s.err}</div>
      ) : rows.length === 0 ? (
        <div className="empty-note">{t(s.query ? 'gui.pb.none_match' : 'gui.pb.none')}</div>
      ) : (
        <div className="pbgrid">
          {rows.map((r) => (
            <Card key={r.name} row={r} />
          ))}
        </div>
      )}
    </>
  )
}

/* ── the graph ──────────────────────────────────────────────────────── */

/* Three states, kept three. `null` on the wire and a field an older answer
   simply omitted both mean "the author wrote nothing"; `[]` means they wrote an
   empty list, which is a different instruction. Collapsing the two would make
   the panel say "not written" about a line someone deliberately wrote. */
type Written = { state: 'unwritten' } | { state: 'empty' } | { state: 'list'; items: string[] }

const written = (v: string[] | null | undefined): Written =>
  v == null ? { state: 'unwritten' } : v.length === 0 ? { state: 'empty' } : { state: 'list', items: v }

/* Placeholders are the one thing in a prompt a reader has to be able to pick out
   at a glance: `${...}` is filled before dispatch, `{{...}}` at run time. */
function Prompt({ text }: { text: string }): JSX.Element {
  const parts = text.split(/(\$\{[^}]*\}|\{\{[^}]*\}\})/g)
  return (
    <pre className="pbprompt">
      {parts.map((p, i) =>
        p.startsWith('${') ? (
          <i className="ph now" key={i}>
            {p}
          </i>
        ) : p.startsWith('{{') ? (
          <i className="ph run" key={i}>
            {p}
          </i>
        ) : (
          <span key={i}>{p}</span>
        )
      )}
    </pre>
  )
}

function Canvas({
  detail,
  picked,
  dims
}: {
  detail: PlaybookDetail
  picked: string | null
  dims: Dims
}): JSX.Element {
  const nodes = detail.nodes
  const { at, width, height } = layout(nodes, dims)
  /* Session groups: nodes sharing (subagent, instance). The only fact the arrows
     cannot carry -- an edge says "after", not "in the same session". */
  const groups = new Map<string, PlaybookNode[]>()
  nodes.forEach((n) => {
    if (!n.instance) return
    const key = n.subagent + '@' + n.instance
    const g = groups.get(key)
    if (g) g.push(n)
    else groups.set(key, [n])
  })
  return (
    <div className="pbcanvas" style={{ width, height }}>
      {[...groups.entries()].map(([key, members]) => {
        if (members.length < 2) return null
        const pts = members.map((m) => at.get(m.id)).filter(Boolean) as Array<{ x: number; y: number }>
        if (pts.length < 2) return null
        const x0 = Math.min(...pts.map((p) => p.x)) - 11
        const y0 = Math.min(...pts.map((p) => p.y)) - 11
        const x1 = Math.max(...pts.map((p) => p.x)) + dims.W + 11
        const y1 = Math.max(...pts.map((p) => p.y)) + dims.H + 11
        return (
          <div className="pblane" key={key} style={{ left: x0, top: y0, width: x1 - x0, height: y1 - y0 }}>
            <b>{t('gui.pb.lane', { handle: members[0]?.instance || '' })}</b>
          </div>
        )
      })}
      <svg className="pbedges" width={width} height={height} aria-hidden="true">
        {nodes.map((n) =>
          n.depends_on.map((d) => {
            const a = at.get(d)
            const b = at.get(n.id)
            if (!a || !b) return null
            const x1 = a.x + dims.W
            const y1 = a.y + dims.H / 2
            const x2 = b.x - 7
            const y2 = b.y + dims.H / 2
            return (
              <g key={`${d}-${n.id}`}>
                <path className="pbwire" d={edge(x1, y1, x2, y2)} />
                <path className="pbtip" d={`M${x2 - 4} ${y2 - 3.5}L${x2 + 1} ${y2}l-5 3.5`} />
              </g>
            )
          })
        )}
      </svg>
      {nodes.map((n) => {
        const p = at.get(n.id)
        if (!p) return null
        const blank = !n.subagent || !n.prompt_template
        return (
          <button
            key={n.id}
            className={'pbnode' + (blank ? ' blank' : '')}
            data-picked={n.id === picked || undefined}
            style={{ left: p.x, top: p.y, width: dims.W, height: dims.H }}
            onClick={() => store.pick(n.id)}
          >
            {/* The step's name, kept short: what it does is prose of unknown
                length and reads whole in the panel. */}
            <span className="l1">{n.id}</span>
            <span className="l2">
              <span className="ag">{n.subagent || t('gui.pb.blank_agent')}</span>
              {n.instance ? <span className="hd">@{n.instance}</span> : null}
              {written(n.skills).state === 'list' ? (
                <span className="mk">{t('gui.pb.n_skills', { n: (n.skills || []).length })}</span>
              ) : null}
              {written(n.mcps).state === 'list' ? (
                <span className="mk dim">{t('gui.pb.n_mcps', { n: (n.mcps || []).length })}</span>
              ) : null}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/* One of the three states. Absent is a gap in the column and an empty list is
   the file's own two characters -- neither gets a sentence about it. */
function Listed({ of, carried }: { of: Written; carried?: string[] }): JSX.Element {
  if (of.state === 'unwritten') return <span className="gap">{t('gui.pb.unwritten')}</span>
  if (of.state === 'empty') return <span className="gap">{t('gui.pb.empty_list')}</span>
  const shipped = new Set(carried || [])
  return (
    <span className="chips">
      {of.items.map((x) => (
        <span className={shipped.has(x) ? 'tag own' : 'tag'} key={x} title={shipped.has(x) ? t('gui.pb.carried') : undefined}>
          {x}
        </span>
      ))}
    </span>
  )
}

/* An input's value is one of three forms the spec allows, and which form it is
   is the interesting part -- a path and a node id read alike otherwise. */
function InputValue({ value }: { value: unknown }): JSX.Element {
  const shape = value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  const file = shape && typeof shape.file === 'string' ? shape.file : null
  const from = shape && typeof shape.node === 'string' ? shape.node : null
  if (file || from) {
    return (
      <>
        <span className="kind">{file ? 'file' : 'node'}</span>
        <span className="val">{file || from}</span>
      </>
    )
  }
  return <span className="val">{typeof value === 'string' ? value : JSON.stringify(value)}</span>
}

/* One step, as the file wrote it.
   `dependsOn` and `instance` are deliberately absent: the arrows already say
   what waits for what, and a shared session is drawn as the lane around its
   members with the handle on every box inside it. Repeating either here would be
   a worse copy of a picture the reader is already looking at. */
function NodePanel({ node, carried }: { node: PlaybookNode | null; carried?: string[] }): JSX.Element {
  if (!node) return <div className="pbpanel empty">{t('gui.pb.pick_step')}</div>
  const inputs = Object.entries(node.inputs || {})
  return (
    <div className="pbpanel">
      <div className="pbphead">
        <b>{node.id}</b>
        {node.node_summary ? <p>{node.node_summary}</p> : null}
      </div>
      <dl className="kv pbkv">
        <dt>subagent</dt>
        <dd>
          {node.subagent ? (
            <span className="who">{node.subagent}</span>
          ) : (
            /* Left for the caller to fill, which is the one thing on this panel
               a reader has to do something about. */
            <span className="tag warn">{t('gui.pb.blank_agent')}</span>
          )}
        </dd>
        <dt>skills</dt>
        <dd>
          <Listed of={written(node.skills)} />
        </dd>
        <dt>mcps</dt>
        <dd>
          {/* Marked, because the name alone does not say which server it is:
              one the playbook ships travels with the file, one it does not is
              whatever this machine has configured under that name. */}
          <Listed of={written(node.mcps)} carried={carried} />
        </dd>
      </dl>
      {inputs.length ? (
        <div className="pbpsec">
          <span className="cap">inputs</span>
          <dl className="pbin">
            {inputs.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>
                  <InputValue value={v} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
      <div className="pbpsec">
        <span className="cap">promptTemplate</span>
        {node.prompt_template ? (
          <Prompt text={node.prompt_template} />
        ) : (
          <span className="tag warn">{t('gui.pb.blank_prompt')}</span>
        )}
      </div>
    </div>
  )
}

/* The viewport the graph is read in: the reader pans and zooms it, so nothing
   here decides on their behalf how much of the graph they should be looking at.
   The opening view frames the whole graph, and after that the view is theirs. */
const ZOOM_MIN = 0.3
const ZOOM_MAX = 2
const ZOOM_STEP = 1.15
/* Two devices, one event. A trackpad sends a stream of small deltas per flick,
   so those zoom in proportion to how far it actually moved and glide. A mouse
   sends one large notch, which is a discrete press and gets a discrete step --
   the same one the button gives, so the two controls agree. Treating a notch as
   a proportional delta is what makes a mouse wheel either crawl or bolt. */
const WHEEL_GAIN = 0.008
const MOUSE_NOTCH = 50
/* A pointer that moved less than this between down and up was a click on
   whatever is under it, not a drag of the canvas. */
const DRAG_SLOP = 4
/* Breathing room between the graph and the viewport edge when the graph is too
   big to centre. */
const EDGE = 14

interface View {
  x: number
  y: number
  z: number
}

const clampZoom = (z: number): number => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z))

function Board({ detail, picked }: { detail: PlaybookDetail; picked: string | null }): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  const [port, setPort] = useState({ w: 0, h: 0 })
  const [view, setView] = useState<View | null>(null)
  const size = layout(detail.nodes, ROOMY)

  /* Measured on every render, plus a frame-by-frame retry while there is nothing
     to measure, and NOT on a notification alone.
     The island mounts into `#pbBody` at boot, while the page is still
     `display: none` -- so the first measurement is always zero-width, and a
     design that settled for 1:1 there would frame every graph wrongly until
     something happened to resize the box. A ResizeObserver rescues that in a
     browser; it is silent in the embedded pane this was verified in, and
     `window.resize` never fired there either. Both are kept as the cheap path,
     but correctness does not depend on either. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    let frame = 0
    let slow = 0
    let tries = 0
    const measure = (): void => {
      const w = el.clientWidth
      const h = el.clientHeight
      if (w <= 0 || h <= 0) {
        /* A burst of frames covers the usual case -- the page is being shown
           right now and the box has a size one frame from here. Past that it is
           hidden for as long as the reader is elsewhere, so the watch drops to a
           slow poll rather than stopping: stopping is what leaves the graph
           framed against a size it no longer has. */
        if (tries++ < 60) frame = requestAnimationFrame(measure)
        else if (!slow) slow = window.setInterval(measure, 500)
        return
      }
      tries = 0
      if (slow) {
        window.clearInterval(slow)
        slow = 0
      }
      setPort((prev) => (prev.w === w && prev.h === h ? prev : { w, h }))
    }
    measure()
    window.addEventListener('resize', measure)
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    if (ro) ro.observe(el)
    return () => {
      if (frame) cancelAnimationFrame(frame)
      if (slow) window.clearInterval(slow)
      window.removeEventListener('resize', measure)
      if (ro) ro.disconnect()
    }
  })

  /* Centred at some zoom, never enlarged past life size: a three-step playbook
     blown up to fill the viewport would read as a bigger playbook. */
  const centred = (z: number): View => ({
    x: (port.w - size.width * z) / 2,
    y: (port.h - size.height * z) / 2,
    z
  })
  const fitZoom = (): number =>
    port.w && port.h ? clampZoom(Math.min(1, port.w / size.width, port.h / size.height)) : 1
  /* The whole graph at once -- what the zoom readout goes back to. */
  const framed = (): View => (port.w ? centred(fitZoom()) : { x: 0, y: 0, z: 1 })
  /* The whole graph is the opening view: a box carries an id and an agent name,
     which stay readable much further out than a paragraph would.
     Below the zoom floor it still overflows, and then it opens at its start
     rather than centred -- the flow reads left to right, and centring a graph
     too big to fit cuts off the first step and the last one at once, which looks
     like damage rather than like a big graph. */
  const opening = (): View => {
    if (!port.w) return { x: 0, y: 0, z: 1 }
    const z = fitZoom()
    const mid = centred(z)
    /* max, not min: a graph that fits keeps its centred offset, and one that
       overflows gets a positive margin at the start instead of the negative
       offset centring would give it. */
    return { x: Math.max(mid.x, EDGE), y: Math.max(mid.y, EDGE), z }
  }

  /* Reframed when the playbook changes or the viewport first has a size, and
     never again -- a reader who has panned somewhere keeps their view. */
  const fitKey = `${detail.name}:${port.w}x${port.h}`
  const lastFit = useRef('')
  if (port.w > 0 && lastFit.current !== fitKey) {
    lastFit.current = fitKey
    if (view === null) setView(opening())
  }
  const at = view ?? { x: 0, y: 0, z: 1 }

  /* Composed off the previous view, not off this render's copy of it: a wheel
     gesture delivers several events before React re-renders, and reading the
     zoom from the closure makes all of them compute the same result -- the
     flick lands as one step and the canvas feels stuck. */
  const zoomBy = (factor: number, about?: { x: number; y: number }): void => {
    setView((prev) => {
      const cur = prev ?? { x: 0, y: 0, z: 1 }
      const z = clampZoom(cur.z * factor)
      /* Zoom about a point: the graph coordinate under it has to stay under it,
         or the canvas swims away from wherever the reader was looking. */
      const cx = about ? about.x : port.w / 2
      const cy = about ? about.y : port.h / 2
      return { x: cx - ((cx - cur.x) / cur.z) * z, y: cy - ((cy - cur.y) / cur.z) * z, z }
    })
  }

  const drag = useRef<{ id: number; x: number; y: number; ox: number; oy: number; moved: boolean } | null>(null)
  const onDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    /* A node is a button and handles its own press; the canvas takes the rest. */
    if ((e.target as HTMLElement).closest('.pbnode')) return
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, ox: at.x, oy: at.y, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d || d.id !== e.pointerId) return
    const dx = e.clientX - d.x
    const dy = e.clientY - d.y
    if (!d.moved && Math.abs(dx) + Math.abs(dy) < DRAG_SLOP) return
    d.moved = true
    setView((prev) => ({ x: d.ox + dx, y: d.oy + dy, z: prev?.z ?? 1 }))
  }
  const onUp = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (d && d.id === e.pointerId) drag.current = null
  }
  /* Attached by hand, non-passive, because React registers `wheel` as a passive
     listener -- and in a passive listener `preventDefault` is a no-op. Through
     the `onWheel` prop the ctrl+wheel reached the browser as its own page-zoom
     gesture: the whole page grew while this canvas zoomed the other way. */
  useEffect(() => {
    const el = box.current
    if (!el) return
    const onWheel = (e: WheelEvent): void => {
      /* Plain wheel belongs to the page: this panel sits in a scrolling column,
         and stealing it would trap the reader inside the canvas. */
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      const r = el.getBoundingClientRect()
      /* deltaY is in lines or pages on some mice; normalise before reading it. */
      const raw = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * (port.h || 1) : e.deltaY
      const factor =
        Math.abs(raw) >= MOUSE_NOTCH ? (raw < 0 ? ZOOM_STEP : 1 / ZOOM_STEP) : Math.exp(-raw * WHEEL_GAIN)
      zoomBy(factor, { x: e.clientX - r.left, y: e.clientY - r.top })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })

  const onKey = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    const pan = e.shiftKey ? 120 : 40
    const step: Record<string, () => void> = {
      '+': () => zoomBy(ZOOM_STEP),
      '=': () => zoomBy(ZOOM_STEP),
      '-': () => zoomBy(1 / ZOOM_STEP),
      '0': () => setView(framed()),
      ArrowUp: () => setView((prev) => ({ ...(prev ?? at), y: (prev ?? at).y + pan })),
      ArrowDown: () => setView((prev) => ({ ...(prev ?? at), y: (prev ?? at).y - pan })),
      ArrowLeft: () => setView((prev) => ({ ...(prev ?? at), x: (prev ?? at).x + pan })),
      ArrowRight: () => setView((prev) => ({ ...(prev ?? at), x: (prev ?? at).x - pan }))
    }
    /* Arrow keys walk the steps while a step is picked (see PlaybooksApp); they
       pan the canvas only when the canvas itself has focus and nothing is. */
    if (/^Arrow/.test(e.key) && picked) return
    const run = step[e.key]
    if (!run) return
    e.preventDefault()
    run()
  }

  return (
    <div className="pbboard">
      <div
        className="pbstage"
        ref={box}
        tabIndex={0}
        role="application"
        aria-label={t('gui.pb.canvas')}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
      >
        <div className="pbview" style={{ transform: `translate(${at.x}px, ${at.y}px) scale(${at.z})` }}>
          <Canvas detail={detail} picked={picked} dims={ROOMY} />
        </div>
      </div>
      <div className="pbzoom">
        <button className="pbzb" aria-label={t('gui.pb.zoom_out')} onClick={() => zoomBy(1 / ZOOM_STEP)}>
          &minus;
        </button>
        {/* The percentage is the control that puts the whole graph back in view,
            not a note about what the page decided to do. */}
        <button className="pbzpct" onClick={() => setView(framed())} title={t('gui.pb.zoom_fit')}>
          {Math.round(at.z * 100)}%
        </button>
        <button className="pbzb" aria-label={t('gui.pb.zoom_in')} onClick={() => zoomBy(ZOOM_STEP)}>
          +
        </button>
      </div>
    </div>
  )
}

/* Which of the three forms a default takes. A param with no default and one
   whose default IS the empty string are different facts; a dash for both loses
   the second, the same way one gap for `skills` would lose the difference
   between unwritten and `[]`. */
function Default({ value }: { value: unknown }): JSX.Element {
  if (value === undefined || value === null) return <span className="gap">{t('gui.pb.no_default')}</span>
  if (value === '') return <span className="df">{'""'}</span>
  return <span className="df">{typeof value === 'string' ? value : JSON.stringify(value)}</span>
}

/* The runtime inputs, as the table they are: name, type, default, and the
   sentence the caller is asked when the value is missing. `required` rides on
   the name rather than taking a column of its own -- a column would print
   "no" once per optional param, which is most of them. */
function Params({ params }: { params: PlaybookDetail['params'] }): JSX.Element {
  const keys = Object.keys(params)
  if (!keys.length) return <p className="pbnone">{t('gui.pb.no_params')}</p>
  return (
    <table className="pbtbl">
      <thead>
        <tr>
          <th>{t('gui.pb.col_name')}</th>
          <th>{t('gui.pb.col_type')}</th>
          <th>{t('gui.pb.col_default')}</th>
          <th className="wide">{t('gui.pb.col_desc')}</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => {
          const p = params[k]
          if (!p) return null
          return (
            <tr key={k}>
              <td className="nm">
                {k}
                {p.required ? <span className="req">{t('gui.pb.required')}</span> : null}
              </td>
              <td className="ty">{p.type}</td>
              <td>
                <Default value={p.default} />
              </td>
              <td className="ds">
                {p.description}
                {p.enum && p.enum.length ? <span className="en">{p.enum.join(' \u00b7 ')}</span> : null}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/* Everything a caller has to know before the graph is worth reading: when this
   fires, and what it will ask them for. */
function Contract({ detail }: { detail: PlaybookDetail }): JSX.Element {
  return (
    <>
      <section className="pbsec">
        <h2>{t('gui.pb.sec_keywords')}</h2>
        <span className="chips">
          {detail.keywords.map((k) => (
            <span className="tag" key={k}>
              {k}
            </span>
          ))}
        </span>
      </section>
      <section className="pbsec">
        <h2>{t('gui.pb.sec_params')}</h2>
        <Params params={detail.params} />
      </section>
      <section className="pbsec">
        <h2>{t('gui.pb.sec_servers')}</h2>
        <CarriedServers servers={detail.mcp_servers || {}} />
      </section>
    </>
  )
}

/* The servers the playbook itself ships. A node's `mcps` entry is only a name,
   and the same name may be a server this machine already configures -- a
   different process, reached differently. Shown so a reader can tell which is
   which before running it.

   `env` and `headers` are printed as the file writes them, which is a reference
   (`{{ params.X }}`) and never a value: a carried server names the credential
   the run has to supply, and the value never enters the file. */
function CarriedServers({ servers }: { servers: NonNullable<PlaybookDetail['mcp_servers']> }): JSX.Element {
  const names = Object.keys(servers)
  if (!names.length) return <p className="pbnone">{t('gui.pb.no_servers')}</p>
  return (
    <table className="pbtbl">
      <thead>
        <tr>
          <th>{t('gui.pb.col_name')}</th>
          <th>{t('gui.pb.col_transport')}</th>
          <th className="wide">{t('gui.pb.col_launch')}</th>
          <th>{t('gui.pb.col_auth')}</th>
          <th>{t('gui.pb.col_refs')}</th>
        </tr>
      </thead>
      <tbody>
        {names.map((name) => {
          const s = servers[name]
          if (!s) return null
          const stdio = !!s.command
          const refs = [...Object.entries(s.env || {}), ...Object.entries(s.headers || {})]
          /* Whatever the wire says, and no fallback: the handler already reports
             the transport the runtime will pick, including for a file that left
             the field out. Guessing again here is how the page came to label an
             `/sse` url as streamable http while the runtime dialled it as sse --
             two readers deriving the same thing separately, and disagreeing. */
          const transport = s.type || ''
          const off = s.enabled === false
          return (
            <tr key={name} className={off ? 'dim' : undefined}>
              <td className="nm">
                {name}
                {/* A definition the run will not dial. Said on the row, because
                    the rest of it reads as launchable. */}
                {off ? <span className="tag warn">{t('gui.pb.server_off')}</span> : null}
              </td>
              <td className="ty">{transport || t('gui.pb.no_transport')}</td>
              <td className="ds">
                {stdio ? [s.command, ...(s.args || [])].join(' ') : s.url}
                {s.tool_timeout && s.tool_timeout !== 30 ? (
                  <span className="en">{t('gui.pb.tool_timeout', { n: s.tool_timeout })}</span>
                ) : null}
              </td>
              <td className="ty">
                {s.auth && s.auth !== 'none' ? s.auth : t('gui.pb.no_auth')}
                {s.has_oauth_config ? <span className="en">{t('gui.pb.own_oauth')}</span> : null}
              </td>
              <td className="ds">
                {refs.length ? (
                  refs.map(([k, v]) => (
                    <span className="en" key={k}>
                      {k + '=' + v}
                    </span>
                  ))
                ) : (
                  <span className="gap">{t('gui.pb.no_refs')}</span>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Detail({ detail }: { detail: PlaybookDetail }): JSX.Element {
  const s = store.getState()
  const node = detail.nodes.find((n) => n.id === s.pickedNode) || null
  const onGraph = s.tab === 'graph'
  return (
    <>
      <div className="pbcrumb">
        <button className="mini ghost" onClick={store.back}>
          {'\u2190 ' + t('gui.pb.back')}
        </button>
      </div>

      <div className="pbhead">
        <Tile name={detail.name} />
        <h1>{detail.name}</h1>
        {detail.disabled ? <span className="pboff">{t('gui.pb.disabled')}</span> : null}
      </div>
      <p className="pbsum">{detail.description}</p>

      <div className="pbmeta">
        <div>
          <span className="pblab">{t('gui.pb.f_mode')}</span>
          <span className="pbval mono">{detail.mode}</span>
        </div>
        <div>
          <span className="pblab">{t('gui.pb.f_version')}</span>
          <span className="pbval mono">{'v' + detail.version}</span>
        </div>
        <div>
          <span className="pblab">{t('gui.pb.f_confirm')}</span>
          {/* The field's own value, both states spelled: nothing is rendered for
              `false` if it is only ever a mark for `true`, and a blank cannot be
              told from a page that did not say. */}
          <span className="pbval mono">{String(detail.confirm)}</span>
        </div>
      </div>

      <div className="pbtabs" role="tablist">
        <button className="pbtab" role="tab" aria-selected={onGraph} onClick={() => store.showTab('graph')}>
          {/* A prompt-mode playbook has no graph to show: what sits here is the
              guidance a model assembles one from, so the tab says that instead
              of naming a picture that is not there. */}
          {t(detail.mode === 'dag' ? 'gui.pb.tab_graph' : 'gui.pb.tab_assembly')}
        </button>
        <button className="pbtab" role="tab" aria-selected={!onGraph} onClick={() => store.showTab('contract')}>
          {t('gui.pb.tab_contract')}
        </button>
      </div>

      {/* Hidden rather than unmounted: the board holds the reader's own pan and
          zoom, and a look at the contract must not throw it away. */}
      <div className={'pbwork' + (detail.mode === 'dag' ? '' : ' solo') + (onGraph ? '' : ' gone')}>
        {detail.mode === 'dag' ? (
          <>
            <Board detail={detail} picked={s.pickedNode} />
            <NodePanel node={node} carried={Object.keys(detail.mcp_servers || {})} />
          </>
        ) : (
          /* One column, because there is no second thing: a panel beside this
             saying the graph is composed per run was a note about the absence
             of a panel. */
          <div className="pbstage prose">
            <span className="cap">prompts</span>
            <Prompt text={detail.prompts} />
          </div>
        )}
      </div>
      {onGraph ? null : <Contract detail={detail} />}
    </>
  )
}

export function PlaybooksApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  /* Arrow keys walk the graph once a step is picked: a canvas a reader has to
     aim at with a mouse is a canvas they stop exploring. */
  useEffect(() => {
    if (!s.detail || !s.pickedNode) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      const target = e.target as HTMLElement | null
      if (target && /INPUT|TEXTAREA/.test(target.tagName)) return
      const nodes = (s.detail as PlaybookDetail).nodes
      const i = nodes.findIndex((n) => n.id === s.pickedNode)
      if (i < 0) return
      const next = nodes[e.key === 'ArrowRight' ? Math.min(i + 1, nodes.length - 1) : Math.max(i - 1, 0)]
      if (next) store.pick(next.id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [s.detail, s.pickedNode])

  if (s.openName && s.detail) return <Detail detail={s.detail} />
  if (s.openName) return <div className="empty-note">{t('gui.pb.reading')}</div>
  return <Library />
}
