import { ds, shell, t } from '../../shell/bridge'

import type { AgentRow, AgentsSource, OpenItem } from './types'
import type { Shell } from '../../shell/bridge'

/* Page state, outside React on purpose: the legacy shell drives this view
 * imperatively (drawWs mounts and unmounts it per redraw, the dag sheet opens
 * nodes into it, wsReset clears it with the session), so the state lives in a
 * plain store the shims can call and the component subscribes.
 *
 * This is the legacy AGENTS/agentOpen/dagNode trio (ui/src/demo/110-subagents.js
 * before the migration) plus the refresh/poll judgements that lived beside it;
 * the fingerprint, the floor and the clock keep their behaviour so the two can
 * be diffed.
 */

export interface AgentsState {
  rows: AgentRow[]
  open: OpenItem | null
  /* The listed row may mislabel a run the list has aged out; the context
     answer carries the truth and corrects the open header through this. */
  who: string | null
  /* Bumped when the open run's status flips under the reader: remounts the
     detail so the header mark and the transcript land on the final state
     together -- the island's equivalent of the legacy full drawWs. */
  epoch: number
  tick: number
}

const initial: AgentsState = { rows: [], open: null, who: null, epoch: 0, tick: 0 }

let state: AgentsState = { ...initial }
const listeners = new Set<() => void>()

export const getState = (): AgentsState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(p: Partial<AgentsState>): void {
  state = { ...state, ...p }
  for (const l of listeners) l()
  clockSync()
}

export const source = (): AgentsSource => ds<AgentsSource>('agents')

function verb<K extends keyof Shell>(name: K): NonNullable<Shell[K]> {
  const v = shell()[name]
  if (!v) throw new Error(`RavenShell.${String(name)} is not wired`)
  return v as NonNullable<Shell[K]>
}

export const absent = (): boolean => {
  const a = source().absent
  return a ? a() : false
}

/* Which agent ran a call. Null is raven's own sub-agent: the difference
   changes what the transcript means, so no row leaves it unsaid. */
export const agentWho = (it: AgentRow): string => (it && it.agent) || 'raven'

export function agentDot(status?: string): string {
  return status === 'run' ? 'run'
    : status === 'error' ? 'bad'
      : status === 'queued' || status === 'skipped' ? 'que'
        : 'ok'
}

export function agentSpan(it: AgentRow): string {
  const t0 = it.started_at ? new Date(it.started_at).getTime() : 0
  if (!t0) return ''
  const t1 = it.ended_at ? new Date(it.ended_at).getTime() : Date.now()
  return verb('dur')(Math.max(t1 - t0, 1000))
}

/* The anchor a still-running span carries so the clock keeps counting. */
export function agentT0(it: AgentRow): number {
  if (it.ended_at || !it.started_at) return 0
  const t0 = new Date(it.started_at).getTime()
  return t0 || 0
}

/* When the run ended, as a wall-clock stamp: the duration says how long it
   took, this says how long ago -- the fact a list of past runs sorts by. */
export function agentEndAt(it: AgentRow): string {
  if (!it.ended_at) return ''
  const d = new Date(it.ended_at)
  if (isNaN(d.getTime())) return ''
  const p = (n: number): string => String(n).padStart(2, '0')
  const sameDay = d.toDateString() === new Date().toDateString()
  return (sameDay ? '' : `${d.getMonth() + 1}/${d.getDate()} `) + `${p(d.getHours())}:${p(d.getMinutes())}`
}

/* What a run cost, for the row. Absent rather than zero when the transport
   that ran it cannot report usage -- "0" there would be a claim about the
   agent instead of an admission about the record. */
export function agentCost(it: AgentRow): string {
  if (it.tokens == null) return ''
  return it.tokens >= 1000 ? `${(it.tokens / 1000).toFixed(1)}k` : String(it.tokens)
}

export const plainTitle = (s: unknown): string => verb('plainTitle')(String(s ?? ''))

/* ── the list ─────────────────────────────────────────────────────────
   Refreshing is three separate judgements, all about what is on screen:
   whether to ask at all, whether the answer still belongs to the conversation
   the reader is in, and whether anything changed enough to repaint. */
let busy = false
let at = 0
/* One fingerprint per drawn list, keyed by conversation as well as content,
   so switching between two sessions that listed the same thing still
   repaints -- and an unchanged answer costs no render at all. */
let drawn = ''

const sessionKey = (): string => {
  const k = shell().sessionKey
  return k ? k() : ''
}

