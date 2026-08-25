/* Putting the page back to what it was showing, one conversation at a time.
 *
 * Each store keeps its own note of what the reader had open (shell/persist.ts);
 * this is the one place that spends those notes, because resuming means asking
 * the gateway what the work looks like NOW and replaying the opens in the order
 * they were made -- and neither of those belongs to any single store.
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

import { resume as dagResume, saved as dagSaved } from '../features/dag/mount'
import * as agents from '../features/subagents/store'
import * as desk from '../features/workspace/deskStore'
import { ds } from './bridge'
import { current } from './session'

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
    desk.openDeskFile(intent.path, intent.dl)
    return
  }
  /* A graph node is reopenable from its (run, node) pair alone: the panel opens
     the node's own record and promotes it to the instance when the row lands,
     which is exactly what it does for a click on the sheet. */
  if (intent.run && intent.node) {
    agents.openDagNode(intent.run, { id: intent.node })
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

async function resumeDag(key: string): Promise<void> {
  const kept = dagSaved(key)
  if (!kept) return
  /* `dag.get` and nothing else decides what the nodes are doing: the note says
     which run and whether it was folded, never a status. */
  const read = ds<TranscriptSource>('transcript').dagRun
  if (!read) return
  dagResume(key, await read(kept.run))
}

/* Whatever this conversation had: the desk, the sheet, or nothing.
 *
 * Settled rather than awaited as a chain, and per part. A run whose directory
 * has been cleaned and a file that has since been deleted are both ordinary
 * outcomes, and neither is a reason for the rest of the layout to stay away. */
export async function resume(key: string): Promise<void> {
  await Promise.allSettled([resumeDesk(key), resumeDag(key)])
}
