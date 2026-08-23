import { useEffect, useRef } from 'react'
import { useSyncExternalStore } from 'react'

import { t } from '../../shell/bridge'
import { instanceMark, instanceState } from './history'
import * as store from './store'

import type { AgentsState } from './store'
import type { AgentRow, InstanceRow, OpenItem } from './types'
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

/* What a row is, beyond its status: whether it can be talked to, and whether it
   is a graph's work rather than a lone call. Tags rather than groups -- the two
   facts are orthogonal, and a heading per combination is four headings. */
function Tags({ it }: { it: InstanceRow }): JSX.Element | null {
  const marks: string[] = []
  if (it.resumable) marks.push(t('gui.ws.instance_resumable'))
  if (it.runId) marks.push(t('gui.ws.instance_of_graph'))
  if (!marks.length) return null
  return (
    <>
      {marks.map((m) => (
        <span className="gr" key={m}>{m}</span>
      ))}
    </>
  )
}

function InstanceRowView({ it }: { it: InstanceRow }): JSX.Element {
  /* Through the store, so this row and the conversation's graph card decide the
     same way: a row that is a node's status rather than a conversation opens the
     node's record. */
  const open = (): void => store.openInstanceRow(it)
  const state = instanceState(it.status ?? undefined)
  return (
    <div
      className="sarow inst"
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
      <Mark status={instanceMark(it.status ?? undefined)} />
      <div className="bd">
        {/* The node's own name where there is one: that is what the graph card in
            the conversation calls this work, and a handle it minted itself is
            not a name the reader chose. */}
        <div className="nm" title={it.handle}>{it.nodeId || it.handle}</div>
        <div className="st">
          {it.status && state !== 'live' ? <span>{t('gui.ws.instance_' + state)}</span> : null}
          <span className="who">{it.agent}</span>
          <Tags it={it} />
        </div>
      </div>
      <button
        className="mini ghost"
        title={t('gui.ws.instance_forget_note')}
        onClick={(e) => {
          /* The row is a button too, and forgetting is not opening. */
          e.stopPropagation()
          store.forgetInstance(it)
        }}
      >
        {t('gui.ws.instance_forget')}
      </button>
      <span className="chev">{'›'}</span>
    </div>
  )
}

function AgentList({ s }: { s: AgentsState }): JSX.Element {
  useEffect(() => {
    /* Both: the list draws instances, but an open run detail reached from the
       conversation's own graph card reads its label and status off `rows`. */
    store.refreshInstances()
    store.refresh()
  }, [])
  /* One flat list of instances, which is what this panel is for. It used to
     draw the runs and hang the instances underneath, and that showed a stateful
     graph node twice -- once as the node, once as the handle it ran on. The
     server now reports one row per invocation, so a graph's four nodes are four
     rows, and which of them can be talked to is a tag rather than a heading. */
  if (!s.instances.length) {
    return (
      <div className="wsempty">
        <div className="ttl">{t(store.absent() ? 'gui.ws.agents_absent' : 'gui.ws.agents_none')}</div>
      </div>
    )
  }
  return (
    <div className="salist">
      {s.instances.map((it) => (
        <InstanceRowView key={`${it.agent}:${it.handle}`} it={it} />
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

/* Carry on the conversation from here. Only for a row the server calls
   resumable: against a stateless agent every turn starts from nothing, so what
   looked like a conversation would be a run of unrelated first turns. */
function InstanceComposer(
  { open, name, fail }: {
    open: Extract<OpenItem, { kind: 'instance' }>
    name: string
    fail: string | null
  },
): JSX.Element | null {
  const box = useRef<HTMLTextAreaElement>(null)
  if (!store.canSend()) return null
  const send = (): void => {
    const el = box.current
    if (!el || !el.value.trim()) return
    const said = el.value
    /* Emptied once the turn is taken, never before: a send is refused whenever
       this instance is still answering the turn before, and clearing on submit
       threw away the reader's own words on the one outcome where they would
       want to try again. Only if the box still holds what was submitted --
       anything typed while the turn was in flight is not this send's to drop. */
    void store.sendToInstance(open.agent, open.handle, said).then((taken) => {
      if (taken && el.value === said) el.value = ''
    })
  }
  return (
    <div className="agf sasend">
      <textarea
        ref={box}
        rows={2}
        /* Named, not "this instance": the whole question a reader has here is
           who they are addressing, and the main composer is one panel away. */
        placeholder={t('gui.ws.instance_say_hint', { name })}
        onKeyDown={(e) => {
          /* Enter sends, shift-enter breaks the line -- the composer's own rule,
             so the two do not disagree about the same keystroke. */
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            send()
          }
        }}
      />
      <div className="foot">
        {fail ? <span className="why">{fail}</span> : null}
        <button className="mini" onClick={send}>{t('gui.ws.instance_say')}</button>
      </div>
    </div>
  )
}

function InstanceDetail(
  { s, open }: { s: AgentsState; open: Extract<OpenItem, { kind: 'instance' }> },
): JSX.Element {
  const row = s.instances.find((x) => x.agent === open.agent && x.handle === open.handle)
  /* The same header chrome the run details use. Its own markup had no
     stylesheet behind it at all, which showed as an unsized back chevron the
     height of the panel and a name run together with its agent. */
  return (
    <>
      <div className="sahd">
        <Back />
        <div className="trow">
          <b>{row?.nodeId || open.handle}</b>
          {/* The handle stays visible, in the slot this header already gives
              machine-readable detail: it is what a direct turn is addressed to,
              so it is worth being able to read even when the node names it.
              Only when it says something the title does not, though: a graph
              that names a node's instance after the node itself made the header
              read "synthesize synthesize". */}
          {row?.nodeId && row.nodeId !== open.handle
            ? <span className="sp">{open.handle}</span>
            : null}
          <span className="who">{open.agent}</span>
          {row?.runId ? <span className="gr">{t('gui.ws.instance_of_graph')}</span> : null}
        </div>
      </div>
      <Stage paint={(box) => store.paintInstance(box, open.agent, open.handle)} />
      {row?.resumable
        ? (
          <InstanceComposer
            open={open}
            name={row.nodeId || open.handle}
            fail={store.sendFailOf(open.agent, open.handle)}
          />
        )
        : null}
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
  if (s.open && s.open.kind === 'instance') {
    return <InstanceDetail key={`in${s.epoch}:${s.open.agent}:${s.open.handle}`} s={s} open={s.open} />
  }
  return <AgentList s={s} />
}
