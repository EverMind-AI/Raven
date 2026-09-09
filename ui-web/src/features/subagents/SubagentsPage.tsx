import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'

import { AgentMark } from '../../shell/agent-mark'
import { ds, t } from '../../shell/bridge'
import { SendGlyph } from '../../shell/ico'
import { composing, fmtSize } from '../composer/store'
import { instanceMark, instanceState } from './history'
import * as store from './store'

import type { AgentsState } from './store'
import type { AgentRow, InstanceRow, OpenItem, SubagentRow } from './types'
import type { Attachment, ComposerSource } from '../composer/types'
import type { JSX } from 'react'

/* Mirrors the glyph the legacy renderer drew with (ICO.up in
   ui-web/src/demo/100-workspace.js, through its ico() helper). */
function IcoUp(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M14.5 6.5 9 12l5.5 5.5" />
    </svg>
  )
}

function IcoPlus(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M12 6v12M6 12h12" />
    </svg>
  )
}

interface OrderedAgentGroup {
  name: string
  registered?: SubagentRow
  children: InstanceRow[]
  latest: number
}

const instanceTime = (row: InstanceRow): number => row.updatedAtMs ?? row.createdAtMs ?? 0

export function orderAgentGroups(roster: SubagentRow[], instances: InstanceRow[]): OrderedAgentGroup[] {
  const registrationOrder = new Map(roster.map((row, index) => [row.name, index]))
  const registered = new Map(roster.map((row) => [row.name, row]))
  const names = Array.from(new Set([...roster.map((row) => row.name), ...instances.map((row) => row.agent)]))
  return names.map((name) => {
    const children = instances.filter((row) => row.agent === name)
      .sort((left, right) => instanceTime(right) - instanceTime(left))
    return {
      name,
      registered: registered.get(name),
      children,
      latest: children.length ? instanceTime(children[0]!) : 0,
    }
  }).sort((left, right) => {
    if (left.latest !== right.latest) return right.latest - left.latest
    const leftOrder = registrationOrder.get(left.name) ?? Number.MAX_SAFE_INTEGER
    const rightOrder = registrationOrder.get(right.name) ?? Number.MAX_SAFE_INTEGER
    return leftOrder - rightOrder || left.name.localeCompare(right.name)
  })
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

export function InstanceRowView({ it, onOpen = store.openInstanceRow, compact = false }: {
  it: InstanceRow
  onOpen?: (row: InstanceRow) => void
  compact?: boolean
}): JSX.Element {
  /* Through the store, so this row and the conversation's graph card decide the
     same way: a row that is a node's status rather than a conversation opens the
     node's record. */
  const open = (): void => onOpen(it)
  if (compact) return (
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
      <span className="desk-status"><Mark status={instanceMark(it.status ?? undefined)} /></span>
      {/* What it was asked, then what asked it. The handle is the fallback and
          not a second line: it is a slug the runtime minted, and a row wearing
          one is a row whose dispatch predates the summaries. */}
      <span className="nm" title={it.title || it.handle}>{it.title || it.nodeId || it.handle}</span>
      {/* Only for a row that came out of a graph. A spawn has no source to name,
          and the run id it used to show here is a timestamp -- which is why the
          absence is drawn as nothing rather than as an id. */}
      {it.runTitle ? <span className="source" title={it.runTitle}>{it.runTitle}</span> : null}
    </div>
  )
  const state = instanceState(it.status ?? undefined)
  return (
    <div className="sarow inst" role="button" tabIndex={0} onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() }
      }}>
      <Mark status={instanceMark(it.status ?? undefined)} />
      <div className="bd">
        <div className="nm" title={it.title || it.handle}>{it.title || it.nodeId || it.handle}</div>
        <div className="st">
          {it.status && state !== 'live' ? <span>{t('gui.ws.instance_' + state)}</span> : null}
          <span className="who">{it.agent}</span>
          {it.runTitle ? <span className="from">{it.runTitle}</span> : null}
          <Tags it={it} />
        </div>
      </div>
      <button className="mini ghost" title={t('gui.ws.instance_forget_note')}
        onClick={(e) => { e.stopPropagation(); store.forgetInstance(it) }}>
        {t('gui.ws.instance_forget')}
      </button>
      <span className="chev">›</span>
    </div>
  )
}

