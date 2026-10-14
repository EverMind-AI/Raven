/* The export wizard: which conversation, which of its turns, and what is done
 * with them -- packed into a download, or filed as a bug report.
 *
 * A flow is a sequence of pages. The export is the turn picker, then the
 * export page; the bug report is the picker, the problem, the decisions and
 * the upload. The picker drops out when the conversation has one turn to give,
 * and the decisions are marked skipped when the redaction history (or a clean
 * snapshot) leaves nothing to decide. The export runs as a gateway job
 * (`trajectory.export`) and the report as a Report Session (`bugreport.*`);
 * the page follows either by polling its status while the gateway works.
 *
 * `epoch` moves on every open and every close, and every answer is checked
 * against the one it was asked under. Two answers need more than dropping:
 *   - a start that lands after its wizard closed still created a job or a
 *     report, which nobody would cancel, so the late id is cancelled on arrival;
 *   - a confirmation raised by one wizard closes only that wizard.
 * A status query that fails leaves the job or the report in place (`lost`) and
 * asks again, because it may well be running fine behind a dropped
 * connection: starting another would leave the first running and count
 * against the gateway's limit.
 *
 * The text fields are uncontrolled, and the pages they are on unmount whenever
 * the reader steps back, so what was typed lives here (`nameDraft`, `draft`):
 * the fields are rebuilt from it and the start sends it, which keeps the two
 * the same.
 */

import { t } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import {
  answered, conflictOf, invalidSelection, isDisabled, isGone, isLimit, isUnavailable, messageOf, stateOf,
} from './source'

import type {
  Action, ExportSource, ExportStatus, ExportTurn, Problem, ReportContexts, ReportItem, ReportStatus, Severity,
} from './types'

export const POLL_MS = 700
export const RETRY_MS = 3000
export const LIST_PAGE = 200
export const REPORTER_KEY = 'raven.gui.export.reporter'
const DEFAULT_BASE = 'raven-export'
const BASE_LIMIT = 80
const ILLEGAL = '/\\:*?"<>|'

export type Flow = 'export' | 'report'
export type PageId = 'turns' | 'export' | 'problem' | 'decide' | 'upload'

export interface Job {
  id: string
  /** The last answer about it; null until the first one. */
  status: ExportStatus | null
  /** Why the last status query failed; the job is still this one. */
  lost: string | null
}

export interface Report {
  id: string
  /** The last answer about it; null until the first one. */
  status: ReportStatus | null
  /** Why the last status query failed; the report is still this one. */
  lost: string | null
}

/** One item's contexts, as far as the page has them. */
export type Context =
  | { state: 'loading' }
  | { state: 'ready'; answer: ReportContexts }
  | { state: 'error'; detail: string }

/** What the error line says, read into words by the page so a language pick reaches it. */
export type Fault =
  | { kind: 'load'; detail: string }
  | { kind: 'changed' }
  | { kind: 'limit' }
  | { kind: 'report_limit' }
  | { kind: 'disabled' }
  | { kind: 'unavailable' }
  | { kind: 'gone' }
  | { kind: 'report_gone' }
  | { kind: 'conflict' }
  | { kind: 'not_acknowledged' }
  | { kind: 'undecided' }
  | { kind: 'risk' }
  | { kind: 'error'; detail: string }

