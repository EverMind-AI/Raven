// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { RpcError } from '../../rpc/transport'
import * as confirm from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { ExportSource, ExportStatus, ExportTurn, ExportTurns, ReportContexts, ReportItem, ReportStatus } from './types'

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

const item = (id: string, kind: ReportItem['kind'], linked: string[] = [], hint: ReportItem['hint'] = null): ReportItem => ({
  id, kind, category: 'high-entropy', masked_sample: `${id}...`, count: 1, sources: [], linked, hint,
})

/* a and b are linked, b and c are linked: one group of three. d stands alone,
   k is a confirmed secret with a hint the page must not apply. */
const ITEMS: ReportItem[] = [
  item('k', 'confirmed'),
  item('a', 'suspected', ['b']),
  item('b', 'suspected', ['a', 'c']),
  item('c', 'suspected', ['b'], 'redacted'),
  item('d', 'suspected'),
]

const rstatus = (phase: ReportStatus['phase'], patch: Partial<ReportStatus> = {}): ReportStatus => ({
  report_id: 'rs-1',
  bug_report_id: 'br-1',
  phase,
  error: null,
  history: { state: 'on', auto_kept: 1, auto_redacted: 0, warning: null },
  items: phase === 'deciding' ? ITEMS : null,
  summary: ['ready', 'uploading', 'uploaded', 'upload_failed'].includes(phase)
    ? { classification: 'needs_review', warnings: [], size_bytes: null, auto_replaced: 2, kept: 3, redacted: 1, acknowledged: 1, completeness: 'complete' }
    : null,
  receipt: phase === 'uploaded' ? { receipt_id: 'rcpt-1', duplicate: false } : null,
  record_dir: null,
  ...patch,
})

let calls: Array<[string, ...unknown[]]>
let onTurns: () => Promise<ExportTurns>
let onStart: () => Promise<{ job_id: string }>
let onStatus: (jobId: string) => Promise<ExportStatus>
let onReportStart: () => Promise<{ report_id: string }>
let onReportStatus: (reportId: string) => Promise<ReportStatus>
let onReportItem: (reportId: string, itemId: string) => Promise<ReportContexts>
let onReportAct: (step: 'decide' | 'upload' | 'retry') => Promise<{ ok: boolean }>

const fake: ExportSource = {
  turns: (key) => { calls.push(['turns', key]); return onTurns() },
  start: (key, ids, name) => { calls.push(['start', key, ids, name]); return onStart() },
  status: (jobId) => { calls.push(['status', jobId]); return onStatus(jobId) },
  cancel: (jobId) => { calls.push(['cancel', jobId]); return Promise.resolve({ ok: true }) },
  reportStart: (key, ids, problem) => { calls.push(['reportStart', key, ids, problem]); return onReportStart() },
  reportStatus: (reportId) => { calls.push(['reportStatus', reportId]); return onReportStatus(reportId) },
  reportItem: (reportId, itemId) => { calls.push(['reportItem', reportId, itemId]); return onReportItem(reportId, itemId) },
  reportDecide: (reportId, decisions, acknowledged) => {
    calls.push(['reportDecide', reportId, decisions, acknowledged])
    return onReportAct('decide')
  },
  reportUpload: (reportId, risk) => { calls.push(['reportUpload', reportId, risk]); return onReportAct('upload') },
  reportRetry: (reportId) => { calls.push(['reportRetry', reportId]); return onReportAct('retry') },
  reportCancel: (reportId) => { calls.push(['reportCancel', reportId]); return Promise.resolve({ ok: true }) },
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
  onReportStart = () => Promise.resolve({ report_id: 'rs-1' })
  onReportStatus = () => Promise.resolve(rstatus('collecting'))
  onReportItem = (_id, itemId) => Promise.resolve({ groups: [{ label: 'a tool result', text: itemId, spans: [[0, 1]], count: 1 }], total_groups: 1 })
  onReportAct = () => Promise.resolve({ ok: true })
  setSources({ export: fake })
  setTranslator((key) => key)
})

afterEach(() => {
  localStorage.clear()
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
    expect(store.exportable('imported:' + 'a'.repeat(32), true)).toBe(false)
  })
})

