// @vitest-environment happy-dom
/* The two mappings a scheduled job crosses: the contract's row into what the
 * page draws, and the page's draft back into what `cron.save` takes. Both
 * carry a rule that a reading of the code alone does not make obvious.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { cronToRow, fmtEvery, fmtStamp, jobToSave } from './source'

import type { CronJobWire } from './source'
import type { CronDraft } from './types'

/* A catalogue that echoes what it was asked for, so an assertion names the key
   and its variables rather than one language's wording. */
setTranslator((key: string, vars?: Record<string, unknown> | null) =>
(vars ? `${key}(${Object.entries(vars).map(([k, v]) => `${k}=${v}`).join(',')})` : key))
vi.spyOn(pageStore, 'show').mockImplementation(() => {})
vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})

const job = (over: Partial<CronJobWire>): CronJobWire => ({
  id: 'j1', name: 'nightly', message: 'summarise', enabled: true, kind: 'cron', expr: '0 9 * * *',
  ...over,
} as CronJobWire)

const draft = (over: Partial<CronDraft>): CronDraft => ({
  id: 'j1', name: ' nightly ', what: ' summarise ', freq: 'day', at: '09:30', on: true,
  deliver: 'app', when: '', next: '', runs: [],
  ...over,
} as CronDraft)

beforeEach(() => {
})

describe('one job, contract shape to page shape', () => {
  /* Every schedule but an interval and a one-shot is stored as a cron job, so
     a row read back as `cron` was a weekly job reopening as a raw expression
     in a box nobody asked for -- and saving from there rewrote it as custom.
     The three shapes the editor writes are the three read back. */
  it('reads a daily expression back as a daily job', () => {
    const row = cronToRow(job({ kind: 'cron', expr: '0 9 * * *' }))
    expect(row.freq).toBe('day')
    expect(row.at).toBe('09:00')
    expect(row.when).not.toBe('')
  })

  it('reads a weekly expression back as a weekly job, with its weekday', () => {
    const row = cronToRow(job({ kind: 'cron', expr: '30 9 * * 3' }))
    expect(row.freq).toBe('week')
    expect(row.at).toBe('09:30')
    expect(row.wd).toBe(3)
  })

  it('reads a monthly expression back as a monthly job, with its day', () => {
    const row = cronToRow(job({ kind: 'cron', expr: '5 8 15 * *' }))
    expect(row.freq).toBe('month')
    expect(row.at).toBe('08:05')
    expect(row.dom).toBe(15)
    /* And words it, which is the row's own second line: the reader of a list
       should never be shown five fields. */
    expect(row.when).toBe('gui.cron.h.monthly(d=15,hm=08:05)')
  })

  /* And anything the editor cannot write stays custom, with the expression
     itself in the box. */
  it('leaves an expression the editor cannot write as a custom one', () => {
    const row = cronToRow(job({ kind: 'cron', expr: '*/10 * * * 1-5' }))
    expect(row.freq).toBe('cron')
    expect(row.at).toBe('*/10 * * * 1-5')
  })

  it('words an interval job in the largest whole unit', () => {
    expect(fmtEvery(3 * 3600000)).toBe('gui.dur.h(n=3)')
    expect(fmtEvery(90 * 60000)).toBe('gui.dur.m(n=90)')
    expect(fmtEvery(45000)).toBe('gui.dur.s(n=45)')
    expect(cronToRow(job({ kind: 'every', expr: undefined, every_ms: 3600000 })).freq).toBe('hour')
  })

  /* `at` is the server's third kind. Mapping it to 'day' is what let the editor
     rewrite a one-shot into a daily job, so it has its own frequency. */
  it('keeps a one-shot a one-shot', () => {
    const row = cronToRow(job({ kind: 'at', expr: undefined, at_ms: Date.UTC(2026, 6, 1, 12, 0) }))
    expect(row.freq).toBe('once')
    expect(row.at).toBe('')
  })

  /* The offset of the instant being converted, not of today: a job on the other
     side of a DST boundary displayed -- and re-saved -- an hour off. The
     round trip is the invariant, and it holds in every zone; two instants six
     months apart is what makes a zone that observes DST exercise it. */
  it('carries a one-shot instant across a DST boundary', () => {
    for (const at of [Date.UTC(2026, 0, 15, 12, 0), Date.UTC(2026, 6, 15, 12, 0)]) {
      const row = cronToRow(job({ kind: 'at', expr: undefined, at_ms: at }))
      expect(new Date(row.at_local as string).getTime()).toBe(at)
    }
  })

  it('reports the last run as one row, and nothing when there was none', () => {
    expect(cronToRow(job({})).runs).toEqual([])
    const [run] = cronToRow(job({ last_run_at_ms: Date.now(), last_status: 'ok' })).runs
    expect(run?.ok).toBe(true)
    expect(run?.note).toBe('gui.cron.ok')
  })

  it('carries the error text of a failed run, in place of the status word', () => {
    const [run] = cronToRow(job({ last_run_at_ms: Date.now(), last_status: 'error', last_error: 'exit 1' })).runs
    expect(run?.ok).toBe(false)
    expect(run?.note).toBe('exit 1')
  })

  it('says paused instead of a next time when the job is off', () => {
    expect(cronToRow(job({ enabled: false, next_run_at_ms: Date.now() })).next).toBe('gui.cron.paused')
  })

  it('words an instant that is not there as a dash', () => {
    expect(fmtStamp(0)).toBe('—')
    expect(fmtStamp(null)).toBe('—')
  })
})