export function refresh(force = false): void {
  /* The list asks on each draw and a fresh answer causes one; the floor keeps
     that from spinning, and doubles as the watch's rate limit. */
  const asked = sessionKey()
  if (!asked || busy || (!force && Date.now() - at < 2500)) return
  busy = true
  source().list(asked)
    .then((rows) => {
      /* An answer for a conversation the reader already left: dropping it is
         the difference between a stale list and somebody else's list. */
      if (asked !== sessionKey()) return
      const next = rows || []
      const dot = document.getElementById('wsAgentRun')
      if (dot) dot.hidden = !next.some((a) => a.status === 'run')
      const print = `${asked}|${JSON.stringify(next)}`
      if (print === drawn) {
        state = { ...state, rows: next }
        return
      }
      drawn = print
      set({ rows: next })
    })
    .catch(() => { /* an empty list is not a broken one: keep what is drawn */ })
    .then(() => {
      busy = false
      at = Date.now()
    })
}

export const rows = (): AgentRow[] => state.rows

/* ── opening and closing ────────────────────────────────────────────── */

export function openRow(it: AgentRow): void {
  stageFresh = true
  paintedStatus = null
  if (it.kind === 'dag') {
    set({
      open: { kind: 'dag', run_id: it.run_id || '', node: it.node || '', agent: it.agent, label: it.node || '' },
      who: null,
    })
  } else {
    set({ open: { kind: 'spawn', id: it.id || '' }, who: null })
  }
}

/* The dag sheet opens its nodes here (the sheet stays the map, this panel is
   the territory); a node reached from the trail's card carries no subagent. */
export function openDagNode(runId: string, n: { id: string; subagent?: string | null }): void {
  stageFresh = true
  paintedStatus = null
  set({ open: { kind: 'dag', run_id: runId, node: n.id, agent: n.subagent, label: n.id }, who: null })
}

/* Back to the list, not to the graph: the graph never went anywhere. */
export function back(): void {
  stageFresh = true
  paintedStatus = null
  set({ open: null, who: null })
}

/* What the dag sheet reads to mark the node whose transcript is open. */
export const sel = (): { run_id: string; node: string } | null =>
  state.open && state.open.kind === 'dag' ? { run_id: state.open.run_id, node: state.open.node } : null

/* ── the detail stage ─────────────────────────────────────────────────
   The stage's DOM belongs to the legacy transcript bridge (the shell's
   agentStagePaint verb draws with the transcript's own renderer); the island
   owns only when to paint and what record to hand over. */
let stageEl: HTMLElement | null = null
export function setStage(el: HTMLElement | null): void {
  stageEl = el
  /* A box arriving is the thing that makes the next paint a fresh one, and it
     is not the same event as the island deciding to start over. `drawWs()`
     wipes the panel body and mounts a new root on every draw, so a reopened
     panel hands over an empty box while the open record has not changed at
     all -- and a paint that believes it is a continuation appends the slice
     it already drew, which is nothing, into a box with nothing in it. */
  if (el) stageFresh = true
}

/* Whether the next paint starts the stage over: true while the box in hand
   has had nothing painted into it. Set when a box arrives, and again when the
   island replaces what is in one (an error note, a different record), so a
   failed first fetch never leaves a poll appending after it. */
let stageFresh = true
/* The status of the last painted record; the poll compares it against the
   list to catch a run finishing under an open page. */
let paintedStatus: string | null = null

function emptyStage(box: HTMLElement, text: string): void {
  box.textContent = ''
  const e = document.createElement('div')
  e.className = 'wsempty'
  e.textContent = text
  box.appendChild(e)
}

function failStage(box: HTMLElement, e: unknown): void {
  stageFresh = true
  emptyStage(box, (e as Error)?.message || String(e))
}

export function paintSpawn(box: HTMLElement, id: string): void {
  const src = source()
  if (!src.context) {
    emptyStage(box, t('gui.ws.agents_none'))
    return
  }
  const openAt = state.open
  src.context(id)
    .then((r) => {
      if (state.open !== openAt || !box.isConnected) return
      /* Opened without a listed row -- a reopened panel, a run that aged out
         -- the header fell back to raven's own sub-agent. The answer carries
         the truth, so correct it on arrival. */
      const who = (r && r.agent) || 'raven'
      if (state.who !== who) set({ who })
      paintedStatus = (r && r.status) || null
      verb('agentStagePaint')(box, r, { key: `sp:${id}`, reset: stageFresh })
      stageFresh = false
    })
    .catch((e: unknown) => {
      if (state.open !== openAt || !box.isConnected) return
      failStage(box, e)
    })
}