export interface ExportState {
  open: boolean
  flow: Flow
  sessionKey: string | null
  /** This flow's pages, in order. */
  pages: PageId[]
  page: PageId
  /** Pages this run passed over: the decisions, when nothing was left to decide. */
  skipped: PageId[]
  title: string
  /** The conversation's turns; null until the gateway has listed them. */
  turns: ExportTurn[] | null
  /** The first listing landed, so the defaults below are set. */
  listed: boolean
  /** Chosen trace ids. */
  selected: string[]
  /** Turns the gateway refused, by trace id, with its reason. */
  invalid: Record<string, string>
  /* The refused turns as the picker last listed them, so one the conversation
     no longer has can still be named on the picker with its reason. */
  refused: ExportTurn[]
  /** The suggested name; the field itself is uncontrolled. */
  fileName: string
  /** `trajectory.export` is in flight. */
  starting: boolean
  job: Job | null
  /** The job the automatic download already fired for. */
  downloaded: string | null
  /** `bugreport.start` is in flight. */
  reportStarting: boolean
  report: Report | null
  /** The problem has a description, so it may be filed. */
  problemReady: boolean
  /** The page's decision for each suspected item. */
  choices: Record<string, Action>
  /** Confirmed items the reader has acknowledged. */
  acknowledged: string[]
  /** The item the right column shows. */
  focus: string | null
  contexts: Record<string, Context>
  /** Items the last refused step named. */
  marked: string[]
  /** The page of the decision list, LIST_PAGE rows each. */
  listPage: number
  riskAccepted: boolean
  /** A decide, upload or retry is in flight, or waiting for the status it moves. */
  acting: boolean
  error: Fault | null
  epoch: number
}

const initial = (): ExportState => ({
  open: false,
  flow: 'export',
  sessionKey: null,
  pages: ['turns', 'export'],
  page: 'turns',
  skipped: [],
  title: '',
  turns: null,
  listed: false,
  selected: [],
  invalid: {},
  refused: [],
  fileName: '',
  starting: false,
  job: null,
  downloaded: null,
  reportStarting: false,
  report: null,
  problemReady: false,
  choices: {},
  acknowledged: [],
  focus: null,
  contexts: {},
  marked: [],
  listPage: 0,
  riskAccepted: false,
  acting: false,
  error: null,
  epoch: 0,
})

/** A fresh report's own state, which a restart clears too. */
const blankReport = (): Partial<ExportState> => ({
  report: null,
  choices: {},
  acknowledged: [],
  focus: null,
  contexts: {},
  marked: [],
  listPage: 0,
  riskAccepted: false,
  acting: false,
  skipped: [],
})

const store = makeStore<ExportState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<ExportState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): ExportSource => ds('export')

/** What the problem page holds; `severity` is one of the four. */
export interface ProblemDraft {
  description: string
  severity: Severity
  expected: string
  actual: string
  steps: string
  reporter: string
}

const readReporter = (): string => {
  try { return window.localStorage.getItem(REPORTER_KEY) ?? '' } catch { return '' }
}

const writeReporter = (name: string): void => {
  try { window.localStorage.setItem(REPORTER_KEY, name) } catch { /* a private window keeps nothing */ }
}

const blankDraft = (): ProblemDraft => ({
  description: '', severity: 'medium', expected: '', actual: '', steps: '', reporter: readReporter(),
})

let timer: ReturnType<typeof setTimeout> | null = null
let querying: string | null = null
let nameDraft: string | null = null
let draft: ProblemDraft = blankDraft()

const clearTimer = (): void => {
  if (timer !== null) clearTimeout(timer)
  timer = null
}

const current = (epoch: number): boolean => store.get().epoch === epoch

/* Fire and forget: a cancel that fails leaves a job or a report the gateway
   expires on its own, and there is no wizard left to say so in. */
const forget = (jobId: string): void => {
  void source().cancel(jobId).catch(() => undefined)
}

const forgetReport = (reportId: string): void => {
  void source().reportCancel(reportId).catch(() => undefined)
}

const pagesFor = (flow: Flow, skipTurns: boolean): PageId[] => {
  const rest: PageId[] = flow === 'report' ? ['problem', 'decide', 'upload'] : ['export']
  return skipTurns ? rest : ['turns', ...rest]
}

/* -- what the page reads ------------------------------------------------- */

/** The file name as the reader left it; the suggestion until they type. */
export const fileName = (s: ExportState = store.get()): string => nameDraft ?? s.fileName

/** What the file name field holds now; nothing is drawn again for it. */
export function editName(value: string): void {
  nameDraft = value
}

/** The problem as the reader left it. */
export const problemDraft = (): Readonly<ProblemDraft> => draft

/* What a problem field holds now. Nothing is drawn again for it, except when
   the description goes from empty to not, which is what enables Next. */