describe('one draft, page shape back to contract shape', () => {
  it('trims the two free-text fields', () => {
    expect(jobToSave(draft({}))).toMatchObject({ name: 'nightly', message: 'summarise' })
  })

  /* A fresh job carries no id: sending one would ask the server to rewrite a
     row that does not exist yet. */
  it('sends the id only for a job that already exists', () => {
    expect(jobToSave(draft({}))).toHaveProperty('id', 'j1')
    expect(jobToSave(draft({ fresh: true }))).not.toHaveProperty('id')
  })

  it('turns a daily draft into a cron expression', () => {
    expect(jobToSave(draft({ freq: 'day', at: '09:30' }))).toMatchObject({ kind: 'cron', expr: '30 9 * * *' })
  })

  it('turns a weekly draft into a cron expression with its weekday', () => {
    expect(jobToSave(draft({ freq: 'week', at: '7:05', wd: 3 }))).toMatchObject({ kind: 'cron', expr: '5 7 * * 3' })
  })

  it('turns a monthly draft into a cron expression with its day', () => {
    expect(jobToSave(draft({ freq: 'month', at: '8:05', dom: 15 })))
      .toMatchObject({ kind: 'cron', expr: '5 8 15 * *' })
  })

  it('sends an interval in whole seconds, and an hour when there is none', () => {
    expect(jobToSave(draft({ freq: 'hour', every_ms: 90000 }))).toMatchObject({ kind: 'every', every_seconds: 90 })
    expect(jobToSave(draft({ freq: 'hour' }))).toMatchObject({ kind: 'every', every_seconds: 3600 })
  })

  it('sends a one-shot as the instant the control produced', () => {
    expect(jobToSave(draft({ freq: 'once', at_local: '2026-07-01T12:00' })))
      .toMatchObject({ kind: 'at', at_iso: '2026-07-01T12:00' })
  })

  /* Refused by throwing, which is what the source turns into a rejection the
     island's catch already handles -- a dead button otherwise. */
  it('refuses a draft it cannot express', () => {
    expect(() => jobToSave(draft({ freq: 'once', at_local: '' }))).toThrow('no instant')
    expect(() => jobToSave(draft({ freq: 'day', at: 'noon' }))).toThrow('bad time')
    expect(() => jobToSave(draft({ freq: 'day', at: '25:00' }))).toThrow('bad time')
    expect(() => jobToSave(draft({ freq: 'day', at: '09:61' }))).toThrow('bad time')
    expect(() => jobToSave(draft({ freq: 'week', at: '09:30', wd: 9 }))).toThrow('bad weekday')
    expect(() => jobToSave(draft({ freq: 'month', at: '09:30', dom: 0 }))).toThrow('bad dom')
    expect(() => jobToSave(draft({ freq: 'month', at: '09:30', dom: 32 }))).toThrow('bad dom')
  })
})