describe('starting the job', () => {
  it('starts one job however often it is pressed', async () => {
    await atExportPage()
    const pending = deferred<{ job_id: string }>()
    onStart = () => pending.promise
    store.editName('a.tar.gz')
    void store.start()
    void store.start()
    expect(of('start')).toEqual([['start', 'gui:a', ['t1', 't2', 't3'], 'a.tar.gz']])
    expect(store.working()).toBe(true)
    pending.resolve({ job_id: 'exj-1' })
    await settle()
    void store.start()
    expect(of('start')).toHaveLength(1)
  })

  it('cancels a job whose start lands after its wizard was closed, and leaves the new wizard alone', async () => {
    await atExportPage()
    const pending = deferred<{ job_id: string }>()
    onStart = () => pending.promise
    void store.start()
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
    await store.start()
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
    await store.start()
    await settle()
    expect(store.get().pages).toEqual(['turns', 'export'])
    expect(store.get().page).toBe('turns')
    expect(store.get().selected).toEqual([])
  })

  it('names the limit and the policy, and lets the export be tried again', async () => {
    await atExportPage()
    onStart = () => Promise.reject(new RpcError(-32025, 'busy', { limit: 2 }))
    await store.start()
    expect(store.get().error).toEqual({ kind: 'limit' })
    expect(store.working()).toBe(false)
    onStart = () => Promise.reject(new RpcError(-32020, 'off'))
    await store.start()
    expect(store.get().error).toEqual({ kind: 'disabled' })
    expect(of('start')).toHaveLength(2)
  })
})

describe('following the job', () => {
  it('asks every 700 ms until the job settles, then stops', async () => {
    await atExportPage()
    await store.start()
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
    await store.start()
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
    await store.start()
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
    await store.start()
    await settle()
    expect(store.get().job).toBeNull()
    expect(store.get().error).toEqual({ kind: 'gone' })
    onStart = () => Promise.resolve({ job_id: 'exj-2' })
    onStatus = (jobId) => Promise.resolve(status('running', jobId))
    await store.start()
    expect(store.get().job?.id).toBe('exj-2')
    expect(of('start')).toHaveLength(2)
  })

  it('writes nothing from a status that lands after the wizard closed', async () => {
    await atExportPage()
    const pending = deferred<ExportStatus>()
    onStatus = () => pending.promise
    await store.start()
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
    await store.start()
    await settle()
    store.close()
    expect(of('cancel')).toEqual([['cancel', 'exj-1']])
    await atExportPage()
    onStatus = () => Promise.resolve(status('done'))
    await store.start()
    await settle()
    store.close()
    expect(of('cancel')).toHaveLength(1)
  })

  it('asks before stopping work, and stops only on yes', async () => {
    await atExportPage()
    await store.start()
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
    await store.start()
    await settle()
    store.dismiss()
    store.close()
    await openOn([turn(1), turn(2)])
    confirm.answer(true)
    expect(store.get().open).toBe(true)
  })
})

/* Open the bug report on three finished turns, step to the problem page and
   describe it. */
async function atProblemPage(): Promise<void> {
  onTurns = () => Promise.resolve({ title: 'Plan', turns: [turn(1), turn(2), turn(3)] })
  store.open('report', 'gui:a')
  await settle()
  store.next()
  store.editProblem('description', 'it broke')
}

/* Filed and waiting for decisions, with the first item's contexts in. */
async function atDecisions(): Promise<void> {
  await atProblemPage()
  onReportStatus = () => Promise.resolve(rstatus('deciding'))
  await store.startReport()
  await settle()
}

async function atUpload(phase: ReportStatus['phase'] = 'ready', patch: Partial<ReportStatus> = {}): Promise<void> {
  await atProblemPage()
  onReportStatus = () => Promise.resolve(rstatus(phase, patch))
  await store.startReport()
  await settle()
}

