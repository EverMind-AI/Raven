/* How long ago something happened, in the largest unit that is not zero.
 *
 * One spelling for every row that carries a past instant -- a remembered test
 * verdict on an agent card, a turn on the transcript's nav -- so "5m ago" reads
 * the same wherever it is drawn. The words are the catalogue's shared `time`
 * namespace; an instant of 0 or null is one nothing measured, and says nothing.
 */
import { t } from '../i18n/t'

export function ago(atMs: number | null, now = Date.now()): string {
  if (!atMs) return ''
  const m = Math.floor((now - atMs) / 60_000)
  if (m < 1) return t('gui.time.ago_now')
  if (m < 60) return t('gui.time.ago_m', { n: m })
  const h = Math.floor(m / 60)
  if (h < 24) return t('gui.time.ago_h', { n: h })
  return t('gui.time.ago_d', { n: Math.floor(h / 24) })
}
