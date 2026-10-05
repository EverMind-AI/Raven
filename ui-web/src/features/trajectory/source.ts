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

export const trajectorySource: TrajectorySource = {
  state: () => gateway().call('trajectory.state', {}),
  list: (sessionKey, cursor) =>
    gateway().call('trajectory.list', { session_key: sessionKey, cursor: cursor ?? null, limit: LIST_PAGE }),
  changes: (sessionKey, epoch, afterRevision) =>
    gateway().call('trajectory.changes', {
      session_key: sessionKey, epoch, after_revision: afterRevision, limit: CHANGES_BATCH,
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