export function editProblem<K extends keyof ProblemDraft>(field: K, value: ProblemDraft[K]): void {
  draft = { ...draft, [field]: value }
  const ready = draft.description.trim() !== ''
  if (ready !== store.get().problemReady) set({ problemReady: ready })
}

/** Whether the conversation on screen may be exported. */
export const exportable = (sessionKey: string | null, offered: boolean): boolean => offered && sessionKey !== null

export const terminal = (job: Job | null): boolean => {
  const state = job?.status?.state
  return state === 'done' || state === 'failed' || state === 'cancelled'
}

/** The report is over: delivered, failed, or cancelled from elsewhere. */
export const reportOver = (report: Report | null): boolean => {
  const phase = report?.status?.phase
  return phase === 'uploaded' || phase === 'failed' || phase === 'cancelled'
}

/* Something is under way that closing the wizard would stop: a job starting
   or running, or a report started and not over -- including one waiting on the
   reader, whose decisions and frozen snapshot a close throws away. */
export const working = (s: ExportState = store.get()): boolean =>
  s.flow === 'report'
    ? s.reportStarting || (s.report !== null && !reportOver(s.report))
    : s.starting || (s.job !== null && !terminal(s.job))

const pickable = (s: ExportState): string[] => (s.turns ?? []).filter((turn) => !turn.in_progress).map((turn) => turn.trace_id)

/** The select-all box: every pickable turn chosen, some, or none. */
export function allState(s: ExportState = store.get()): 'all' | 'some' | 'none' {
  const ids = pickable(s)
  const chosen = ids.filter((id) => s.selected.includes(id)).length
  if (!chosen) return 'none'
  return chosen === ids.length ? 'all' : 'some'
}

/** `<title>-<YYYYMMDD-HHMM>.tar.gz` in local time, without what a file name may not hold. */
export function defaultFileName(title: string, now: Date = new Date()): string {
  const two = (n: number): string => String(n).padStart(2, '0')
  const kept = [...title].filter((c) => c.charCodeAt(0) >= 32 && c.charCodeAt(0) !== 127 && !ILLEGAL.includes(c))
  const base = kept.join('').trim().slice(0, BASE_LIMIT).trim() || DEFAULT_BASE
  const stamp = `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}`
  return `${base}-${stamp}.tar.gz`
}

/** The report's items still to decide; empty outside the decisions. */
export const items = (s: ExportState = store.get()): ReportItem[] => s.report?.status?.items ?? []

/** Every confirmed item has been acknowledged. */
export function acknowledgedAll(s: ExportState = store.get()): boolean {
  return items(s).every((item) => item.kind !== 'confirmed' || s.acknowledged.includes(item.id))
}

/* An item and every item linked to it, however far: one decision covers the
   whole group, which is what the gateway checks. Links are read both ways. */
export function linkedGroup(all: readonly ReportItem[], start: string): string[] {
  const seen = new Set([start])
  const queue = [start]
  while (queue.length) {
    const id = queue.shift()!
    for (const item of all) {
      const next = item.id === id ? item.linked : item.linked.includes(id) ? [item.id] : []
      for (const other of next) {
        if (!seen.has(other)) {
          seen.add(other)
          queue.push(other)
        }
      }
    }
  }
  return [...seen]
}

/* -- the turns ----------------------------------------------------------- */

async function listTurns(epoch: number): Promise<void> {
  const key = store.get().sessionKey
  if (key === null) return
  set({ turns: null })
  let answer
  try {
    answer = await source().turns(key)
  } catch (err) {
    if (current(epoch)) set({ error: { kind: 'load', detail: messageOf(err) } })
    return
  }
  if (!current(epoch)) return
  const s = store.get()
  if (!s.listed) {
    const ids = answer.turns.filter((turn) => !turn.in_progress).map((turn) => turn.trace_id)
    const pages = pagesFor(s.flow, answer.turns.length === 1 && ids.length === 1)
    set({
      title: answer.title,
      turns: answer.turns,
      listed: true,
      selected: ids,
      pages,
      page: pages[0]!,
      fileName: defaultFileName(answer.title),
    })
    return
  }
  const there = new Set(answer.turns.filter((turn) => !turn.in_progress).map((turn) => turn.trace_id))
  set({ turns: answer.turns, selected: s.selected.filter((id) => there.has(id) && !(id in s.invalid)) })
}

