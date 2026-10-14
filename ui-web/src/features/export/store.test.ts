// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { RpcError } from '../../rpc/transport'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { ExportSource, ExportStatus, ExportTurn, ExportTurns } from './types'

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (err: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((ok, no) => { resolve = ok; reject = no })
  return { promise, resolve, reject }
}

const turn = (n: number, inProgress = false): ExportTurn => ({
  trace_id: `t${n}`, number: n, start: '2026-10-01T10:00:00Z', input_preview: `input ${n}`, in_progress: inProgress,
})

const status = (state: ExportStatus['state'], jobId = 'exj-1'): ExportStatus => ({
  job_id: jobId,
  state,
  phase: state === 'done' ? 'done' : 'collecting',
  file_name: 'a.tar.gz',
  download_url: state === 'done' ? '/trajectory/export/download?token=x' : null,
  size_bytes: state === 'done' ? 2048 : null,
  counts: state === 'done' ? { spans: 3, artifacts: 1, messages: 2 } : null,
  error: null,
})

let calls: Array<[string, ...unknown[]]>
let onTurns: () => Promise<ExportTurns>
let onStart: () => Promise<{ job_id: string }>
let onStatus: (jobId: string) => Promise<ExportStatus>

const fake: ExportSource = {
  turns: (key) => { calls.push(['turns', key]); return onTurns() },
  start: (key, ids, name) => { calls.push(['start', key, ids, name]); return onStart() },
  status: (jobId) => { calls.push(['status', jobId]); return onStatus(jobId) },
  cancel: (jobId) => { calls.push(['cancel', jobId]); return Promise.resolve({ ok: true }) },
}

const of = (method: string): Array<[string, ...unknown[]]> => calls.filter((call) => call[0] === method)
const settle = (ms = 0): Promise<void> => vi.advanceTimersByTimeAsync(ms).then(() => undefined)

async function openOn(turns: ExportTurn[], title = 'Plan'): Promise<void> {
  onTurns = () => Promise.resolve({ title, turns })
  store.open('export', 'gui:a')
  await settle()
}

/* Open on three finished turns and step to the export page. */
async function atExportPage(): Promise<void> {
  await openOn([turn(1), turn(2), turn(3)])
  store.next()
}

beforeEach(() => {
  vi.useFakeTimers()
  calls = []
  onStart = () => Promise.resolve({ job_id: 'exj-1' })
  onStatus = (jobId) => Promise.resolve(status('running', jobId))
  setSources({ export: fake })
  setTranslator((key) => key)
})

afterEach(() => {
  store._resetForTests()
  confirm._resetForTests()
  resetSources()
  resetTranslator()
  vi.useRealTimers()
})

describe('picking the turns', () => {
  it('opens with every finished turn chosen and the running one left out', async () => {
    vi.setSystemTime(new Date(2026, 9, 14, 9, 5))
    await openOn([turn(1), turn(2), turn(3, true)])
    const s = store.get()
    expect(of('turns')).toEqual([['turns', 'gui:a']])
    expect(s.selected).toEqual(['t1', 't2'])
    expect(s.pages).toEqual(['turns', 'export'])
    expect(s.page).toBe('turns')
    expect(s.fileName).toBe('Plan-20261014-0905.tar.gz')
  })

  it('skips the picker for a conversation of one finished turn', async () => {
    await openOn([turn(1)])
    expect(store.get().pages).toEqual(['export'])
    expect(store.get().page).toBe('export')
  })

  it('keeps the picker, and moves on from it never, when the one turn is still running', async () => {
    await openOn([turn(1, true)])
    expect(store.get().page).toBe('turns')
    expect(store.get().selected).toEqual([])
    store.next()
    expect(store.get().page).toBe('turns')
  })

  it('reads the select-all box as all, some or none of the finished turns', async () => {
    await openOn([turn(1), turn(2), turn(3, true)])
    expect(store.allState()).toBe('all')
    store.toggleAll()
    expect(store.allState()).toBe('none')
    store.toggle('t2')
    expect(store.allState()).toBe('some')
    store.toggle('t3')
    expect(store.get().selected).toEqual(['t2'])
    store.toggleAll()
    expect(store.get().selected).toEqual(['t1', 't2'])
  })

  it('drops a listing that lands after its wizard closed', async () => {
    const late = deferred<ExportTurns>()
    onTurns = () => late.promise
    store.open('export', 'gui:a')
    store.close()
    late.resolve({ title: 'Plan', turns: [turn(1)] })
    await settle()
    expect(store.get().open).toBe(false)
    expect(store.get().turns).toBeNull()
  })

  it('names a listing that failed and lists again on request', async () => {
    onTurns = () => Promise.reject(new Error('offline'))
    store.open('export', 'gui:a')
    await settle()
    expect(store.get().error).toEqual({ kind: 'load', detail: 'offline' })
    onTurns = () => Promise.resolve({ title: 'Plan', turns: [turn(1), turn(2)] })
    store.retryTurns()
    await settle()
    expect(store.get().error).toBeNull()
    expect(store.get().selected).toEqual(['t1', 't2'])
  })

  it('suggests a file name without what a file system refuses', () => {
    const now = new Date(2026, 0, 2, 3, 4)
    expect(store.defaultFileName('a/b: "c"?', now)).toBe('ab c-20260102-0304.tar.gz')
    expect(store.defaultFileName('  ', now)).toBe('raven-export-20260102-0304.tar.gz')
  })

  it('offers the export only with a conversation and the view offered', () => {
    expect(store.exportable('gui:a', true)).toBe(true)
    expect(store.exportable(null, true)).toBe(false)
    expect(store.exportable('gui:a', false)).toBe(false)
  })
})

