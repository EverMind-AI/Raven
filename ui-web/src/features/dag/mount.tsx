/* The island's face: one host and one React root per conversation, and the four
 * calls the live layer makes into it.
 *
 * The rack decides when a host is in the document -- it files sheets under the
 * conversation that raised them and mounts only the open one -- so what is kept
 * here is the root's lifetime, not the sheet's visibility. The takedown handed
 * to `sheets.add` is what stops the clock: the interval lives in the component,
 * and a sheet whose conversation was deleted would otherwise tick for the life
 * of the page.
 */

import { createRoot } from 'react-dom/client'

import { t } from '../../shell/bridge'
import { add as sheetAdd, askingIn, remove as sheetRemove, watchAsking } from '../composer/sheets'
import { Sheet } from './DagSheet'
import { fromSnapshot } from './nodes'
import * as store from './store'

import type { DagRun, DagSummary } from './types'
import type { Root } from 'react-dom/client'

const HOSTS = new Map<string, { el: HTMLElement; root: Root }>()

/* The graph steps aside while the reader is being asked something.
 *
 * The two dock in the same rack and a graph is tall, so an approval landing
 * under a running one goes below the fold -- the reader is being asked for a
 * decision they cannot see. The graph folds itself for as long as the question
 * stands and opens again when it is answered.
 *
 * Only if the fold was ours to take, and only if it still is. The store keeps
 * that record, because it owns the flag and sees every reader path to it -- see
 * `takeFold`. A graph the reader had already folded stays folded afterwards,
 * and one whose fold they touched at all while the question stood is left where
 * they left it, however many times they changed their mind.
 */
const onAsking = (key: string, asking: number): void => {
  if (asking > 0) store.takeFold(key)
  else store.releaseFold(key)
}

/* Registered at module scope because that is this island's whole lifecycle --
   one island, page lifetime, no mount to hang it off. The unsubscribe is kept
   anyway so the test seam can drop and re-arm it: `sheets.ts` deliberately
   keeps watchers across its own reset, so without this the isolation between
   test files would rest on vitest handing each one a fresh module registry --
   true today, and not something this file should depend on. */
let unwatch = watchAsking(onAsking)

function drop(key: string): void {
  const h = HOSTS.get(key)
  store.forget(key)
  if (h) sheetRemove(h.el)
}

function host(key: string): void {
  if (HOSTS.has(key)) return
  /* The sheet's own element, not a wrapper around it: the rack writes
     `data-sess` on what it is handed and `.dock .sheets > *` styles it as the
     flex item, so an extra div would take both. React owns the children of a
     container it is given; these three attributes never change, so they are set
     once here, and `data-fold`, which does change, is written by the component. */
  const el = document.createElement('div')
  el.className = 'dsheet'
  el.setAttribute('role', 'group')
  el.setAttribute('aria-label', t('gui.dag.aria'))
  /* Written here as well as by the component, and in this order on purpose: the
     rack adds `data-sess` the moment it is handed the element, so setting the
     fold state afterwards would leave the sheet's attributes in a different
     order than the imperative builder left them. Same DOM, same order. */
  el.dataset.fold = String(!!store.run(key)?.folded)
  const root = createRoot(el)
  HOSTS.set(key, { el, root })
  sheetAdd(el, key, () => {
    HOSTS.delete(key)
    /* Off the current task: unmounting a root from inside a React commit --
       which is where this lands when the rack retires a sheet during a render --
       is refused with a warning. */
    setTimeout(() => root.unmount(), 0)
  })
  root.render(<Sheet sess={key} host={el} onClose={() => drop(key)} />)
}

/* A graph arrives whole, before any node runs, so this is also the only moment
   the layout is decided. Replaces whatever that conversation was watching: one
   run at a time is what a sheet can show, and the previous run keeps its own
   card in the trail. */
export function start(key: string, run: DagRun): void {
  store.set(key, run)
  /* A replacement is not a change in the rack: this conversation already has a
     host, so nothing docks and nothing leaves, and the asking watcher never
     fires for the new run. Left at that, a graph that replaces one standing
     under a question arrives unfolded and pushes that question below the fold
     again -- the failure this island steps aside to prevent. So the run is
     handed the state the rack is already in, rather than only the changes to
     it. Harmless for a first graph, where `sheetAdd` reports the same state a
     line later: `takeFold` refuses a second claim on the same key. */
  onAsking(key, askingIn(key))
  host(key)
}

/* What `dag.get` answers, as much of it as a resumed sheet reads. Loose in the
   same way the adapters are: this crosses the wire, so every field is a claim.
 */
interface RunWire {
  run_id?: unknown
  dir?: unknown
  finalized?: unknown
  task_summary?: unknown
  files?: unknown
  summary?: unknown
}

const text = (v: unknown): string => (typeof v === 'string' ? v : '')

/* What the two live events carry, as loosely as they cross the wire. */
interface NodeWire {
  run_id?: unknown
  node?: unknown
  status?: unknown
  started_at?: unknown
  ended_at?: unknown
}

interface CompletionWire {
  run_id?: unknown
  dir?: unknown
  summary?: unknown
  files?: unknown
}

