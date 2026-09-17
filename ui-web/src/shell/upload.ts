/* What the page refuses to send before it asks the gateway to take it.
 *
 * Mirrors MAX_UPLOAD_BYTES in raven/rpc/files.py, which is the source; change
 * it there first. Kept as a second copy because no limit rides the wire today
 * and putting one there is a contract change this does not need.
 *
 * Why the page refuses an oversized upload instead of letting the server say
 * no: the bytes ride as base64 inside ONE JSON-RPC frame, so a file over the
 * limit builds a frame over the socket's own ceiling, and the transport closes
 * the connection before `fs.upload` is ever called. That reached the reader as
 * a reconnect and took every other in-flight call on the socket down with it.
 * Refusing here turns it into a sentence about the file.
 */

import { t } from '../i18n/t'

export const UPLOAD_MAX_BYTES = 25 * 1024 * 1024

/* Two entries because the two callers hold the file in different forms, and
   asking for the wrong one costs the reader real time: a caller with the `File`
   answers before encoding it, which is what keeps a 500 MB drop from freezing
   the tab on a base64 it was always going to refuse. */
export function refusalBySize(name: string, bytes: number): string {
  if (bytes <= UPLOAD_MAX_BYTES) return ''
  const mb = (n: number): string => `${(n / 1048576).toFixed(1)} MB`
  return t('gui.att.too_big', { name, size: mb(bytes), limit: mb(UPLOAD_MAX_BYTES) })
}

/* The padding is subtracted rather than ignored: without it a file of exactly
   the limit decodes to two bytes over it, and is refused by a message quoting
   the same number on both sides of itself. */
export function refusal(name: string, b64: string): string {
  const text = String(b64 || '')
  const pad = text.endsWith('==') ? 2 : text.endsWith('=') ? 1 : 0
  return refusalBySize(name, Math.max(0, (text.length / 4) * 3 - pad))
}