/** Opens the wizard on a conversation; one already open is closed first. */
export function open(flow: Flow, sessionKey: string): void {
  if (store.get().open) close()
  clearTimer()
  nameDraft = null
  draft = blankDraft()
  const epoch = store.get().epoch + 1
  store.set({ ...initial(), open: true, flow, sessionKey, pages: pagesFor(flow, false), epoch })
  void listTurns(epoch)
}

/** Lists the turns again after a listing failed. */
export function retryTurns(): void {
  const s = store.get()
  if (!s.open) return
  set({ error: null })
  void listTurns(s.epoch)
}

export function toggle(traceId: string): void {
  const s = store.get()
  if (!pickable(s).includes(traceId)) return
  set({ selected: s.selected.includes(traceId) ? s.selected.filter((id) => id !== traceId) : [...s.selected, traceId] })
}

export function toggleAll(): void {
  const s = store.get()
  set({ selected: allState(s) === 'all' ? [] : pickable(s) })
}

/** From the picker to the page after it; every later page moves by what it does. */
export function next(): void {
  const s = store.get()
  const at = s.pages.indexOf(s.page)
  if (s.page !== 'turns' || !s.selected.length || at < 0 || at >= s.pages.length - 1) return
  set({ page: s.pages[at + 1]!, error: null })
}

export function back(): void {
  const s = store.get()
  const at = s.pages.indexOf(s.page)
  if (working(s) || at <= 0) return
  set({ page: s.pages[at - 1]!, error: null })
}

/* A refused start, for either flow. A refused selection goes back to the
   picker, lists the turns again, marks the refused ones and keeps the rest. */
function refused(err: unknown, epoch: number): void {
  const s = store.get()
  const selection = invalidSelection(err)
  if (selection && selection.invalid.length) {
    const invalid = Object.fromEntries(selection.invalid.map((row) => [row.trace_id, row.why]))
    const listed = new Map((s.turns ?? []).map((turn) => [turn.trace_id, turn]))
    set({
      starting: false,
      reportStarting: false,
      invalid,
      refused: selection.invalid.map((row) => listed.get(row.trace_id) ?? {
        trace_id: row.trace_id, number: 0, start: '', input_preview: row.trace_id, in_progress: false,
      }),
      selected: s.selected.filter((id) => !(id in invalid)),
      pages: pagesFor(s.flow, false),
      page: 'turns',
      error: { kind: 'changed' },
    })
    void listTurns(epoch)
    return
  }
  const error: Fault = isLimit(err) ? { kind: s.flow === 'report' ? 'report_limit' : 'limit' }
    : isDisabled(err) ? { kind: 'disabled' }
    : isUnavailable(err) ? { kind: 'unavailable' }
    : { kind: 'error', detail: messageOf(err) }
  set({ starting: false, reportStarting: false, error })
}

/* -- the export job ------------------------------------------------------ */

/** Starts the export under the drafted name; a second press while one is starting or running does nothing. */
export async function start(): Promise<void> {
  const s = store.get()
  if (!s.open || s.flow !== 'export' || working(s) || s.sessionKey === null || !s.selected.length) return
  const mine = s.epoch
  const name = fileName(s)
  set({ starting: true, error: null, job: null, downloaded: null })
  let answer
  try {
    answer = await source().start(s.sessionKey, s.selected, name)
  } catch (err) {
    if (current(mine)) refused(err, mine)
    return
  }
  if (!current(mine)) {
    forget(answer.job_id)
    return
  }
  set({ starting: false, job: { id: answer.job_id, status: null, lost: null } })
  void query(answer.job_id, mine)
}

const owns = (jobId: string, epoch: number): boolean => {
  const s = store.get()
  return s.epoch === epoch && s.job?.id === jobId
}

const patchJob = (patch: Partial<Job>): void => {
  const job = store.get().job
  if (job) set({ job: { ...job, ...patch } })
}

