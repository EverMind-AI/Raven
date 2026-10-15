/* -- imported conversations: the rpc source and how its refusals read ------- */

import { gateway } from '../../rpc/gateway'

import type { ImportedSource } from './types'

/** The package was refused; `data.reason` says why. */
export const PACKAGE_CODE = -32030
/** Another import holds the gateway's one slot. */
export const BUSY_CODE = -32031

export const importedSource: ImportedSource = {
  upload: (name, contentB64, force) => gateway().call('imported.upload', { name, content_b64: contentB64, force }),
}

export const codeOf = (err: unknown): number | null =>
  err !== null && typeof err === 'object' && typeof (err as { code?: unknown }).code === 'number'
    ? (err as { code: number }).code
    : null

const dataOf = (err: unknown): Record<string, unknown> => {
  const data = err !== null && typeof err === 'object' ? (err as { data?: unknown }).data : undefined
  return data !== null && typeof data === 'object' ? (data as Record<string, unknown>) : {}
}

/** Why a package was refused, in the gateway's stable words (`not_importable`, `too_large`, ...). */
export const reasonOf = (err: unknown): string => {
  const reason = dataOf(err).reason
  return typeof reason === 'string' ? reason : ''
}

/** The sentence the gateway gave, or the error's own message. */
export const detailOf = (err: unknown): string => {
  const detail = dataOf(err).detail
  if (typeof detail === 'string' && detail) return detail
  const message = err !== null && typeof err === 'object' ? (err as { message?: unknown }).message : undefined
  return typeof message === 'string' && message ? message : String(err)
}