export function AgentList({ s, onOpen, compact = false }: {
  s: AgentsState
  onOpen?: (row: InstanceRow) => void
  compact?: boolean
}): JSX.Element {
  const [closed, setClosed] = useState<Set<string>>(() => new Set())
  useEffect(() => {
    /* Both: the list draws instances, but an open run detail reached from the
       conversation's own graph card reads its label and status off `rows`. */
    store.refreshInstances()
    store.refresh()
    store.refreshRoster()
  }, [])
  /* One flat list of instances, which is what this panel is for. It used to
     draw the runs and hang the instances underneath, and that showed a stateful
     graph node twice -- once as the node, once as the handle it ran on. The
     server now reports one row per invocation, so a graph's four nodes are four
     rows, and which of them can be talked to is a tag rather than a heading. */
  if (!s.instances.length && (!compact || !s.roster.length)) {
    return (
      <div className="wsempty">
        <div className="ttl">{t(store.absent() ? 'gui.ws.agents_absent' : 'gui.ws.agents_none')}</div>
      </div>
    )
  }
  if (!compact) {
    return (
      <div className="salist">
        {s.instances.map((it) => <InstanceRowView key={`${it.agent}:${it.handle}`} it={it} onOpen={onOpen} />)}
      </div>
    )
  }
  const groups = orderAgentGroups(s.roster, s.instances)
  return (
    <div className="salist agent-roster">
      {groups.map(({ name, registered, children }) => {
        const folded = closed.has(name)
        const foldable = children.length > 0
        return (
          <section className="agent-group" key={name}>
            <div className="agent-headrow">
              <button
                className="agent-head"
                aria-expanded={foldable ? !folded : undefined}
                disabled={!foldable}
                onClick={() => setClosed((value) => {
                  if (!foldable) return value
                  const next = new Set(value)
                  if (next.has(name)) next.delete(name)
                  else next.add(name)
                  return next
                })}
              >
                {/* The slot is drawn on every head, foldable or not. `hidden` took it
                    out of the flow, so a group with children started its icon and its
                    name 12px right of every leaf row's -- one list on two vertical
                    lines. What a leaf row has no business showing is the glyph, not
                    the column. */}
                <span className="agent-fold" data-open={foldable && !folded} data-empty={!foldable} aria-hidden="true">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="m5.5 6.5 2.5 3 2.5-3" />
                  </svg>
                </span>
                <AgentMark preset={registered?.preset} />
                <b title={name}>{name}</b>
                <span className="agent-kind">{registered?.kind || children[0]?.kind || 'agent'}</span>
              </button>
              {/* Outside the head, not inside it: the head is a button, and a
                  button cannot hold another one. */}
              {store.addressable(registered) && (
                <button
                  className="agent-new"
                  title={t('gui.ws.instance_new_hint', { name })}
                  aria-label={t('gui.ws.instance_new_hint', { name })}
                  disabled={!!s.starting}
                  onClick={() => { void store.startInstance(name, onOpen) }}
                >
                  {s.starting === name ? <span className="wkg" aria-hidden="true"><i /><i /><i /></span> : <IcoPlus />}
                </button>
              )}
            </div>
            {s.startFail?.agent === name && (
              <p className="agent-newfail">{t('gui.ws.instance_new_fail', { why: s.startFail.why })}</p>
            )}
            <div className="agent-instances" hidden={folded}>
              {children.map((it) => (
                <InstanceRowView key={`${it.agent}:${it.handle}`} it={it} onOpen={onOpen} compact />
              ))}
            </div>
          </section>
        )
      })}
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
/* Where a direct chat's files go up: the same seam the page composer uploads
   through, and nothing when it is not installed (the demo canvas). Read per
   render rather than captured, like the composer store's own `canAttach`. */
function uploader(): ComposerSource['upload'] | undefined {
  try {
    return ds<ComposerSource>('composer').upload
  } catch {
    return undefined
  }
}

function AttachGlyph(): JSX.Element {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M15 7l-6.2 6.2a2.6 2.6 0 0 0 3.7 3.7L19 10a4.4 4.4 0 0 0-6.2-6.2L6 10.5a6.2 6.2 0 0 0 8.8 8.8l3.4-3.4" />
    </svg>
  )
}

/* The chips are the page composer's -- same class names, so the stylesheet
   dresses both -- minus the lightbox: a pane one size down has no room for it. */
function InstanceAtt({ a, onRemove }: { a: Attachment; onRemove: () => void }): JSX.Element {
  const size = a.uploading ? t('gui.att.uploading') : fmtSize(a.size)
  return (
    <div className={'att' + (a.uploading ? ' up' : '')}>
      <span className="nm">{a.name}</span>
      <span className="sz">{size}</span>
      <button className="rm" aria-label={t('gui.att.remove', { name: a.name })}
        onClick={(e) => { e.stopPropagation(); onRemove() }}>✕</button>
    </div>
  )
}

export function InstanceComposer(
  { open, name, fail }: {
    open: Extract<OpenItem, { kind: 'instance' }>
    name: string
    fail: string | null
  },
): JSX.Element | null {
  const box = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  const [text, setText] = useState('')
  /* This instance's own tray, not the page composer's: a file dropped here is
     for the sub-agent the reader is addressing, and the main composer is one
     panel away with a tray of its own. Kept in a ref as well as in state so an
     upload landing after a re-render still finds the entry it started. */
  const [atts, setAtts] = useState<Attachment[]>([])
  const held = useRef<Attachment[]>([])
  const [hold, setHold] = useState<string | null>(null)
  const chat = store.directChat(open.agent, open.handle)
  if (!store.canSend()) return null
  const paintAtts = (next: Attachment[]): void => {
    held.current = next
    setAtts(next)
  }
  const up = uploader()
  /* The page composer's `addFiles`, for one instance: bytes go up through the
     same seam into <workspace>/uploads, and the message carries paths. */
  const addFiles = (files: ArrayLike<File>): void => {
    if (!up) return
    Array.from(files).forEach((file) => {
      const entry: Attachment = { name: file.name, size: file.size, uploading: true, path: null, url: null }
      paintAtts(held.current.concat([entry]))
      const drop = (): void => paintAtts(held.current.filter((a) => a !== entry))
      const reader = new FileReader()
      reader.onload = () => {
        const b64 = String(reader.result).split(',')[1] || ''
        up({ name: file.name, content_b64: b64 })
          .then((r) => {
            entry.path = r.path
            entry.size = r.size
            entry.uploading = false
            paintAtts(held.current.slice())
          })
          .catch((e: unknown) => {
            drop()
            setHold(t('gui.att.fail', { name: file.name }) + ': ' + ((e as Error)?.message || String(e)))
          })
      }
      reader.onerror = drop
      reader.readAsDataURL(file)
    })
  }
  const send = (): void => {
    const el = box.current
    const staged = held.current
    if (!el || (!el.value.trim() && !staged.length)) return
    if (staged.some((a) => a.uploading)) {
      /* Before anything is cleared: a send refused for a file still on its way
         up must not take the typed line or the other files with it. */
      setHold(t('gui.att.pending'))
      return
    }
    setHold(null)
    const typed = el.value
    /* The note the page composer writes, word for word: the live layer reads
       the paths back out of it into the typed `media` field, and the reader's
       own bubble renders its chips from it. */
    const paths = staged.map((a) => String(a.path || '')).filter(Boolean)
    const said = paths.length
      ? `${typed.trim()}\n\n${t('gui.att.note')}\n${paths.map((p) => `- ${p}`).join('\n')}`
      : typed
    /* Emptied once the turn is taken, never before: a send is refused whenever
       this instance is still answering the turn before, and clearing on submit
       threw away the reader's own words on the one outcome where they would
       want to try again. Only if the box still holds what was submitted --
       anything typed while the turn was in flight is not this send's to drop. */
    void store.sendToInstance(open.agent, open.handle, said).then((taken) => {
      if (taken && el.value === typed) {
        el.value = ''
        setText('')
        if (held.current === staged) paintAtts([])
      }
    })
  }
  return (
    <div className="dock instance-dock sasend">
      <div className="dock-in">
        {chat.queue.length ? (
          <div className="instance-queue" aria-label={t('gui.queue.title', undefined, 'Queued messages')}>
            {chat.queue.map((item, index) => (
              <div className="instance-qrow" key={`${index}:${item}`}>
                <span>{item}</span>
                <button onClick={() => store.removeQueued(open.agent, open.handle, index)} aria-label={t('gui.queue.remove', undefined, 'Remove')}>×</button>
              </div>
            ))}
          </div>
        ) : null}
        {atts.length ? (
          <div className="atts instance-atts">
            {atts.map((a, index) => (
              <InstanceAtt key={`${index}:${a.name}`} a={a}
                onRemove={() => paintAtts(held.current.filter((_, i) => i !== index))} />
            ))}
          </div>
        ) : null}
        <div className="field">
      <textarea
        ref={box}
        rows={2}
        /* Named, not "this instance": the whole question a reader has here is
           who they are addressing, and the main composer is one panel away. */
        placeholder={t('gui.ws.instance_say_hint', { name })}
        onInput={(e) => setText(e.currentTarget.value)}
        onPaste={(e) => {
          const files = up && e.clipboardData ? [...e.clipboardData.files] : []
          if (!files.length) return
          e.preventDefault()
          addFiles(files)
        }}
        onKeyDown={(e) => {
          /* The keystroke that confirms an IME candidate is an Enter too, and it
             arrives while the composition is still open. Unguarded it sent the
             half-typed line, and because the conversion then changed the text the
             box was not emptied -- so the reader pressed Enter again, and that
             second send, arriving while the instance was answering the first, was
             queued. One line typed, one sent and one waiting behind it. Every
             other field that submits on Enter asks this first. */
          if (composing(e.nativeEvent)) return
          /* Enter sends, shift-enter breaks the line -- the composer's own rule,
             so the two do not disagree about the same keystroke. */
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            send()
          }
        }}
      />
        </div>
        <div className="under">
          {up ? (
            <>
              <button className="tool-btn instance-attach" data-tip={t('gui.attach')} aria-label={t('gui.attach')}
                onClick={() => picker.current?.click()}>
                <AttachGlyph />
              </button>
              <input ref={picker} type="file" multiple hidden
                onChange={(e) => {
                  if (e.currentTarget.files) addFiles(e.currentTarget.files)
                  e.currentTarget.value = ''
                }} />
            </>
          ) : null}
          {fail || hold ? <span className="why">{fail || hold}</span> : <span />}
          <button className="go" disabled={!text.trim() && !atts.length} onClick={send} aria-label={t('gui.ws.instance_say')}>
            <SendGlyph />
          </button>
          <button className="mini legacy-instance-send" tabIndex={-1} aria-hidden="true" onClick={send} />
        </div>
      </div>
    </div>
  )
}