describe('starting the job', () => {
  it('starts one job however often it is pressed', async () => {
    await atExportPage()
    const pending = deferred<{ job_id: string }>()
    onStart = () => pending.promise
    void store.start('a.tar.gz')
    void store.start('a.tar.gz')
    expect(of('start')).toEqual([['start', 'gui:a', ['t1', 't2', 't3'], 'a.tar.gz']])
    expect(store.working()).toBe(true)
    pending.resolve({ job_id: 'exj-1' })
    await settle()
    void store.start('a.tar.gz')
    expect(of('start')).toHaveLength(1)
  })

  it('cancels a job whose start lands after its wizard was closed, and leaves the new wizard alone', async () => {
    await atExportPage()
    const pending = deferred<{ job_id: string }>()
    onStart = () => pending.promise
    void store.start('a.tar.gz')
    store.dismiss()
    expect(confirm.get().open).toBe(true)
    confirm.answer(true)
    expect(store.get().open).toBe(false)
    await openOn([turn(1), turn(2)])
    const fresh = store.get()
    pending.resolve({ job_id: 'exj-old' })
    await settle(10_000)
    expect(of('cancel')).toEqual([['cancel', 'exj-old']])
    expect(store.get()).toEqual(fresh)
    expect(of('status')).toEqual([])
  })

  it('goes back to the picker on a refused selection, marks the turns and keeps the rest', async () => {
    await atExportPage()
    onStart = () => Promise.reject(new RpcError(-32602, 'refused', {
      reason: 'invalid_selection', invalid: [{ trace_id: 't2', why: 'in_progress' }],
    }))
    onTurns = () => Promise.resolve({ title: 'Plan', turns: [turn(1), turn(2, true), turn(3)] })
    await store.start('a.tar.gz')
    await settle()
    const s = store.get()
    expect(s.page).toBe('turns')
    expect(s.invalid).toEqual({ t2: 'in_progress' })
    expect(s.selected).toEqual(['t1', 't3'])
    expect(s.error).toEqual({ kind: 'changed' })
    expect(s.starting).toBe(false)
    expect(of('turns')).toHaveLength(2)
  })

  it('shows the picker after a refusal even when it was skipped', async () => {
    await openOn([turn(1)])
    onStart = () => Promise.reject(new RpcError(-32602, 'refused', {
      reason: 'invalid_selection', invalid: [{ trace_id: 't1', why: 'not_in_session' }],
    }))
    onTurns = () => Promise.resolve({ title: 'Plan', turns: [] })
    await store.start('a.tar.gz')
    await settle()
    expect(store.get().pages).toEqual(['turns', 'export'])
    expect(store.get().page).toBe('turns')
    expect(store.get().selected).toEqual([])
  })

  it('names the limit and the policy, and lets the export be tried again', async () => {
    await atExportPage()
    onStart = () => Promise.reject(new RpcError(-32025, 'busy', { limit: 2 }))
    await store.start('a.tar.gz')
    expect(store.get().error).toEqual({ kind: 'limit' })
    expect(store.working()).toBe(false)
    onStart = () => Promise.reject(new RpcError(-32020, 'off'))
    await store.start('a.tar.gz')
    expect(store.get().error).toEqual({ kind: 'disabled' })
    expect(of('start')).toHaveLength(2)
  })
})

