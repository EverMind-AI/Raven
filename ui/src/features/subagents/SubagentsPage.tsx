import { useEffect, useRef } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { AgentsState } from './store'
import type { AgentRow, OpenItem } from './types'
import type { JSX } from 'react'

/* Mirrors the glyph the legacy renderer drew with (ICO.up in
   ui/src/demo/100-workspace.js, through its ico() helper). */
function IcoUp(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M14.5 6.5 9 12l5.5 5.5" />
    </svg>
  )
}

/* A run's state, as ONE element in the row's leading column. In flight that
   is the work glyph -- the same one the turn's own row wears; finished, it is
   the status dot. */
function Mark({ status }: { status?: string }): JSX.Element {
  if (status !== 'run') return <span className={'dot ' + store.agentDot(status)} />
  return (
    <span className="wkg sw" aria-hidden="true" title={t('gui.ws.agent_run')}>
      <i />
      <i />
      <i />
    </span>
  )
}

/* The span, plus the anchor a still-running one needs to keep counting: the
   store's one clock re-renders every second while any data-t0 is live. */
function Span({ it }: { it: AgentRow }): JSX.Element | null {
  const text = store.agentSpan(it)
  if (!text) return null
  const t0 = store.agentT0(it)
  return <span className="sp" {...(t0 ? { 'data-t0': String(t0) } : {})}>{text}</span>
}

function Row({ it }: { it: AgentRow }): JSX.Element {
  const open = (): void => store.openRow(it)
  const ended = store.agentEndAt(it)
  const cost = store.agentCost(it)
  return (
    <div
      className="sarow"
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <Mark status={it.status} />
      <div className="bd">
        <div className="nm">{store.plainTitle(it.label)}</div>
        <div className="st">
          {it.status !== 'run'
            ? <span>{t(it.status === 'skipped' ? 'gui.ws.agent_skip' : 'gui.ws.agent_' + store.agentDot(it.status))}</span>
            : null}
          <Span it={it} />
          {ended
            ? <span className="ed" title={t('gui.ws.agent_ended', { t: ended })}>{ended}</span>
            : null}
          {cost
            ? <span className="tk" title={t('gui.ws.agent_tokens', { n: String(it.tokens) })}>{cost}</span>
            : null}
          <span className="who">{store.agentWho(it)}</span>
          {it.kind === 'dag' ? <span className="gr">{t('gui.ws.agent_of_graph')}</span> : null}
        </div>
      </div>
      <span className="chev">{'›'}</span>
    </div>
  )
}

function AgentList({ s }: { s: AgentsState }): JSX.Element {
  useEffect(() => {
    store.refresh()
  }, [])
  /* A queued node has not run, has no transcript, and opening it shows an
     empty panel. The graph is where waiting work belongs; this list is the
     runs there is something to read about. The rows themselves keep them:
     the sheet resolves a node's status through the store. */
  const shown = s.rows.filter((a) => a.status !== 'queued')
  if (!shown.length) {
    return (
      <div className="wsempty">
        <div className="ttl">{t(store.absent() ? 'gui.ws.agents_absent' : 'gui.ws.agents_none')}</div>
      </div>
    )
  }
  return (
    <div className="salist">
      {shown.map((it, i) => (
        <Row key={it.kind === 'dag' ? `d:${it.run_id}:${it.node}` : it.id || `i${i}`} it={it} />
      ))}
    </div>
  )
}

function Back(): JSX.Element {
  return (
    <button className="back" onClick={() => store.back()}>
      <IcoUp />
      <span>{t('gui.cron.back')}</span>
    </button>
  )
}

/* Its own scroller: the run is a transcript of unknown length and must not
   push the header it belongs to off the top of the panel. The children are
   the legacy transcript bridge's, never React's. */
function Stage({ paint }: { paint: (box: HTMLElement) => void }): JSX.Element {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = box.current
    if (!el) return
    store.setStage(el)
    /* The mount is the paint: a different run remounts through the key. */
    paint(el)
    return () => store.setStage(null)
  }, [])
  return <div className="satx" ref={box} />
}

function DagDetail({ open }: { open: Extract<OpenItem, { kind: 'dag' }> }): JSX.Element {
  return (
    <>
      <div className="sahd">
        <Back />
        <div className="trow">
          <b>{open.node}</b>
          <span className="who">{open.agent || 'raven'}</span>
        </div>
      </div>
      <Stage paint={(box) => store.paintDag(box, open)} />
    </>
  )
}

function SpawnDetail({ s, open }: { s: AgentsState; open: Extract<OpenItem, { kind: 'spawn' }> }): JSX.Element {
  const it = s.rows.find((a) => a.id === open.id) || { label: open.id, status: 'run' }
  const cost = store.agentCost(it)
  return (
    <>
      <div className="sahd">
        <Back />
        <div className="trow">
          {/* No status mark here: on the open transcript the transcript itself
              already says what is happening. */}
          <b>{store.plainTitle(it.label)}</b>
          <Span it={it} />
          {cost ? <span className="tk">{cost}</span> : null}
          <span className="who">{s.who || store.agentWho(it)}</span>
        </div>
      </div>
      <Stage paint={(box) => store.paintSpawn(box, open.id)} />
    </>
  )
}

export function SubagentsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  if (s.open && s.open.kind === 'dag') {
    return <DagDetail key={`d${s.epoch}:${s.open.run_id}:${s.open.node}`} open={s.open} />
  }
  if (s.open && s.open.kind === 'spawn') {
    return <SpawnDetail key={`s${s.epoch}:${s.open.id}`} s={s} open={s.open} />
  }
  return <AgentList s={s} />
}