export function InstanceConversation({ row }: { row: InstanceRow }): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getState)
  const [poll, setPoll] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const current = state.instances.find((it) => it.agent === row.agent && it.handle === row.handle) || row
  const chat = store.directChat(row.agent, row.handle)
  useEffect(() => store.subscribeDetailPoll(() => {
    if (instanceMark(current.status ?? undefined) === 'run') setPoll((value) => value + 1)
  }), [current.status])
  useEffect(() => {
    if (box.current) store.paintInstanceDirect(box.current, current.agent, current.handle)
  }, [current.agent, current.handle, current.status, current.updatedAtMs, chat.busy, chat.pending.length, poll])
  /* The pane reserves the composer's room -- a bottom inset and the glass it
     scrolls under -- so it has to say whether there IS one: a record with
     nothing to type into was leaving a blank 150px band under its last line. */
  return (
    <div className="instance-conversation" data-composer={String(!!current.resumable)}>
      <div className="satx" ref={box} />
      {current.resumable
        ? (
          <InstanceComposer
            open={{ kind: 'instance', agent: current.agent, handle: current.handle }}
            /* What the instance is for, ahead of the slug and the handle -- the
               order the panel's own header already uses. Asking for the id first
               addressed the reader to `raven-f5caf2` inside a pane headed by the
               task, which reads as two different things on one screen. */
            name={current.title || current.nodeId || current.handle}
            fail={store.sendFailOf(current.agent, current.handle)}
          />
        )
        : null}
    </div>
  )
}