export function paintDag(box: HTMLElement, it: { run_id: string; node: string }): void {
  const src = source()
  if (!src.node) {
    emptyStage(box, t('gui.ws.agents_none'))
    return
  }
  const openAt = state.open
  src.node(it.run_id, it.node)
    .then((n) => {
      if (state.open !== openAt || !box.isConnected) return
      const row = state.rows.find((a) => a.kind === 'dag' && a.run_id === it.run_id && a.node === it.node)
      paintedStatus = row ? row.status || null : null
      verb('agentStagePaint')(
        box,
        { messages: (n && n.messages) || [], status: row ? row.status : null },
        { key: `dag:${it.run_id}:${it.node}`, empty: t('gui.dag.node_empty'), reset: stageFresh },
      )
      stageFresh = false
      /* Said plainly rather than left to a reader wondering where the rest
         went: the file on disk is whole, this is its head. Once per stage. */
      if (n && n.output_truncated && !box.querySelector(':scope > .wsnote')) {
        const note = document.createElement('div')
        note.className = 'wsnote'
        note.textContent = t('gui.dag.truncated')
        box.appendChild(note)
      }
    })
    .catch((e: unknown) => {
      if (state.open !== openAt || !box.isConnected) return
      failStage(box, e)
    })
}

/* ── the watch ────────────────────────────────────────────────────────
   The live source calls back every couple of seconds; everything about what
   deserves asking is decided here, against what is on screen. A run in
   flight has to move without being reopened, and its header has to change
   the moment the run does -- a detail page still saying "working" over a run
   the list already knows failed is the panel lying. */
let hooked: AgentsSource | null = null

export function hook(): void {
  const seam = window.DS
  const src = seam && (seam['agents'] as AgentsSource | undefined)
  if (!src || !src.watch || src === hooked) return
  hooked = src
  src.watch(onPoll)
}

function onPoll(): void {
  const shows = shell().wsShows
  if (!shows || !shows('agents')) return
  const open = state.open
  if (!open) {
    refresh()
    return
  }
  refresh(true)
  const it = open.kind === 'dag'
    ? state.rows.find((a) => a.kind === 'dag' && a.run_id === open.run_id && a.node === open.node)
    : state.rows.find((a) => a.id === open.id)
  const stNow = it ? it.status || null : null
  if (paintedStatus && stNow && paintedStatus !== stNow) {
    paintedStatus = stNow
    stageFresh = true
    set({ epoch: state.epoch + 1 })
    return
  }
  if (!it || it.status !== 'run') return
  const box = stageEl
  if (!box || !box.isConnected) return
  if (open.kind === 'dag') paintDag(box, open)
  else paintSpawn(box, open.id)
}

/* ── the clock ────────────────────────────────────────────────────────
   Drives every live span on screen, list row and open detail header alike,
   through one interval and a re-render; stops itself once none is left, so a
   finished run costs nothing. */
let ticker: ReturnType<typeof setInterval> | null = null
let mounted = false

function anyLive(): boolean {
  const open = state.open
  if (!open) return state.rows.some((a) => a.status !== 'queued' && agentT0(a) > 0)
  if (open.kind !== 'spawn') return false
  const it = state.rows.find((a) => a.id === open.id)
  return !!it && agentT0(it) > 0
}

function stopClock(): void {
  if (ticker) {
    clearInterval(ticker)
    ticker = null
  }
}

export function clockSync(): void {
  if (!mounted || !anyLive()) {
    stopClock()
    return
  }
  if (ticker) return
  ticker = setInterval(() => {
    if (!anyLive()) {
      stopClock()
      return
    }
    set({ tick: state.tick + 1 })
  }, 1000)
}

export function attached(on: boolean): void {
  mounted = on
  clockSync()
}

/* ── session lifecycle ────────────────────────────────────────────────
   Subagents belong to the session that spawned them, so they leave with it
   -- carrying the list into the next conversation would attribute one
   conversation's background work to another. The open dag node goes for the
   same reason, and because dag.node is addressed by session. */
export function reset(): void {
  drawn = ''
  at = 0
  busy = false
  stageFresh = true
  paintedStatus = null
  const dot = document.getElementById('wsAgentRun')
  if (dot) dot.hidden = true
  set({ ...initial })
}

/* Test seam only: module-level timers and flags survive between tests. */
export function _resetForTests(): void {
  stopClock()
  mounted = false
  hooked = null
  stageEl = null
  reset()
}
