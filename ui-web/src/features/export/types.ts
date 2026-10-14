/* The export wizard's view of the gateway: the conversation's turns, and a
   background job that packs the chosen ones into a download. */

import type { ResultOf } from '../../rpc/generated'

export type ExportTurns = ResultOf<'trajectory.turns'>
export type ExportTurn = ExportTurns['turns'][number]
export type ExportStatus = ResultOf<'trajectory.export_status'>

/** One turn the gateway refused to select, and why (`in_progress`, `not_in_session`). */
export interface InvalidTurn {
  trace_id: string
  why: string
}

export interface ExportSource {
  turns(sessionKey: string): Promise<ExportTurns>
  start(sessionKey: string, traceIds: string[], fileName: string): Promise<{ job_id: string }>
  status(jobId: string): Promise<ExportStatus>
  cancel(jobId: string): Promise<{ ok: boolean }>
}
