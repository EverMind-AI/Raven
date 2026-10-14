/* -- the export wizard: the rpc source and how its refusals read ------------ */

import { gateway } from '../../rpc/gateway'

import type { ExportSource, InvalidTurn } from './types'

export const DISABLED_CODE = -32020
export const NOT_FOUND_CODE = -32024
export const LIMIT_CODE = -32025
export const UNAVAILABLE_CODE = -32026
export const CONFLICT_CODE = -32027
export const STATE_CODE = -32028
const INVALID_PARAMS_CODE = -32602

export const exportSource: ExportSource = {
  turns: (sessionKey) => gateway().call('trajectory.turns', { session_key: sessionKey }),
  start: (sessionKey, traceIds, fileName) =>
    gateway().call('trajectory.export', { session_key: sessionKey, trace_ids: traceIds, file_name: fileName }),
  status: (jobId) => gateway().call('trajectory.export_status', { job_id: jobId }),
  cancel: (jobId) => gateway().call('trajectory.export_cancel', { job_id: jobId }),
  reportStart: (sessionKey, traceIds, problem) =>
    gateway().call('bugreport.start', { session_key: sessionKey, trace_ids: traceIds, problem }),
  reportStatus: (reportId) => gateway().call('bugreport.status', { report_id: reportId }),
  reportItem: (reportId, itemId) => gateway().call('bugreport.item', { report_id: reportId, item_id: itemId }),
  reportDecide: (reportId, decisions, acknowledged) =>
    gateway().call('bugreport.decide', { report_id: reportId, decisions, acknowledged }),
  reportUpload: (reportId, riskAccepted) =>
    gateway().call('bugreport.upload', { report_id: reportId, risk_accepted: riskAccepted }),
  reportRetry: (reportId) => gateway().call('bugreport.retry_upload', { report_id: reportId }),
  reportCancel: (reportId) => gateway().call('bugreport.cancel', { report_id: reportId }),
}

const codeOf = (err: unknown): number | null =>
  err !== null && typeof err === 'object' && typeof (err as { code?: unknown }).code === 'number'
    ? (err as { code: number }).code
    : null

const dataOf = (err: unknown): Record<string, unknown> => {
  const data = err !== null && typeof err === 'object' ? (err as { data?: unknown }).data : undefined
  return data !== null && typeof data === 'object' ? (data as Record<string, unknown>) : {}
}

/** The trajectory policy is off on this gateway. */
export const isDisabled = (err: unknown): boolean => codeOf(err) === DISABLED_CODE

/** The job is no longer the gateway's: it restarted, or the job's record expired. */
export const isGone = (err: unknown): boolean => codeOf(err) === NOT_FOUND_CODE

/** As many exports, or bug reports, are running as the gateway allows. */
export const isLimit = (err: unknown): boolean => codeOf(err) === LIMIT_CODE

/* The gateway answered with a refusal of its own. A transport failure (the
   socket closed, or never opened) carries -1, and an error without a code is
   no answer at all: either way the call may or may not have been taken. */
export const answered = (err: unknown): boolean => {
  const code = codeOf(err)
  return code !== null && code !== -1
}

/** The gateway has no bug report receiver configured. */
export const isUnavailable = (err: unknown): boolean => codeOf(err) === UNAVAILABLE_CODE

const ids = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : []

/** The linked items that were given different decisions, or null for any other error. */
export const conflictOf = (err: unknown): string[] | null =>
  codeOf(err) === CONFLICT_CODE ? ids(dataOf(err).item_ids) : null

/** Why the report refused a step in its phase, or null for any other error. */
export function stateOf(err: unknown): { phase: string; reason: string; itemIds: string[] } | null {
  if (codeOf(err) !== STATE_CODE) return null
  const data = dataOf(err)
  return {
    phase: typeof data.phase === 'string' ? data.phase : '',
    reason: typeof data.reason === 'string' ? data.reason : '',
    itemIds: ids(data.item_ids),
  }
}

/* The selection was refused turn by turn: each one the gateway would not take
   and why. Any other invalid-params answer is an ordinary error. */
export function invalidSelection(err: unknown): { invalid: InvalidTurn[]; why: string | null } | null {
  if (codeOf(err) !== INVALID_PARAMS_CODE) return null
  const data = dataOf(err)
  if (data.reason !== 'invalid_selection') return null
  const invalid = Array.isArray(data.invalid)
    ? data.invalid.filter(
      (row): row is InvalidTurn =>
        row !== null && typeof row === 'object'
        && typeof (row as InvalidTurn).trace_id === 'string' && typeof (row as InvalidTurn).why === 'string',
    )
    : []
  return { invalid, why: typeof data.why === 'string' ? data.why : null }
}

/** What an error says, for the wizard's error line. */
export const messageOf = (err: unknown): string =>
  err instanceof Error ? err.message
  : err !== null && typeof err === 'object' && typeof (err as { message?: unknown }).message === 'string'
    ? (err as { message: string }).message
    : String(err)
