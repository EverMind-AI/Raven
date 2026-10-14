/* The export wizard: which conversation, which of its turns, and the job that
 * packs them into a download.
 *
 * A flow is a sequence of pages -- the export is the turn picker, then the
 * export page -- and the picker drops out when the conversation has one turn
 * to give. The job runs in the gateway (`trajectory.export`) and the page
 * follows it by polling `trajectory.export_status` until it settles.
 *
 * `epoch` moves on every open and every close, and every answer is checked
 * against the one it was asked under. Two answers need more than dropping:
 *   - a `trajectory.export` that lands after its wizard closed still created a
 *     job, which nobody would cancel, so the late id is cancelled on arrival;
 *   - a confirmation raised by one wizard closes only that wizard.
 * A status query that fails leaves the job in place (`lost`) and asks again,
 * because the job may well be running fine behind a dropped connection: a new
 * export would leave the old one running and count against the gateway's limit.
 *
 * The file name field is uncontrolled, and the page it is on unmounts whenever
 * the reader steps back, so what was typed lives here (`nameDraft`): the field
 * is rebuilt from it and the export sends it, which keeps the two the same.
 */

import { t } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { invalidSelection, isDisabled, isGone, isLimit, messageOf } from './source'

import type { ExportSource, ExportStatus, ExportTurn } from './types'

export const POLL_MS = 700
export const RETRY_MS = 3000
const DEFAULT_BASE = 'raven-export'
const BASE_LIMIT = 80
const ILLEGAL = '/\\:*?"<>|'

export type Flow = 'export'
export type PageId = 'turns' | 'export'

export interface Job {
  id: string
  /** The last answer about it; null until the first one. */
  status: ExportStatus | null
  /** Why the last status query failed; the job is still this one. */
  lost: string | null
}

/** What the error line says, read into words by the page so a language pick reaches it. */
export type Fault =
  | { kind: 'load'; detail: string }
  | { kind: 'changed' }
  | { kind: 'limit' }
  | { kind: 'disabled' }
  | { kind: 'gone' }
  | { kind: 'error'; detail: string }

export interface ExportState {
  open: boolean
  flow: Flow
  sessionKey: string | null
  /** This flow's pages, in order. */
  pages: PageId[]
  page: PageId
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
  error: Fault | null
  /** The job the automatic download already fired for. */
  downloaded: string | null
  epoch: number
}

const initial = (): ExportState => ({
  open: false,
  flow: 'export',
  sessionKey: null,
  pages: ['turns', 'export'],
  page: 'turns',
  title: '',
  turns: null,
  listed: false,
  selected: [],
  invalid: {},
  refused: [],
  fileName: '',
  starting: false,
  job: null,
  error: null,
  downloaded: null,
  epoch: 0,
})

const store = makeStore<ExportState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<ExportState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): ExportSource => ds('export')

let timer: ReturnType<typeof setTimeout> | null = null
let querying: string | null = null
let nameDraft: string | null = null

const clearTimer = (): void => {
  if (timer !== null) clearTimeout(timer)
  timer = null
}

const current = (epoch: number): boolean => store.get().epoch === epoch

/* Fire and forget: a cancel that fails leaves a job the gateway expires on its
   own, and there is no wizard left to say so in. */
const forget = (jobId: string): void => {
  void source().cancel(jobId).catch(() => undefined)
}

/* -- what the page reads ------------------------------------------------- */

/** The file name as the reader left it; the suggestion until they type. */
export const fileName = (s: ExportState = store.get()): string => nameDraft ?? s.fileName

/** What the file name field holds now; nothing is drawn again for it. */
export function editName(value: string): void {
  nameDraft = value
}

/** Whether the conversation on screen may be exported. */
export const exportable = (sessionKey: string | null, offered: boolean): boolean => offered && sessionKey !== null

export const terminal = (job: Job | null): boolean => {
  const state = job?.status?.state
  return state === 'done' || state === 'failed' || state === 'cancelled'
}

/** Something is under way that closing the wizard would stop. */
export const working = (s: ExportState = store.get()): boolean => s.starting || (s.job !== null && !terminal(s.job))

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
    const skip = answer.turns.length === 1 && ids.length === 1
    const pages: PageId[] = skip ? ['export'] : ['turns', 'export']
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
  const epoch = store.get().epoch + 1
  store.set({ ...initial(), open: true, flow, sessionKey, epoch })
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

export function next(): void {
  const s = store.get()
  const at = s.pages.indexOf(s.page)
  if (!s.selected.length || at < 0 || at >= s.pages.length - 1) return
  set({ page: s.pages[at + 1]!, error: null })
}

export function back(): void {
  const s = store.get()
  const at = s.pages.indexOf(s.page)
  if (working(s) || at <= 0) return
  set({ page: s.pages[at - 1]!, error: null })
}

/* -- the job ------------------------------------------------------------- */

/** Starts the export under the drafted name; a second press while one is starting or running does nothing. */
export async function start(): Promise<void> {
  const s = store.get()
  if (!s.open || working(s) || s.sessionKey === null || !s.selected.length) return
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

function refused(err: unknown, epoch: number): void {
  const selection = invalidSelection(err)
  if (selection && selection.invalid.length) {
    const s = store.get()
    const invalid = Object.fromEntries(selection.invalid.map((row) => [row.trace_id, row.why]))
    const listed = new Map((s.turns ?? []).map((turn) => [turn.trace_id, turn]))
    set({
      starting: false,
      invalid,
      refused: selection.invalid.map((row) => listed.get(row.trace_id) ?? {
        trace_id: row.trace_id, number: 0, start: '', input_preview: row.trace_id, in_progress: false,
      }),
      selected: s.selected.filter((id) => !(id in invalid)),
      pages: ['turns', 'export'],
      page: 'turns',
      error: { kind: 'changed' },
    })
    void listTurns(epoch)
    return
  }
  const error: Fault = isLimit(err) ? { kind: 'limit' }
    : isDisabled(err) ? { kind: 'disabled' }
    : { kind: 'error', detail: messageOf(err) }
  set({ starting: false, error })
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

/** Asks about the job now, instead of at the next retry. */
export function requery(): void {
  const s = store.get()
  if (!s.job || terminal(s.job)) return
  clearTimer()
  void query(s.job.id, s.epoch)
}

/** The automatic download fired for this job. */
export function markDownloaded(jobId: string): void {
  set({ downloaded: jobId })
}

/* -- closing ------------------------------------------------------------- */

/** Closes the wizard, cancelling a job that has not settled. */
export function close(): void {
  const s = store.get()
  clearTimer()
  nameDraft = null
  if (s.job && !terminal(s.job)) forget(s.job.id)
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
  confirm.ask(t('gui.export.stop_title'), t('gui.export.stop_body'), t('gui.export.stop_label'), () => {
    if (current(asked)) close()
  })
}

export function _resetForTests(): void {
  clearTimer()
  querying = null
  nameDraft = null
  store._resetForTests()
}