/* Put a sheet back after the page was replaced.
 *
 * The reader had one open on this conversation and `dag.get` has just said what
 * the run looks like now, so `wire` decides every node's status and the stored
 * note decides only the fold. That asymmetry is the reason this exists at all:
 * a resumed graph that trusted a stored copy would show the run frozen at the
 * moment of the reload, and the nodes that finished while the page was away
 * would sit at `running` for good.
 *
 * False when there is nothing to put back -- no note, a sheet already up (the
 * live events won the race), or a read that came back without nodes. */
export function resume(key: string, wire: unknown): boolean {
  const kept = store.saved(key)
  if (!kept || store.run(key)) return false
  const run = (wire || {}) as RunWire
  const nodes = fromSnapshot(run.files)
  if (!nodes.length) return false
  start(key, {
    /* The read's own id, falling back to the note's: the two agree, and asking
       the answer rather than the request is what keeps a run that was resumed
       under a canonical id from being drawn under the one we asked with. */
    run_id: text(run.run_id) || kept.run,
    session: key,
    order: nodes.map((n) => n.id),
    nodes: new Map(nodes.map((n) => [n.id, n])),
    summary: (run.summary || null) as DagSummary | null,
    /* The manifest is written when the run finalizes, so this is the gateway's
       own answer to "is it over" -- not something the page can infer from node
       statuses, which an interrupted run leaves looking unfinished forever. */
    done: !!run.finalized,
    folded: kept.folded,
    dir: text(run.dir) || null,
    task_summary: text(run.task_summary) || null,
  })
  return true
}

const num = (v: unknown): number | null => (typeof v === 'number' && isFinite(v) ? v : null)

/* One node's report. Applied to the run the sheet is watching, and only when
   that is the run the event is about: the sheet shows one graph per
   conversation, and an event for another one belongs to a card in the trail. */
export function advance(key: string, p: NodeWire): void {
  const d = store.run(key)
  if (!d || d.run_id !== String(p.run_id)) return
  const n = d.nodes.get(String(p.node))
  if (!n) return
  n.status = text(p.status) || n.status
  n.started_at = num(p.started_at) ?? n.started_at
  n.ended_at = num(p.ended_at) ?? n.ended_at
  store.touch()
}

/* The run's last word: each node's final status, the tally, and `done`.
 *
 * Not the fold. That belongs to the reader, and a run finishing is news about
 * the run rather than an instruction about the sheet -- see the note further
 * down where the fold used to be written.
 *
 * The status and nothing else. This used to stamp `Date.now()` on any node whose
 * own end nobody had reported, which measured that node from its own start to
 * the whole run's end -- so a node that finished in the first twenty seconds of
 * a five-minute graph read as having taken the five minutes. A node whose end is
 * unknown shows no duration instead, which is what the trail's card already does
 * with the same gap, and one reload reads the manifest, which carries the real
 * stamps. */
export function settle(key: string, p: CompletionWire): void {
  const d = store.run(key)
  if (!d || d.run_id !== String(p.run_id)) return
  const files = Array.isArray(p.files) ? p.files : []
  const named = new Set<string>()
  files.forEach((f) => {
    const n = f && typeof f === 'object' ? d.nodes.get(String((f as NodeWire).node)) : undefined
    if (!n) return
    named.add(n.id)
    n.status = text((f as NodeWire).status) || n.status
  })
  /* A node the closing manifest did not name, that the run last reported as
     running, cannot still be running: the run has closed. A run that ends
     without a manifest -- collapsed, or stopped -- carries no `files` at all, and
     `raven/rpc/spine.py` says the coercion that turns that into an empty list is
     load-bearing rather than defensive, precisely so a consumer can close the
     graph rather than crash. `ui-tui/src/domain/dagRun.ts` reads it that way and
     this side did not, so the last reported node stayed running under a graph
     that says done and its duration went on counting.
     Only a running node moves. Pending and terminal nodes the manifest omits are
     left exactly as they were -- a node that never started did not get
     interrupted. */
  d.nodes.forEach((n) => {
    if (!named.has(n.id) && n.status === 'running') n.status = 'interrupted'
  })
  d.summary = (p.summary || null) as DagSummary | null
  d.dir = text(p.dir) || null
  d.done = true
  /* The fold is the reader's, and finishing is not a reason to take it. This
     used to collapse itself the moment the run ended, which is the moment its
     result is worth reading -- and it collapsed a sheet the reader had just
     opened to watch, so the graph they were following disappeared at the end.
     A finished sheet stays as they left it and closes when they close it. */
  store.touch()
}

/* The caller mutated the run it holds -- a node's status, a time, the summary --
   and wants the sheet to say so. */
export const touch = (): void => store.touch()

/* Called wherever the open conversation changes, right after the rack's own
   sync: a sheet that has just been mounted can measure its labels, and one that
   has just been detached should stop trying. */
export const sync = (): void => store.touch()

/* The conversation went away. Not the same as the reader closing the sheet, but
   it ends the same way. */
export const forget = (key: string): void => drop(key)

export const run = (key: string): DagRun | null => store.run(key)

/* What this conversation had open before the page was replaced. Read by
   shell/resume.ts, which is what turns it back into a sheet. */
export const saved = store.saved

/* Test seam. */
export function _resetForTests(): void {
  HOSTS.forEach(({ root }) => root.unmount())
  HOSTS.clear()
  store._resetForTests()
  unwatch()
  unwatch = watchAsking(onAsking)
}