describe('following the job', () => {
  it('asks every 700 ms until the job settles, then stops', async () => {
    await atExportPage()
    await store.start('a.tar.gz')
    await settle()
    expect(of('status')).toHaveLength(1)
    await settle(store.POLL_MS)
    expect(of('status')).toHaveLength(2)
    onStatus = () => Promise.resolve(status('done'))
    await settle(store.POLL_MS)
    expect(store.get().job?.status?.state).toBe('done')
    await settle(10_000)
    expect(of('status')).toHaveLength(3)
  })

  it('keeps the same job through a failed query and picks it up again', async () => {
    await atExportPage()
    onStatus = () => Promise.reject(new Error('socket closed'))
    await store.start('a.tar.gz')
    await settle()
    expect(store.get().job).toMatchObject({ id: 'exj-1', lost: 'socket closed' })
    expect(store.working()).toBe(true)
    onStatus = () => Promise.resolve(status('done'))
    await settle(store.RETRY_MS)
    expect(store.get().job).toMatchObject({ id: 'exj-1', lost: null })
    expect(store.get().job?.status?.state).toBe('done')
    expect(of('start')).toHaveLength(1)
    expect(of('status').every((call) => call[1] === 'exj-1')).toBe(true)
  })

  it('checks now on request, with one query in flight at a time', async () => {
    await atExportPage()
    onStatus = () => Promise.reject(new Error('socket closed'))
    await store.start('a.tar.gz')
    await settle()
    const pending = deferred<ExportStatus>()
    onStatus = () => pending.promise
    store.requery()
    store.requery()
    expect(of('status')).toHaveLength(2)
    await settle(store.RETRY_MS * 2)
    expect(of('status')).toHaveLength(2)
    pending.resolve(status('done'))
    await settle()
    expect(store.get().job?.status?.state).toBe('done')
  })

  it('lets a job the gateway no longer has go, and starts a new one on request', async () => {
    await atExportPage()
    onStatus = () => Promise.reject(new RpcError(-32024, 'gone', { id: 'exj-1' }))
    await store.start('a.tar.gz')
    await settle()
    expect(store.get().job).toBeNull()
    expect(store.get().error).toEqual({ kind: 'gone' })
    onStart = () => Promise.resolve({ job_id: 'exj-2' })
    onStatus = (jobId) => Promise.resolve(status('running', jobId))
    await store.start('a.tar.gz')
    expect(store.get().job?.id).toBe('exj-2')
    expect(of('start')).toHaveLength(2)
  })

  it('writes nothing from a status that lands after the wizard closed', async () => {
    await atExportPage()
    const pending = deferred<ExportStatus>()
    onStatus = () => pending.promise
    await store.start('a.tar.gz')
    store.close()
    pending.resolve(status('done'))
    await settle(10_000)
    expect(store.get().open).toBe(false)
    expect(store.get().job).toBeNull()
    expect(of('status')).toHaveLength(1)
  })
})

describe('closing', () => {
  it('cancels a running job and leaves a finished one alone', async () => {
    await atExportPage()
    await store.start('a.tar.gz')
    await settle()
    store.close()
    expect(of('cancel')).toEqual([['cancel', 'exj-1']])
    await atExportPage()
    onStatus = () => Promise.resolve(status('done'))
    await store.start('a.tar.gz')
    await settle()
    store.close()
    expect(of('cancel')).toHaveLength(1)
  })

  it('asks before stopping work, and stops only on yes', async () => {
    await atExportPage()
    await store.start('a.tar.gz')
    await settle()
    store.dismiss()
    expect(confirm.get().open).toBe(true)
    confirm.answer(false)
    expect(store.get().open).toBe(true)
    expect(of('cancel')).toEqual([])
    store.dismiss()
    confirm.answer(true)
    expect(store.get().open).toBe(false)
    expect(of('cancel')).toEqual([['cancel', 'exj-1']])
  })

  it('closes at once with nothing under way', async () => {
    await atExportPage()
    store.dismiss()
    expect(confirm.get().open).toBe(false)
    expect(store.get().open).toBe(false)
  })

  it('lets an old question close only the wizard it was asked about', async () => {
    await atExportPage()
    await store.start('a.tar.gz')
    await settle()
    store.dismiss()
    store.close()
    await openOn([turn(1), turn(2)])
    confirm.answer(true)
    expect(store.get().open).toBe(true)
  })
})
