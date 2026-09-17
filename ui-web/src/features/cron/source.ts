/* -- schedules: the rpc source ---------------------------------------
   The cron island (ui-web/src/features/cron/) owns the renderer; this module
   only knows how to speak cron.* over /rpc and how to shape one job both
   ways. The live layer installs it onto the seam, which replaces the fixture
   source before the first paint. */

import type { CronJob, CronDraft, CronRun, CronSource } from './types'
import type { ParamsOf, ResultOf } from '../../rpc/generated'
import type { RailSource } from '../rail/types'

import { cronExprHuman } from './humanize'
import { islands } from '../../islands'
import { t } from '../../i18n/t'
import { ds } from '../../state/sources'
import { setCurrent as sessionSet } from '../../lib/session'
import { show as toast } from '../../state/toast'
import { gateway } from '../../state/gateway'

/** One job as `cron.list` sends it. */
export type CronJobWire = ResultOf<'cron.list'>['jobs'][number]
/** One recorded run as `cron.runs` sends it. */
export type CronRunWire = ResultOf<'cron.runs'>['runs'][number]
/** What `cron.save` takes: the draft, translated back into the contract. */
export type CronSaveParams = ParamsOf<'cron.save'>

const fmt2 = (n: number): string => String(n).padStart(2, '0')

/** An instant, worded against today. */
export function fmtStamp(ms: number | null | undefined): string {
  if (!ms) return '—'
  const d = new Date(ms), now = new Date()
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const hm = `${fmt2(d.getHours())}:${fmt2(d.getMinutes())}`
  if (d.getTime() >= day0 && d.getTime() < day0 + 86400000) return t('gui.time.today', { hm })
  if (d.getTime() >= day0 - 86400000 && d.getTime() < day0) return t('gui.time.yesterday', { hm })
  if (d.getTime() >= day0 + 86400000 && d.getTime() < day0 + 2 * 86400000) return t('gui.time.tomorrow', { hm })
  return t('gui.time.md_hm', { m: d.getMonth() + 1, d: d.getDate(), hm })
}

/** A span, in the largest whole unit it divides into. */
export function fmtEvery(ms: number): string {
  if (ms % 3600000 === 0) return t('gui.dur.h', { n: ms / 3600000 })
  if (ms % 60000 === 0) return t('gui.dur.m', { n: ms / 60000 })
  return t('gui.dur.s', { n: Math.round(ms / 1000) })
}

