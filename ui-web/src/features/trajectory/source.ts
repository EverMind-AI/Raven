/* -- trajectory: the source --------------------------------------------
   Everything this domain knows about speaking to the gateway: the three
   reads, with the page sizes the view is built around, and the two verdicts a
   failed call can carry that are about the surface rather than the call.
*/

import { gone } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'

import type { TrajectorySource } from './types'

/** One `trajectory.list` page: what the first screen reads in one go. */
export const LIST_PAGE = 200
/** One `trajectory.changes` batch: the contract's own ceiling. */
export const CHANGES_BATCH = 500

/** The code `trajectory.*` answers when the view is off for this process. */
export const DISABLED_CODE = -32020
/** The code `trajectory.list` answers for a page cursor whose snapshot is gone. */
export const CURSOR_EXPIRED_CODE = -32022
/** The code the detail reads answer for an entry the index no longer has, or a block it does not know. */
export const ENTRY_NOT_FOUND_CODE = -32021
/** The code the detail reads answer when the entry moved on since the caller looked. */
export const REVISION_CHANGED_CODE = -32023

export const trajectorySource: TrajectorySource = {
  state: () => gateway().call('trajectory.state', {}),
  list: (sessionKey, cursor) =>
    gateway().call('trajectory.list', { session_key: sessionKey, cursor: cursor ?? null, limit: LIST_PAGE }),
  changes: (sessionKey, epoch, afterRevision) =>
    gateway().call('trajectory.changes', {
      session_key: sessionKey, epoch, after_revision: afterRevision, limit: CHANGES_BATCH,
    }),
  detail: (sessionKey, entryId, revision) =>
    gateway().call('trajectory.detail', { session_key: sessionKey, entry_id: entryId, entry_revision: revision ?? null }),
  block: (sessionKey, entryId, revision, epoch, blockId, cursor) =>
    gateway().call('trajectory.block', {
      session_key: sessionKey, entry_id: entryId, entry_revision: revision, epoch, block_id: blockId, cursor: cursor ?? null,
    }),
}

const codeOf = (err: unknown): number | null =>
  err !== null && typeof err === 'object' && typeof (err as { code?: unknown }).code === 'number'
    ? (err as { code: number }).code
    : null

/** The gateway serves the surface but has the view switched off. */
export const isDisabled = (err: unknown): boolean => codeOf(err) === DISABLED_CODE

/** The snapshot a list walk was reading has been let go; the walk starts over. */
export const isCursorExpired = (err: unknown): boolean => codeOf(err) === CURSOR_EXPIRED_CODE

/* The gateway does not serve the surface at all. Recorded through the page's
   one -32601 memory, so a later caller can ask without a second failed call. */
export const isAbsent = (err: unknown): boolean => gone('trajectory', err)

/** The same verdict read off the error alone, with nothing remembered. */
export const saysAbsent = (err: unknown): boolean => codeOf(err) === -32601

const dataOf = (err: unknown): Record<string, unknown> => {
  const data = err !== null && typeof err === 'object' ? (err as { data?: unknown }).data : undefined
  return data !== null && typeof data === 'object' ? (data as Record<string, unknown>) : {}
}

/** The entry is no longer in the index (the same code with a `block_id` names an unknown block instead). */
export const isEntryGone = (err: unknown): boolean =>
  codeOf(err) === ENTRY_NOT_FOUND_CODE && typeof dataOf(err).block_id !== 'string'

/** The entry exists but has no block by that id. */
export const isUnknownBlock = (err: unknown): boolean =>
  codeOf(err) === ENTRY_NOT_FOUND_CODE && typeof dataOf(err).block_id === 'string'

/* The entry moved on since the caller looked: where it is now, so the caller
   can ask again at the right revision, or null for any other failure. */
export function revisionChange(err: unknown): { revision: number; epoch: string } | null {
  if (codeOf(err) !== REVISION_CHANGED_CODE) return null
  const data = dataOf(err)
  const revision = data.current_revision
  const epoch = data.current_epoch
  return typeof revision === 'number' && typeof epoch === 'string' ? { revision, epoch } : null
}
