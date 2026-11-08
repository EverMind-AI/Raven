/* How long ago something happened, in the largest unit that is not zero.
 *
 * One spelling for every row that carries a past instant -- a remembered test
 * verdict on an agent card, a turn on the transcript's nav -- so "5m ago" reads
 * the same wherever it is drawn. The words are the message catalogue's shared
 * `time` namespace; an instant of 0 or null is one nothing measured, and says nothing.
 *
 * `since` climbs the same ladder as a span rather than a distance back -- "2d",
 * not "2d ago" -- for a line that names a state first and then how long it has
 * held ("Idle 2d").
 */
import { t } from '../i18n/t'

interface Rungs {
  readonly now: string
  readonly m: string
  readonly h: string
  readonly d: string
}

const AGO: Rungs = { now: 'gui.time.ago_now', m: 'gui.time.ago_m', h: 'gui.time.ago_h', d: 'gui.time.ago_d' }
const SPAN: Rungs = { now: 'gui.time.for_now', m: 'gui.time.for_m', h: 'gui.time.for_h', d: 'gui.time.for_d' }

function ladder(atMs: number | null, now: number, words: Rungs): string {
  if (!atMs) return ''
  const m = Math.floor((now - atMs) / 60_000)
  if (m < 1) return t(words.now)
  if (m < 60) return t(words.m, { n: m })
  const h = Math.floor(m / 60)
  if (h < 24) return t(words.h, { n: h })
  return t(words.d, { n: Math.floor(h / 24) })
}

export function ago(atMs: number | null, now = Date.now()): string {
  return ladder(atMs, now, AGO)
}

export function since(atMs: number | null, now = Date.now()): string {
  return ladder(atMs, now, SPAN)
}