describe('the bug report flow', () => {
  it('pages through the picker, the problem, the decisions and the upload', async () => {
    await atProblemPage()
    expect(store.get().pages).toEqual(['turns', 'problem', 'decide', 'upload'])
    expect(store.get().page).toBe('problem')
    store.back()
    expect(store.get().page).toBe('turns')
  })

  it('leaves the picker out for one finished turn', async () => {
    onTurns = () => Promise.resolve({ title: 'Plan', turns: [turn(1)] })
    store.open('report', 'gui:a')
    await settle()
    expect(store.get().pages).toEqual(['problem', 'decide', 'upload'])
  })

  it('needs a description, keeps the draft through a step back and sends it', async () => {
    onTurns = () => Promise.resolve({ title: 'Plan', turns: [turn(1), turn(2)] })
    store.open('report', 'gui:a')
    await settle()
    store.next()
    await store.startReport()
    expect(of('reportStart')).toEqual([])
    store.editProblem('description', 'it broke')
    store.editProblem('severity', 'high')
    store.editProblem('reporter', 'sam')
    expect(store.get().problemReady).toBe(true)
    store.back()
    store.next()
    expect(store.problemDraft()).toMatchObject({ description: 'it broke', severity: 'high', reporter: 'sam' })
    await store.startReport()
    expect(of('reportStart')).toEqual([['reportStart', 'gui:a', ['t1', 't2'], {
      description: 'it broke', severity: 'high', expected: '', actual: '', steps: '', reporter: 'sam',
    }]])
    expect(localStorage.getItem(store.REPORTER_KEY)).toBe('sam')
  })

  it('remembers the reporter, and files the report even where storage throws', async () => {
    localStorage.setItem(store.REPORTER_KEY, 'kim')
    await atProblemPage()
    expect(store.problemDraft().reporter).toBe('kim')
    /* A private window: reaching for the storage at all throws. */
    const denied = vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('denied') })
    store.close()
    await atProblemPage()
    expect(store.problemDraft().reporter).toBe('')
    await store.startReport()
    expect(of('reportStart')).toHaveLength(1)
    denied.mockRestore()
  })

  it('files one report however often it is pressed', async () => {
    await atProblemPage()
    const pending = deferred<{ report_id: string }>()
    onReportStart = () => pending.promise
    void store.startReport()
    void store.startReport()
    expect(of('reportStart')).toHaveLength(1)
  })

  it('cancels a report whose start lands after its wizard was closed', async () => {
    await atProblemPage()
    const pending = deferred<{ report_id: string }>()
    onReportStart = () => pending.promise
    void store.startReport()
    store.dismiss()
    confirm.answer(true)
    await atProblemPage()
    const fresh = store.get()
    pending.resolve({ report_id: 'rs-old' })
    await settle(10_000)
    expect(of('reportCancel')).toEqual([['reportCancel', 'rs-old']])
    expect(store.get()).toEqual(fresh)
    expect(of('reportStatus')).toEqual([])
  })

  it('polls while collecting and sets the decisions up from the hints', async () => {
    await atProblemPage()
    await store.startReport()
    await settle()
    expect(store.get().page).toBe('problem')
    onReportStatus = () => Promise.resolve(rstatus('deciding'))
    await settle(store.POLL_MS)
    const s = store.get()
    expect(s.page).toBe('decide')
    expect(s.choices).toEqual({ a: 'kept', b: 'kept', c: 'redacted', d: 'kept' })
    expect(s.acknowledged).toEqual([])
    expect(s.focus).toBe('k')
    expect(of('reportItem')).toEqual([['reportItem', 'rs-1', 'k']])
    await settle(10_000)
    expect(of('reportStatus')).toHaveLength(2)
  })

  it('marks the decisions skipped when nothing was left to decide', async () => {
    await atUpload('ready')
    expect(store.get().page).toBe('upload')
    expect(store.get().skipped).toEqual(['decide'])
  })

  it('decides every suspected item, or the focused one with its whole linked group', async () => {
    await atDecisions()
    store.choose('redacted', 'all')
    expect(store.get().choices).toEqual({ a: 'redacted', b: 'redacted', c: 'redacted', d: 'redacted' })
    store.focusItem('a')
    store.choose('kept', 'focus')
    expect(store.get().choices).toEqual({ a: 'kept', b: 'kept', c: 'kept', d: 'redacted' })
    store.focusItem('k')
    store.choose('redacted', 'focus')
    expect(store.get().choices).toEqual({ a: 'kept', b: 'kept', c: 'kept', d: 'redacted' })
  })

  it('asks for an item\'s contexts once, and drops one that lands after a close', async () => {
    await atDecisions()
    store.focusItem('a')
    store.focusItem('a')
    await settle()
    expect(of('reportItem').filter((call) => call[2] === 'a')).toHaveLength(1)
    expect(store.get().contexts.a).toMatchObject({ state: 'ready' })
    const late = deferred<ReportContexts>()
    onReportItem = () => late.promise
    store.focusItem('d')
    store.close()
    late.resolve({ groups: [], total_groups: 0 })
    await settle()
    expect(store.get().contexts).toEqual({})
  })

  it('sends every decision with the acknowledgements, and only once every secret is acknowledged', async () => {
    await atDecisions()
    await store.decide()
    expect(of('reportDecide')).toEqual([])
    store.acknowledge('k', true)
    onReportStatus = () => Promise.resolve(rstatus('ready'))
    await store.decide()
    await settle()
    expect(of('reportDecide')).toEqual([['reportDecide', 'rs-1', [
      { item_id: 'a', action: 'kept' },
      { item_id: 'b', action: 'kept' },
      { item_id: 'c', action: 'redacted' },
      { item_id: 'd', action: 'kept' },
    ], ['k']]])
    expect(store.get().page).toBe('upload')
    expect(store.get().skipped).toEqual([])
  })

  it('marks a conflict, clears it on the next choice, and names the unacknowledged', async () => {
    await atDecisions()
    store.acknowledge('k', true)
    onReportAct = () => Promise.reject(new RpcError(-32027, 'clash', { item_ids: ['a', 'b'] }))
    await store.decide()
    expect(store.get().marked).toEqual(['a', 'b'])
    expect(store.get().error).toEqual({ kind: 'conflict' })
    expect(store.get().acting).toBe(false)
    store.choose('kept', 'all')
    expect(store.get().marked).toEqual([])
    onReportAct = () => Promise.reject(new RpcError(-32028, 'ack', { phase: 'deciding', reason: 'not_acknowledged', item_ids: ['k'] }))
    await store.decide()
    expect(store.get().marked).toEqual(['k'])
    expect(store.get().error).toEqual({ kind: 'not_acknowledged' })
  })

  it('asks the status again when a step finds the report in another phase', async () => {
    await atDecisions()
    store.acknowledge('k', true)
    const before = of('reportStatus').length
    onReportAct = () => Promise.reject(new RpcError(-32028, 'phase', { phase: 'finishing', reason: 'wrong_phase' }))
    onReportStatus = () => Promise.resolve(rstatus('ready'))
    await store.decide()
    await settle()
    expect(of('reportStatus')).toHaveLength(before + 1)
    expect(store.get().page).toBe('upload')
  })

  it('uploads only with the risk accepted when the report needs review', async () => {
    await atUpload('ready')
    await store.upload()
    expect(of('reportUpload')).toEqual([])
    store.acceptRisk(true)
    onReportStatus = () => Promise.resolve(rstatus('uploaded'))
    await store.upload()
    await settle()
    expect(of('reportUpload')).toEqual([['reportUpload', 'rs-1', true]])
    expect(store.get().report?.status?.receipt?.receipt_id).toBe('rcpt-1')
  })

  it('retries an upload only when the failure said it may help', async () => {
    await atUpload('upload_failed', { error: { message: 'refused', retryable: false, status: 400 } })
    await store.retryUpload()
    expect(of('reportRetry')).toEqual([])
    store.close()
    await atUpload('upload_failed', { error: { message: 'unreachable', retryable: true, status: null } })
    await store.retryUpload()
    expect(of('reportRetry')).toHaveLength(1)
  })

  it('settles an upload whose answer was lost by asking the status, and sends it once', async () => {
    await atUpload('ready', { summary: { classification: 'clean', warnings: [], size_bytes: null, auto_replaced: 0, kept: 0, redacted: 0, acknowledged: 0, completeness: 'complete' } })
    onReportAct = () => Promise.reject(new RpcError(-1, 'connection closed'))
    onReportStatus = () => Promise.resolve(rstatus('uploaded'))
    await store.upload()
    await settle()
    expect(store.get().report?.status?.phase).toBe('uploaded')
    expect(of('reportUpload')).toHaveLength(1)
  })

  it('sends no second step while the first is still settling, even through a lost status', async () => {
    await atUpload('ready')
    store.acceptRisk(true)
    const pending = deferred<ReportStatus>()
    onReportStatus = () => pending.promise
    await store.upload()
    await store.upload()
    expect(of('reportUpload')).toHaveLength(1)
    pending.reject(new Error('socket closed'))
    await settle()
    expect(store.get().report?.lost).toBe('socket closed')
    await store.upload()
    expect(of('reportUpload')).toHaveLength(1)
    onReportStatus = () => Promise.resolve(rstatus('uploaded'))
    await settle(store.RETRY_MS)
    expect(store.get().acting).toBe(false)
    expect(store.get().report?.status?.phase).toBe('uploaded')
  })

  it.each([
    ['decide', async () => { await atDecisions(); store.acknowledge('k', true); await store.decide() }],
    ['upload', async () => { await atUpload('ready'); store.acceptRisk(true); await store.upload() }],
    ['retry', async () => {
      await atUpload('upload_failed', { error: { message: 'x', retryable: true, status: null } })
      await store.retryUpload()
    }],
  ])('lets a report the gateway no longer has be filed again after a %s', async (_name, step) => {
    onReportAct = () => Promise.reject(new RpcError(-32024, 'gone', { id: 'rs-1' }))
    await step()
    const s = store.get()
    expect(s.report).toBeNull()
    expect(s.page).toBe('problem')
    expect(s.error).toEqual({ kind: 'report_gone' })
    expect(store.problemDraft().description).toBe('it broke')
    onReportAct = () => Promise.resolve({ ok: true })
    await store.startReport()
    expect(of('reportStart')).toHaveLength(2)
  })

  it.each([
    ['decide', 'resolve'], ['decide', 'reject'], ['upload', 'resolve'], ['upload', 'reject'], ['retry', 'resolve'], ['retry', 'reject'],
  ] as const)('writes nothing from a %s that lands (%s) after its wizard closed', async (step, how) => {
    const pending = deferred<{ ok: boolean }>()
    onReportAct = () => pending.promise
    if (step === 'decide') {
      await atDecisions()
      store.acknowledge('k', true)
      void store.decide()
    } else if (step === 'upload') {
      await atUpload('ready')
      store.acceptRisk(true)
      void store.upload()
    } else {
      await atUpload('upload_failed', { error: { message: 'x', retryable: true, status: null } })
      void store.retryUpload()
    }
    store.close()
    await atProblemPage()
    const fresh = store.get()
    const asked = of('reportStatus').length
    if (how === 'resolve') pending.resolve({ ok: true })
    else pending.reject(new RpcError(-32027, 'clash', { item_ids: ['a'] }))
    await settle(10_000)
    expect(store.get()).toEqual(fresh)
    expect(of('reportStatus')).toHaveLength(asked)
  })

  it('starts again after a failure, letting the failed report go', async () => {
    await atProblemPage()
    onReportStatus = () => Promise.resolve(rstatus('failed', { error: { message: 'snapshot gone', retryable: false, status: null } }))
    await store.startReport()
    await settle()
    expect(store.get().page).toBe('problem')
    expect(store.working()).toBe(false)
    onReportStart = () => Promise.resolve({ report_id: 'rs-2' })
    onReportStatus = () => Promise.resolve(rstatus('collecting'))
    await store.startReport()
    expect(of('reportCancel')).toEqual([['reportCancel', 'rs-1']])
    expect(store.get().report?.id).toBe('rs-2')
  })

  it('keeps the same report through a failed status query and names one that is gone', async () => {
    await atProblemPage()
    onReportStatus = () => Promise.reject(new Error('socket closed'))
    await store.startReport()
    await settle()
    expect(store.get().report).toMatchObject({ id: 'rs-1', lost: 'socket closed' })
    onReportStatus = () => Promise.reject(new RpcError(-32024, 'gone', { id: 'rs-1' }))
    await settle(store.RETRY_MS)
    expect(store.get().report).toBeNull()
    expect(store.get().error).toEqual({ kind: 'report_gone' })
  })

  it('cancels any report on close, a delivered one included, and none when there is none', async () => {
    await atProblemPage()
    store.close()
    expect(of('reportCancel')).toEqual([])
    await atUpload('uploaded')
    expect(store.working()).toBe(false)
    store.dismiss()
    expect(confirm.get().open).toBe(false)
    expect(of('reportCancel')).toEqual([['reportCancel', 'rs-1']])
  })

  it('asks before discarding a report waiting on the reader', async () => {
    await atDecisions()
    store.dismiss()
    expect(confirm.get().open).toBe(true)
    expect(of('reportCancel')).toEqual([])
    confirm.answer(true)
    expect(of('reportCancel')).toEqual([['reportCancel', 'rs-1']])
  })
})
