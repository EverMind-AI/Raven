/* What the trajectory view is made of, as the page holds it.
 *
 * The wire shapes are `trajectory.*`'s own contract types, re-exported rather
 * than hand-maintained -- see rpc-schema/openrpc.json's `TrajectoryEntry` and
 * friends. What is added here is the vocabulary the island speaks that the
 * wire does not: which of the chat column's two views is up, and which door a
 * selection came through.
 */

import type {
  JsonValue,
  TrajectoryChangesResult,
  TrajectoryEntry,
  TrajectoryIndexState,
  TrajectoryListResult,
  TrajectoryRemoved,
  TrajectoryStateResult,
} from '../../rpc/generated'

export type {
  JsonValue,
  TrajectoryChangesResult,
  TrajectoryEntry,
  TrajectoryIndexState,
  TrajectoryListResult,
  TrajectoryRemoved,
  TrajectoryStateResult,
}

/** The chat column shows the conversation, or the trajectory of it. */
export type View = 'chat' | 'trajectory'

/* Where a selection was made. Every door goes through the store's one
   `select`, and the door is recorded so a later surface (the duration bar, a
   detail link) can tell its own selection from the reader's click. */
export type SelectSource = 'click' | 'keyboard' | 'bar' | 'link' | 'migrate'

/* The seam the island reads. Three reads and nothing else: the view is a
   mirror of the session's trace store and writes nothing back. */
export interface TrajectorySource {
  state(): Promise<TrajectoryStateResult>
  list(sessionKey: string, cursor?: string | null): Promise<TrajectoryListResult>
  changes(sessionKey: string, epoch: string, afterRevision: number): Promise<TrajectoryChangesResult>
}
