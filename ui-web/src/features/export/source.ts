/* -- the export wizard: the rpc source and how its refusals read ------------ */

import { gateway } from '../../rpc/gateway'

import type { ExportSource, InvalidTurn } from './types'

export const DISABLED_CODE = -32020
export const NOT_FOUND_CODE = -32024
export const LIMIT_CODE = -32025
const INVALID_PARAMS_CODE = -32602

export const exportSource: ExportSource = {
  turns: (sessionKey) => gateway().call('trajectory.turns', { session_key: sessionKey }),
  start: (sessionKey, traceIds, fileName) =>
    gateway().call('trajectory.export', { session_key: sessionKey, trace_ids: traceIds, file_name: fileName }),
  status: (jobId) => gateway().call('trajectory.export_status', { job_id: jobId }),
  cancel: (jobId) => gateway().call('trajectory.export_cancel', { job_id: jobId }),
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

/** As many exports are running as the gateway allows. */
export const isLimit = (err: unknown): boolean => codeOf(err) === LIMIT_CODE

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
