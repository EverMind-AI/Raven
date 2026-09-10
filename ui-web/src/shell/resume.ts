/* Putting the page back to what it was showing, one conversation at a time.
 *
 * Each store keeps its own note of what the reader had open (shell/persist.ts);
 * this is the one place that spends those notes, because resuming means asking
 * the gateway what the work looks like NOW and replaying the opens in the order
 * they were made -- and neither of those belongs to any single store.
 *
 * A note is a preference, though, not the price of admission. It is written when
 * a live event is applied, so a conversation whose work began while the reader
 * was in another one has none -- and that is the conversation with the most to
 * put back. The graph half therefore also takes the run ids the caller read off
 * `session.resume`, and asks the gateway even where this page remembers nothing
 * (see `dagToRead`).
 *
 * Called when a conversation is opened rather than at boot, which is a decision
 * and not a convenience. The page boots to the new-task screen on purpose
 * (live/200-boot.js), so at startup there is no conversation for a restored
 * sheet to belong to; the rack is session-scoped for the same reason, filing a
 * sheet under the conversation that raised it and mounting it only while that
 * one is open. A reload that gives nothing back until the reader returns to the
 * conversation is therefore the shape the page already has for a session
 * switch, not a new one.
 *
 * Replaying the opens, rather than rebuilding the panes, is what keeps a resumed
 * window honest: it goes through the same verb a click goes through, so it reads
 * its own body from the gateway and lands in the same slot with the same
 * promotion rules.
 */

import { resume as dagResume, run as dagOpen, saved as dagSaved } from '../features/dag/mount'
import * as agents from '../features/subagents/store'
import * as desk from '../features/workspace/deskStore'
import { ds } from './bridge'
import { only } from './persist'
import { current, onChange } from './session'

import type { DeskIntent } from '../features/workspace/deskStore'
import type { AgentRow, InstanceRow } from '../features/subagents/types'
import type { TranscriptSource } from '../features/transcript/types'

/* A direct chat and a spawn's record are named by ids only the panel's own
   lists can resolve, and a page that has just loaded has asked for neither. So
   the intent waits on the panel's own read of that list -- awaited, rather than
   watched for through the store's notifications, because the store notifies for
   more than a list answer: another intent's replay notifies too, and taking that
   for "the list came back without my row" gave up on a window that was about to
   be listed. */
async function whenListed<T>(pick: () => T | null, ask: () => Promise<void>, open: (found: T) => void): Promise<void> {
  const first = pick()
  if (first) {
    open(first)
    return
  }
  await ask()
  const found = pick()
  /* Nothing to do when it is still not there: the row has been forgotten, or the
     read failed. Either way the intent stays on file (see deskStore.replaying),
     so the next reload asks again rather than deciding from one failed read that
     the reader never had that window. */
  if (found) open(found)
}

async function replay(intent: DeskIntent): Promise<void> {
  if (intent.k === 'file') {
    desk.openDeskFile(intent.path)
    return
  }
  /* A graph node is reopenable from its (run, node) pair alone: the panel opens
     the node's own record and promotes it to the instance when the row lands,
     which is exactly what it does for a click on the sheet. */
  if (intent.run && intent.node) {
    /* With the heading the reader had. The summary that produces it lives on the
       run, and `resumeDag` reads that concurrently with this replay -- so asking
       the run for it here is a race, and loses outright when the run's directory
       has been cleaned. The note carries it instead. */
    agents.openDagNode(intent.run, { id: intent.node, summary: intent.k === 'record' ? intent.label : null })
    return
  }
  if (intent.k === 'agent') {
    await whenListed<InstanceRow>(
      () => agents.instances().find((row) => row.agent === intent.agent && row.handle === intent.handle) || null,
      () => agents.refreshInstances(true),
      (row) => agents.openInstance(row),
    )
    return
  }
  if (!intent.id) return
  await whenListed<AgentRow>(
    () => agents.rows().find((row) => row.id === intent.id) || null,
    () => agents.refresh(true),
    (row) => agents.openRow(row),
  )
}

async function resumeDesk(key: string): Promise<void> {
  const kept = desk.saved(key)
  if (!kept) return
  /* Marked for the whole replay, ended in a `finally`: the opens go through the
     same verbs a reader's clicks do, and the note must not be rewritten from a
     desk that is only half-way back. */
  desk.replaying(key, true)
  try {
    /* Started in order -- the synchronous opens land in the order the reader
       made them -- and all awaited, so the frame below is applied once the last
       of them is up. */
    await Promise.allSettled(kept.open.map(replay))
    /* Asked again on this side of the await: a list can take long enough for the
       reader to open another conversation, and the desk on screen is then that
       one's. The opens are safe on their own -- a list answer for the
       conversation being left is discarded, so nothing is found to open -- but
       the frame is not: `tab` and `splits` have no pane to be absent, so they
       would land on whatever is there, and that conversation's next mutation
       would file them as its own. */
    if (current() === key) desk.applyLayout(kept)
  } finally {
    desk.replaying(key, false)
  }
}