function schedule(ms: number): void {
  clearTimer()
  const s = store.get()
  const job = s.job
  if (!job) return
  const { epoch } = s
  timer = setTimeout(() => {
    timer = null
    void query(job.id, epoch)
  }, ms)
}

async function query(jobId: string, epoch: number): Promise<void> {
  const ticket = `${epoch}:${jobId}`
  if (querying === ticket) return
  querying = ticket
  let status
  try {
    status = await source().status(jobId)
  } catch (err) {
    if (querying === ticket) querying = null
    if (!owns(jobId, epoch)) return
    if (isGone(err)) {
      set({ job: null, error: { kind: 'gone' } })
      return
    }
    patchJob({ lost: messageOf(err) })
    schedule(RETRY_MS)
    return
  }
  if (querying === ticket) querying = null
  if (!owns(jobId, epoch)) return
  patchJob({ status, lost: null })
  if (!terminal(store.get().job)) schedule(POLL_MS)
}

/** Asks about the job or the report now, instead of at the next retry. */
export function requery(): void {
  const s = store.get()
  if (s.flow === 'report') {
    if (!s.report || reportOver(s.report)) return
    clearTimer()
    void queryReport(s.report.id, s.epoch)
    return
  }
  if (!s.job || terminal(s.job)) return
  clearTimer()
  void query(s.job.id, s.epoch)
}

/** The automatic download fired for this job. */
export function markDownloaded(jobId: string): void {
  set({ downloaded: jobId })
}

/* -- the bug report ------------------------------------------------------ */

const POLLED: ReadonlySet<string> = new Set(['collecting', 'finishing', 'uploading'])

/* Files the problem; a second press while a report is starting or under way
   does nothing. A report that failed or was cancelled elsewhere is let go and
   the same turns and words are filed again. */
export async function startReport(): Promise<void> {
  const s = store.get()
  if (!s.open || s.flow !== 'report' || working(s) || s.sessionKey === null || !s.selected.length || !s.problemReady) return
  if (s.report) forgetReport(s.report.id)
  const mine = s.epoch
  const problem: Problem = { ...draft }
  writeReporter(draft.reporter)
  set({ ...blankReport(), reportStarting: true, error: null })
  let answer
  try {
    answer = await source().reportStart(s.sessionKey, s.selected, problem)
  } catch (err) {
    if (current(mine)) refused(err, mine)
    return
  }
  if (!current(mine)) {
    forgetReport(answer.report_id)
    return
  }
  set({ reportStarting: false, report: { id: answer.report_id, status: null, lost: null } })
  void queryReport(answer.report_id, mine)
}

const ownsReport = (reportId: string, epoch: number): boolean => {
  const s = store.get()
  return s.epoch === epoch && s.report?.id === reportId
}

function scheduleReport(ms: number): void {
  clearTimer()
  const s = store.get()
  const report = s.report
  if (!report) return
  const { epoch } = s
  timer = setTimeout(() => {
    timer = null
    void queryReport(report.id, epoch)
  }, ms)
}

async function queryReport(reportId: string, epoch: number): Promise<void> {
  const ticket = `${epoch}:${reportId}`
  if (querying === ticket) return
  querying = ticket
  let status
  try {
    status = await source().reportStatus(reportId)
  } catch (err) {
    if (querying === ticket) querying = null
    if (!ownsReport(reportId, epoch)) return
    if (isGone(err)) {
      set({ ...blankReport(), page: 'problem', error: { kind: 'report_gone' } })
      return
    }
    const report = store.get().report!
    set({ report: { ...report, lost: messageOf(err) } })
    scheduleReport(RETRY_MS)
    return
  }
  if (querying === ticket) querying = null
  if (!ownsReport(reportId, epoch)) return
  applyStatus(status)
}

/* Where an answer puts the wizard. The decisions are set up the first time
   the report asks for them -- the history's hint where it has one, kept
   otherwise -- and a report that never asked has its decisions page marked
   skipped. */
