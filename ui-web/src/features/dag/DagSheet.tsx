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

import { useEffect, useLayoutEffect, useState, useSyncExternalStore } from 'react'

import { ds, t } from '../../shell/bridge'
import { CHEVRON_DOWN, CROSS, Glyph } from '../../shell/ico'
import { revealStint } from '../playbooks/store'
import { getState as subState, subscribe as subSubscribe } from '../subagents/store'
import { DagGraph } from './DagGraph'
import { SHEET, ordered, summary } from './graph'
import * as store from './store'

import type { DagRun } from './types'
import type { TranscriptSource } from '../transcript/types'
import type { JSX } from 'react'

const anyRunning = (d: DagRun): boolean => [...d.nodes.values()].some((n) => n.status === 'running')

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
  const [, tick] = useState(0)

  /* Folding is a class-free attribute on the sheet's own element, which React
     does not own: it is the container this tree was rendered into. */
  useLayoutEffect(() => { host.dataset.fold = String(!!(d && d.folded)) })

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
  const nodes = ordered(d)
  const sel = sub.open && sub.open.kind === 'dag' ? sub.open : null
  const foldLabel = t(d.folded ? 'gui.dag.unfold' : 'gui.dag.fold')

  return (
    <>
      <div className="hd">
        {/* What this run is for, in the model's own words. The generic word
            was the title until there was a line to put here, and it said the
            same thing on every graph the reader had ever watched. */}
        <span className="ttl" title={d.task_summary || undefined}>{d.task_summary || t('gui.dag.title')}</span>
        <Round run={d} />
        {/* The tally when there is one, and nothing before then. The graph's
            shape used to sit here while it ran -- node count, depth, how many
            run at once -- which is the one thing the picture below says better
            than a sentence can, and it said it on every graph the reader had
            ever watched. */}
        {d.done ? <div className="sum">{summary(d)}</div> : null}
        <button className="ic tipdn" data-tip={foldLabel} aria-label={foldLabel}
          onClick={() => store.fold(sess, !d.folded)}>
          <Glyph d={CHEVRON_DOWN} cls="cv" />
        </button>
        <button className="ic tipdn" data-tip={t('gui.dag.close')} aria-label={t('gui.dag.close')}
          onClick={onClose}>
          <Glyph d={CROSS} />
        </button>
      </div>
      <DagGraph dims={SHEET} nodes={nodes} now={Date.now()} surface="sheet"
        selectedId={sel && sel.run_id === d.run_id ? sel.node : null}
        onPick={(n) => ds<TranscriptSource>('transcript').openDagNode?.(d.run_id, n.id, n.node_summary)} />
    </>
  )
}

/* Which round of which multi-round run this graph is, when it is one.
 *
 * A stint dispatches one ordinary graph a round, so without this the sheet is
 * indistinguishable from a graph a tool call started -- and it is replaced by
 * the next round's, which reads as the same run having been redrawn. The mark
 * says both things the picture cannot: that there is a run above this graph,
 * and how far through it this graph is.
 *
 * It is a button because the thing it names has a page, and that page is where
 * the rounds before this one are. The title line does carry the round in prose
 * (`rounds: round 3 of at most 30`, the dispatcher's own task summary) --
 * this is the same fact where a reader's eye already goes for status, and it
 * leads somewhere.
 */
/* Opening the page is the part that must happen; reading the run into it is the
   part that can fail. `revealStint` resolves the playbooks seam eagerly and
   throws when the page's island was never installed -- from an onClick that
   takes the render tree down with it, over a detail the reader can also get by
   looking at the page they are now on. */
async function openRun(stintId: string): Promise<void> {
  try {
    await revealStint(stintId)
  } catch {
    /* the page is open; it will load its own list */
  }
}

function Round({ run }: { run: DagRun }): JSX.Element | null {
  if (!run.stint_id || !run.round_index) return null
  const label = run.round_budget
    ? t('gui.pb.stint_round', { n: String(run.round_index), max: String(run.round_budget) })
    : t('gui.pb.stint_rounds_run', { n: String(run.round_index) })
  return (
    <button className="dround tipdn" type="button" data-tip={t('gui.dag.stint_open')}
      aria-label={t('gui.dag.stint_open')}
      onClick={() => { void openRun(run.stint_id as string) }}>
      {label}
    </button>
  )
}