/* Which run this conversation's sheet should be showing, or nothing.
 *
 * Three sources, in the order they can be trusted to be current:
 *
 * - the graph already on screen, which is the newest thing this page knows: a
 *   run that started since the transcript was read is only here;
 * - the stored note, which names the run the page was watching when it was
 *   last replaced. It exists only where a live event for this conversation was
 *   once applied, so it is absent exactly when the third source is needed -- but
 *   where it is present it is the reader's own choice of which graph to watch,
 *   and a conversation that ran several does not get moved off it;
 * - the ids the transcript carries, which is the gateway's own record of every
 *   graph this conversation started. Last because it is the only one that cannot
 *   express a preference, and new: it is what survives a run the reader never saw
 *   start, where no live event reached this page and so nothing was written down.
 *
 * The newest of those ids rather than the first: a sheet shows one graph per
 * conversation, and with nothing to say which the reader wanted, the newest is
 * the one whose nodes may still be moving. */
const dagToRead = (key: string, runIds?: readonly string[]): string | null =>
  dagOpen(key)?.run_id || dagSaved(key)?.run
  || (runIds && runIds.length ? runIds[runIds.length - 1]! : null)

async function resumeDag(key: string, runIds?: readonly string[]): Promise<void> {
  const id = dagToRead(key, runIds)
  if (!id) return
  /* `dag.get` and nothing else decides what the nodes are doing: the sources
     above say WHICH run, never a status. */
  const read = ds<TranscriptSource>('transcript').dagRun
  if (!read) return
  dagResume(key, await read(id))
}

/* Whatever this conversation had: the desk, the sheet, or nothing.
 *
 * `dagRunIds` is every run the conversation's transcript names, oldest first, as
 * the caller read them off `session.resume`. Optional: a caller with none still
 * gets the sheet back from what the page itself remembers.
 *
 * Settled rather than awaited as a chain, and per part. A run whose directory
 * has been cleaned and a file that has since been deleted are both ordinary
 * outcomes, and neither is a reason for the rest of the layout to stay away. */
export async function resume(key: string, dagRunIds?: readonly string[]): Promise<void> {
  await Promise.allSettled([resumeDesk(key), resumeDag(key, dagRunIds)])
}

/* The graph alone, for a conversation that never left this page.
 *
 * Coming back to a conversation whose turn was parked restores its transcript
 * from detached DOM rather than from disk, so `resume` above is the wrong verb
 * there: the desk half replays the reader's opens through the same verbs a click
 * goes through, and every window they had would come back a second time. The
 * graph half has the opposite problem and needs running -- a parked turn buffers
 * the events that arrive while it is away, but only while it is BUSY, and a
 * graph outlives the turn that started it. So the node reports that land after
 * that turn ends are dropped, and the sheet on screen is stale until something
 * re-reads it. This is that something.
 *
 * Settled rather than awaited, for the reason `resume` above settles: a run
 * whose directory has been cleaned answers with a rejection, and the live layer
 * calls this without awaiting it -- so a raise here would be an unhandled
 * rejection rather than a sheet that simply does not refresh. */
export async function refreshDag(key: string): Promise<void> {
  await Promise.allSettled([resumeDag(key)])
}

/* Which conversation the tab was on.
 *
 * Boot opens the new-task screen rather than the last conversation, and that is
 * a deliberate first frame: landing in someone's half-finished transcript is
 * worse than an empty composer. A RELOAD is not a first frame, though -- the
 * reader was already somewhere and did not ask to leave -- and the two are
 * exactly what this store tells apart. A refreshed tab kept its note and comes
 * back to the conversation with the sheet and the desk it had; a tab opened
 * fresh has none, and still gets the new-task screen.
 *
 * Recorded from the session pointer itself rather than from the openers, so
 * every way of arriving somewhere is covered by one line -- including a draft
 * becoming a session on its first message, which no opener runs for.
 *
 * Started by the LIVE layer rather than when this module loads, and that is
 * load-bearing. The demo shell runs first on every page, live mode included,
 * and it drives the same pointer with fixture data: it opens its canned session
 * and the live boot guard then clears the pointer again, which between them
 * rewrote the note and then deleted it before boot could ever read it. Watching
 * from the point the live layer takes over means the first change this sees is
 * a real one. A page with no live layer -- the demo on its own -- records
 * nothing, which is right: it has no conversation to come back to. */
const OPEN = only<{ id: string }>('open', 1)

export function watch(): void {
  onChange((id) => {
    /* A draft is cleared rather than recorded: the reader asking for a new task
       is the one case where coming back to the last conversation is wrong, and
       a draft has no id to come back to anyway. */
    if (id) OPEN.write({ id })
    else OPEN.clear()
  })
}

/* Where this tab should land at boot: the conversation it was on, or nothing.
 *
 * Asked WITH the reader's own list rather than answered from the note alone. A
 * note for a conversation deleted since names something that is not there, and
 * the new-task screen is the right answer for that -- better than an open that
 * fails in front of the reader with a toast about a session they did not ask
 * for. The decision lives here rather than in the boot sequence so it can be
 * tested; the boot layer only acts on it. */
export function landing(ids: string[]): string | null {
  const id = OPEN.read()?.id
  return id && ids.includes(id) ? id : null
}