export function AgentRecordConversation({ row }: { row: AgentRow }): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getState)
  const [poll, setPoll] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const current = state.rows.find((item) => row.kind === 'dag'
    ? item.kind === 'dag' && item.run_id === row.run_id && item.node === row.node
    : item.kind !== 'dag' && item.id === row.id) || row
  useEffect(() => store.subscribeDetailPoll(() => {
    if (current.status === 'run') setPoll((value) => value + 1)
  }), [current.status])
  useEffect(() => {
    if (box.current) store.paintAgentRecord(box.current, current)
  }, [current.id, current.kind, current.node, current.run_id, current.status, poll])
  return (
    <div className="instance-conversation" data-composer="false">
      <div className="satx" ref={box} />
    </div>
  )
}

function InstanceDetail(
  { s, open }: { s: AgentsState; open: Extract<OpenItem, { kind: 'instance' }> },
): JSX.Element {
  const row = s.instances.find((x) => x.agent === open.agent && x.handle === open.handle)
  const shownName = row?.title || row?.nodeId || open.handle
  /* The same header chrome the run details use. Its own markup had no
     stylesheet behind it at all, which showed as an unsized back chevron the
     height of the panel and a name run together with its agent. */
  return (
    <>
      <div className="sahd">
        <Back />
        <div className="trow">
          <b>{shownName}</b>
          {/* The handle stays visible, in the slot this header already gives
              machine-readable detail: it is what a direct turn is addressed to,
              and this header is the only place it appears -- the composer and
              the pane header both take the title now. Suppressed only when the
              name above already IS the handle, which a graph naming a node's
              instance after the node itself produces: that read "synthesize
              synthesize". Compared against what is DRAWN, not against `nodeId`:
              a row whose node id equals its handle still has a title unlike
              both, and testing `nodeId` there hid the address entirely. */}
          {shownName && shownName !== open.handle
            ? <span className="sp">{open.handle}</span>
            : null}
          <span className="who">{open.agent}</span>
          {row?.runTitle ? <span className="from">{row.runTitle}</span> : null}
          {row?.runId ? <span className="gr">{t('gui.ws.instance_of_graph')}</span> : null}
        </div>
      </div>
      <Stage paint={(box) => store.paintInstance(box, open.agent, open.handle)} />
      {row?.resumable
        ? (
          <InstanceComposer
            open={open}
            name={row.title || row.nodeId || open.handle}
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
