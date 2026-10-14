/* The export wizard's view of the gateway: the conversation's turns, a
   background job that packs the chosen ones into a download, and the bug
   report the same turns can be filed as (one Report Session per report). */

import type { ParamsOf, ResultOf } from '../../rpc/generated'

export type ExportTurns = ResultOf<'trajectory.turns'>
export type ExportTurn = ExportTurns['turns'][number]
export type ExportStatus = ResultOf<'trajectory.export_status'>
export type ReportStatus = ResultOf<'bugreport.status'>
export type ReportItem = NonNullable<ReportStatus['items']>[number]
export type ReportContexts = ResultOf<'bugreport.item'>
export type Problem = ParamsOf<'bugreport.start'>['problem']
export type Severity = NonNullable<Problem['severity']>
export type Action = 'kept' | 'redacted'

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
  reportStart(sessionKey: string, traceIds: string[], problem: Problem): Promise<{ report_id: string }>
  reportStatus(reportId: string): Promise<ReportStatus>
  reportItem(reportId: string, itemId: string): Promise<ReportContexts>
  reportDecide(reportId: string, decisions: { item_id: string; action: Action }[], acknowledged: string[]): Promise<{ ok: boolean }>
  reportUpload(reportId: string, riskAccepted: boolean): Promise<{ ok: boolean }>
  reportRetry(reportId: string): Promise<{ ok: boolean }>
  reportCancel(reportId: string): Promise<{ ok: boolean }>
}