export function cronToRow(j: CronJobWire): CronJob {
  const when = j.kind === 'cron' ? cronExprHuman(j.expr as string)
    : j.kind === 'every' ? t('gui.cron.every', { every: fmtEvery(j.every_ms as number) })
    : t('gui.cron.once', { at: fmtStamp(j.at_ms) })
  const runs: CronRun[] = j.last_run_at_ms
    ? [{ at: fmtStamp(j.last_run_at_ms), ok: j.last_status !== 'error', ms: 0,
        note: j.last_error || (j.last_status === 'ok' ? t('gui.cron.ok') : j.last_status || ''), sid: null }]
    : []
  /* `at` is the server's third kind, and mapping it to 'day' is what let the
     editor rewrite a one-shot into a daily job. It has its own frequency now,
     and carries its instant in the shape the datetime input reads. */
  /* The offset of the instant being converted, not of today: `new Date()` with
     no argument is now, so a job on the other side of a DST boundary displayed
     -- and re-saved -- an hour off. Same shape as the bug above it, one layer
     down: a value re-derived through a conversion that does not know which
     instant it is converting. */
  const local = j.kind === 'at' && j.at_ms
    ? new Date(j.at_ms - new Date(j.at_ms).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ''
  return { id: j.id, name: j.name, on: j.enabled, what: j.message,
    freq: j.kind === 'cron' ? 'cron' : j.kind === 'every' ? 'hour' : 'once',
    at: j.kind === 'cron' ? (j.expr as string) : '', at_local: local,
    when, next: j.enabled ? fmtStamp(j.next_run_at_ms) : t('gui.cron.paused'),
    deliver: 'app', runs, kind: j.kind, every_ms: j.every_ms, at_ms: j.at_ms, tzv: j.tz }
}

export function jobToSave(j: CronDraft): CronSaveParams {
  const base = { name: j.name.trim(), message: j.what.trim() } as CronSaveParams
  if (j.id && !j.fresh) base.id = j.id
  if (j.freq === 'hour') {
    return { ...base, kind: 'every', every_seconds: j.every_ms ? Math.round(j.every_ms / 1000) : 3600 }
  }
  if (j.freq === 'cron') return { ...base, kind: 'cron', expr: j.at.trim() }
  if (j.freq === 'once') {
    if (!j.at_local) throw new Error('no instant')
    return { ...base, kind: 'at', at_iso: j.at_local }
  }
  /* A time the reader typed, and nothing else read back out of prose: the
     weekday is a number the control produced. */
  const hm = j.at.match(/^\s*(\d{1,2}):(\d{2})\s*$/)
  if (!hm) throw new Error('bad time')
  const [h, m] = [Number(hm[1]), Number(hm[2])]
  if (h > 23 || m > 59) throw new Error('bad time')
  if (j.freq === 'week') {
    const wd = Number(j.wd)
    if (!Number.isInteger(wd) || wd < 0 || wd > 6) throw new Error('bad weekday')
    return { ...base, kind: 'cron', expr: `${m} ${h} * * ${wd}` }
  }
  return { ...base, kind: 'cron', expr: `${m} ${h} * * *` }
}

export const cronSource: CronSource = {
  rows: () => gateway().call('cron.list', {}).then((r) => r.jobs.map(cronToRow)),
  toggle: (j) => gateway().call('cron.set_enabled', { id: j.id, enabled: !j.on })
    .then(() => toast(t(!j.on ? 'gui.cron.resumed_x' : 'gui.cron.paused_x', { name: j.name })))
    .catch((e) => toast(t('gui.op.action_failed', { detail: e.message || e }))),
  /* Toasted here, and still rejected: the caller's success branch closes the
     job's page, so resolving after a failed delete would bounce the reader
     back to a list where the row they just deleted is still there. */
  remove: (j) => gateway().call('cron.delete', { id: j.id })
    .then(() => toast(t('gui.cron.deleted_x', { name: j.name })))
    .catch((err) => {
      toast(t('gui.op.delete_failed', { detail: err.message || err }))
      throw { handled: true }
    }),
  /* `async` is load bearing, not decoration: `jobToSave` reports a bad draft by
     throwing, and a plain arrow would throw it before the caller's
     `.then(...).catch(...)` chain exists -- so the refusal never reaches
     `jobRefuse` and the reader gets a dead button instead of the note that
     says which field is wrong. An async function turns that into a rejection
     the existing catch already handles. */
  save: async (draft) => {
    const payload = jobToSave(draft)
    return gateway().call('cron.save', payload).then((r) => cronToRow(r.job)).catch((e) => {
      toast(t('gui.op.save_failed', { detail: (e.data && e.data.detail) || e.message || e }))
      throw { handled: true }
    })
  },
  runs: (j) => gateway().call('cron.runs', { id: j.id })
    .then((r) => (r.runs || []).map((x: CronRunWire) => ({
      at: x.at_ms ? fmtStamp(x.at_ms) : '—', ok: !!x.ok, note: x.preview || '',
    }))),
  runNow: (j) => gateway().call('cron.run_now', { id: j.id })
    .then(() => toast(t('gui.cron.triggered_x', { name: j.name })))
    .catch((e) => toast(t('gui.op.trigger_failed', { detail: e.message || e }))),
  openRun: async (j) => {
    islands.cron.close()
    const rail = ds<RailSource>('sessions')
    const s = { id: `cron:${j.id}`, title: j.name, last: '', when: '',
      at: Math.floor(Date.now() / 1000), run: null, live: true, from: 'cron' }
    const rows = rail.snapshot().rows
    if (!rows.find((row) => row.id === s.id)) rows.unshift(s)
    sessionSet(s.id)
    islands.rail.draw()
    rail.open(s)
  },
}