function applyStatus(status: ReportStatus): void {
  const s = store.get()
  const report = s.report!
  const first = report.status?.phase !== status.phase
  const patch: Partial<ExportState> = { report: { ...report, status, lost: null }, acting: false }
  switch (status.phase) {
    case 'collecting':
      patch.page = 'problem'
      break
    case 'deciding': {
      patch.page = 'decide'
      if (first) {
        const list = status.items ?? []
        patch.choices = Object.fromEntries(
          list.filter((item) => item.kind === 'suspected').map((item) => [item.id, item.hint ?? 'kept']),
        )
        patch.acknowledged = []
        patch.focus = list[0]?.id ?? null
        patch.listPage = 0
      }
      break
    }
    case 'finishing':
    case 'ready':
    case 'uploading':
    case 'uploaded':
    case 'upload_failed':
      if (s.page === 'problem' && !s.skipped.includes('decide')) patch.skipped = [...s.skipped, 'decide']
      patch.page = 'upload'
      break
    case 'failed':
    case 'cancelled':
      patch.page = 'problem'
      break
  }
  set(patch)
  if (patch.focus) loadContext(patch.focus)
  if (POLLED.has(status.phase)) scheduleReport(POLL_MS)
}

function loadContext(itemId: string): void {
  const s = store.get()
  const report = s.report
  if (!report || s.contexts[itemId]?.state === 'ready' || s.contexts[itemId]?.state === 'loading') return
  const { epoch } = s
  set({ contexts: { ...s.contexts, [itemId]: { state: 'loading' } } })
  source().reportItem(report.id, itemId).then(
    (answer) => {
      if (ownsReport(report.id, epoch)) set({ contexts: { ...store.get().contexts, [itemId]: { state: 'ready', answer } } })
    },
    (err: unknown) => {
      if (ownsReport(report.id, epoch)) {
        set({ contexts: { ...store.get().contexts, [itemId]: { state: 'error', detail: messageOf(err) } } })
      }
    },
  )
}

/** Shows an item's contexts in the right column, asking for them once. */
export function focusItem(itemId: string): void {
  set({ focus: itemId })
  loadContext(itemId)
}

/** Asks again for contexts that failed. */
export function retryContext(itemId: string): void {
  const s = store.get()
  if (s.contexts[itemId]?.state !== 'error') return
  const contexts = { ...s.contexts }
  delete contexts[itemId]
  set({ contexts })
  loadContext(itemId)
}

/* Keep or redact: every suspected item, or the focused one with its whole
   linked group. A confirmed item has no such choice -- its value is already
   replaced -- so a focus on one changes nothing. */
export function choose(action: Action, scope: 'all' | 'focus'): void {
  const s = store.get()
  const list = items(s)
  const suspected = new Set(list.filter((item) => item.kind === 'suspected').map((item) => item.id))
  let ids: string[]
  if (scope === 'all') ids = [...suspected]
  else {
    if (!s.focus || !suspected.has(s.focus)) return
    ids = linkedGroup(list, s.focus).filter((id) => suspected.has(id))
  }
  const choices = { ...s.choices }
  for (const id of ids) choices[id] = action
  set({ choices, marked: [], error: null })
}

export function acknowledge(itemId: string, on: boolean): void {
  const s = store.get()
  const rest = s.acknowledged.filter((id) => id !== itemId)
  set({ acknowledged: on ? [...rest, itemId] : rest, marked: s.marked.filter((id) => id !== itemId) })
}

export function setListPage(page: number): void {
  const pages = Math.max(1, Math.ceil(items().length / LIST_PAGE))
  set({ listPage: Math.min(Math.max(0, page), pages - 1) })
}

export function acceptRisk(on: boolean): void {
  set({ riskAccepted: on })
}

/* One step that moves the report on: a decide, an upload or a retry. What the
   gateway answers is only that it took the step -- the report's worker carries
   it out -- so the outcome is read from the report's own status, and `acting`
   stays up until that status lands. Nothing is sent twice meanwhile, and an
   answer that cannot say whether the step was taken (a dropped connection) is
   settled the same way rather than by sending it again. A step that lands
   after its wizard closed writes nothing and asks nothing. `onRefusal` takes
   the refusals the reader can put right and says whether it did. */
