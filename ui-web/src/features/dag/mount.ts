/* The island's face: the run one conversation is watching, and the four calls
 * the live layer makes into it.
 *
 * No sheet of its own any more. A graph used to dock in the rack above the
 * composer as a `.dsheet`, which put the whole of it between the transcript
 * and the box you type in for as long as the run lasted -- and left a settled
 * one there afterwards. The strip below the rack already names every running
 * task in a line each, and its chip opens the same graph in the desk's task
 * pane with room to pan it, so the sheet was the third place one run was
 * drawn and the only one that charged the conversation for it.
 *
 * What is kept is the run itself. The trail's delegation card is built from
 * the same frames (`features/transcript/store.ts`'s `dagFeed`) and is the
 * conversation's durable record of the graph; this store keeps it for
 * `state/session/resume.ts` to put back on reload, and has no other reader
 * today -- a pane's graph comes from the desk's own `tasks.list` read
 * instead.
 */

import { fromSnapshot } from './nodes'
import * as store from './store'

import type { DagNode, DagRun, DagSummary } from './types'

function drop(key: string): void {
  store.forget(key)
}

/* A graph arrives whole, before any node runs, so this is also the only moment
   the layout is decided. Replaces whatever that conversation was watching: one
   run at a time is what a sheet can show, and the previous run keeps its own
   card in the trail. */
export function start(key: string, run: DagRun): void {
  store.set(key, run)
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

/* Draw what `dag.get` just said, either as a sheet this conversation did not
 * have or as fresher state for the one it does.
 *
 * `wire` decides every node's status and the reader decides only the fold. That
 * asymmetry is the reason this exists at all: a graph that trusted a stored copy
 * would show the run frozen at the moment the page stopped hearing about it, and
 * the nodes that finished in between would sit at `running` for good.
 *
 * A run already held is refreshed rather than declined, and that is the case
 * this was missing. A run is fed live events, and only for the conversation on
 * screen -- so every node report that lands while the reader is in another
 * conversation is dropped, and what they come back to is as stale as the
 * moment they left. Declining is right only for a DIFFERENT run: the live one is
 * then the newer of the two and replacing it would step back to a graph that has
 * already been superseded.
 *
 * Whether this conversation should be asked about at all is the caller's
 * decision, not this function's -- see `resumeDag` in state/session/resume.ts, which
 * refuses when it has no run to read. It used to be settled here by requiring a
 * stored note, which is per-tab and only ever as new as the last event that
 * reached this page: a run that started while the reader was elsewhere left no
 * note, so the read that could have drawn it was never made.
 *
 * False when nothing was put back -- a read that came back without nodes, or a
 * refresh, which updates a sheet rather than raising one. */
export function resume(key: string, wire: unknown): boolean {
  const run = (wire || {}) as RunWire
  const nodes = fromSnapshot(run.files)
  if (!nodes.length) return false
  const id = text(run.run_id)
  const live = store.run(key)
  const kept = store.saved(key)
  if (live) {
    if (!id || live.run_id !== id) return false
    draw(key, run, nodes, id, live.folded)
    return false
  }
  /* The read's own id, falling back to the note's: the two agree, and asking
     the answer rather than the request is what keeps a run that was resumed
     under a canonical id from being drawn under the one we asked with. */
  const drawAs = id || kept?.run
  if (!drawAs) return false
  draw(key, run, nodes, drawAs, !!kept?.folded)
  return true
}

function draw(key: string, run: RunWire, nodes: DagNode[], runId: string, folded: boolean): void {
  start(key, {
    run_id: runId,
    session: key,
    order: nodes.map((n) => n.id),
    nodes: new Map(nodes.map((n) => [n.id, n])),
    summary: (run.summary || null) as DagSummary | null,
    /* The manifest is written when the run finalizes, so this is the gateway's
       own answer to "is it over" -- not something the page can infer from node
       statuses, which an interrupted run leaves looking unfinished forever. */
    done: !!run.finalized,
    folded,
    dir: text(run.dir) || null,
    task_summary: text(run.task_summary) || null,
  })
}

const num = (v: unknown): number | null => (typeof v === 'number' && isFinite(v) ? v : null)

/* One node's report. Applied to the run this conversation is holding, and only
   when that is the run the event is about: one run is held per conversation,
   and an event for another one belongs to a card in the trail. */
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
 * the run rather than an instruction about how it is drawn -- see the note
 * further down where the fold used to be written.
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
   and wants its readers told. Nothing subscribes today: the sheet was the one
   subscriber and the trail's card keeps its own copy of the same frames. Kept
   as the store's own verb rather than deleted through its callers, so a reader
   added later has the notification it expects. */
export const touch = (): void => store.touch()

/* Called wherever the open conversation changes, from src/main.tsx. */
export const sync = (): void => store.touch()

/* The conversation went away, and its run goes with it. */
export const forget = (key: string): void => drop(key)

export const run = (key: string): DagRun | null => store.run(key)

/* What this conversation was holding before the page was replaced. Read by
   state/session/resume.ts, which puts the run back with it or without it: the
   note settles which run, never whether there is one. */
export const saved = store.saved

/* Test seam. */
export function _resetForTests(): void {
  store._resetForTests()
}