async function act(step: (reportId: string) => Promise<unknown>, onRefusal: (err: unknown) => boolean): Promise<void> {
  const s = store.get()
  const report = s.report
  if (!report || s.acting) return
  const mine = s.epoch
  set({ acting: true, error: null })
  let failure: unknown = null
  let failed = false
  try {
    await step(report.id)
  } catch (err) {
    failure = err
    failed = true
  }
  if (!ownsReport(report.id, mine)) return
  if (!failed) {
    void queryReport(report.id, mine)
    return
  }
  if (isGone(failure)) {
    set({ ...blankReport(), page: 'problem', error: { kind: 'report_gone' } })
    return
  }
  if (onRefusal(failure)) return
  const refusal = stateOf(failure)
  if (!answered(failure) || refusal?.reason === 'wrong_phase' || refusal?.reason === 'not_retryable') {
    void queryReport(report.id, mine)
    return
  }
  set({ acting: false, error: { kind: 'error', detail: messageOf(failure) } })
}

/** Sends every decision on the page; every confirmed item must be acknowledged first. */
export async function decide(): Promise<void> {
  const s = store.get()
  if (s.report?.status?.phase !== 'deciding' || !acknowledgedAll(s)) return
  const list = items(s)
  const decisions = list
    .filter((item) => item.kind === 'suspected')
    .map((item) => ({ item_id: item.id, action: s.choices[item.id] ?? 'kept' }))
  const confirmed = new Set(list.filter((item) => item.kind === 'confirmed').map((item) => item.id))
  const acknowledged = s.acknowledged.filter((id) => confirmed.has(id))
  await act((id) => source().reportDecide(id, decisions, acknowledged), (err) => {
    const clash = conflictOf(err)
    if (clash) {
      set({ acting: false, marked: clash, error: { kind: 'conflict' } })
      return true
    }
    const refusal = stateOf(err)
    if (refusal?.reason === 'not_acknowledged' || refusal?.reason === 'undecided') {
      set({ acting: false, marked: refusal.itemIds, error: { kind: refusal.reason } })
      return true
    }
    return false
  })
}

/** Uploads the frozen report; one that needs review needs the risk accepted. */
export async function upload(): Promise<void> {
  const s = store.get()
  const status = s.report?.status
  if (status?.phase !== 'ready') return
  if (status.summary?.classification === 'needs_review' && !s.riskAccepted) return
  await act((id) => source().reportUpload(id, s.riskAccepted), (err) => {
    if (stateOf(err)?.reason === 'risk_not_accepted') {
      set({ acting: false, error: { kind: 'risk' } })
      return true
    }
    return false
  })
}

/** Tries the upload again, when the failure said it may help. */
export async function retryUpload(): Promise<void> {
  const status = store.get().report?.status
  if (status?.phase !== 'upload_failed' || !status.error?.retryable) return
  await act((id) => source().reportRetry(id), () => false)
}

/* -- closing ------------------------------------------------------------- */

/* Closes the wizard, cancelling a job that has not settled and any report: one
   not landed is removed, a landed record stays, and one that is over only lets
   the gateway forget its result. */
export function close(): void {
  const s = store.get()
  clearTimer()
  nameDraft = null
  draft = blankDraft()
  if (s.job && !terminal(s.job)) forget(s.job.id)
  if (s.report) forgetReport(s.report.id)
  store.set({ ...initial(), epoch: s.epoch + 1 })
}

/* The close button and Escape. With work under way the reader is asked first,
   and the answer closes only the wizard it was asked about. */
export function dismiss(): void {
  const s = store.get()
  if (!s.open) return
  if (!working(s)) {
    close()
    return
  }
  const asked = s.epoch
  const report = s.flow === 'report'
  confirm.ask(
    report ? t('gui.export.stop_report_title') : t('gui.export.stop_title'),
    report ? t('gui.export.stop_report_body') : t('gui.export.stop_body'),
    report ? t('gui.export.stop_report_label') : t('gui.export.stop_label'),
    () => {
      if (current(asked)) close()
    },
  )
}

export function _resetForTests(): void {
  clearTimer()
  querying = null
  nameDraft = null
  draft = blankDraft()
  store._resetForTests()
}
