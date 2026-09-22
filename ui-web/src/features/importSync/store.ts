/* The import the rail follows: one row above Settings from the wizard's start
 * to the last phase, and what a reader can do to it at each point.
 *
 * The row is drawn from `import.status` alone. Its counts live in the
 * importer's state file, so they survive a gateway restart; `running` and the
 * phase in flight are the gateway process's own, so after a restart the file
 * still says how far a run got while nothing is running it -- which is the
 * "paused" row, and a click on it asks for the same run again. The store polls
 * only while a run is on; otherwise the page reads once at boot and once when
 * the wizard closes (app/install.ts), the two moments a run can begin.
 *
 * A settled run -- finished, or stopped short -- is on the rail only while
 * this page has something to say about it: it asked for the run or found it in
 * flight, or the row's click can still act on it. The importer's file outlives
 * every run that wrote it, so without that the next reader of any install whose
 * file is not empty is told about an import they never started. A finished run
 * that did earn its row stays until dismissed, and the dismissal is remembered
 * per run (its request and its final counts) so a reload does not bring the
 * same finished row back. */

import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'

import type { ImportPhase, ImportSource, ImportStarted, ImportStatus, ImportSyncSource, ImportTier } from './types'

export const POLL_MS = 3000
const DISMISSED_KEY = 'raven.importSync.dismissed'

export type RowKind = 'hidden' | 'scan' | 'run' | 'wrap' | 'paused' | 'done'

export interface ImportSyncState {
  status: ImportStatus | null
  /* Between asking for a run and hearing that it started: the scan the gateway
     does before it answers is the one stretch with no counts to show. */
  starting: boolean
  /* A run of this page's own is in flight: it asked for one, or it found one
     running. Becomes `followed` the moment that run settles. */
  watching: boolean
  /* The run this page followed to its end, by the same signature `dismissed`
     uses. A settled run is only ever on the rail because of this or because
     its click still does something -- see `stale` below. Page state, never
     stored: a reload has followed nothing. */
  followed: string
  dismissed: string
  error: string
}

export interface RowView {
  kind: RowKind
  pct: number
  failed: number
  phase: ImportPhase | null
  /* The source the pass is on. Its own counts move with every batch, while the
     share one source buys the percentage is a fraction of a point in a run of
     many, and that is the movement a reader is looking for. */
  source: ImportSource | null
  /* Whether a click asks for the run again: a stopped run resumes, a finished
     run with failures retries them, and both are the same call. */
  clickable: boolean
}

const HIDDEN: RowView = { kind: 'hidden', pct: 0, failed: 0, phase: null, source: null, clickable: false }

/* Whether a run that is NOT running has earned a row. Two reasons it has:
   something can still be done about it, or this page followed THAT run to its
   end. A run that is neither is history -- the importer's file outlives every
   run that wrote it, so a CLI import from weeks ago, or one an earlier reader
   started, would otherwise greet the next reader as news. It is not news:
   nothing is moving, the click does nothing, and the reader never asked for
   it. The row is the work outstanding, never a receipt for work nobody here
   watched.

   Held per run rather than per page, because a page outlives a run: a tab that
   followed one import and stayed open would otherwise draw the receipt of the
   next one somebody ran from the CLI. */
const stale = (s: ImportSyncState, st: ImportStatus, clickable: boolean): boolean =>
  !clickable && s.followed !== signature(st)

const readDismissed = (): string => {
  try { return window.localStorage.getItem(DISMISSED_KEY) ?? '' } catch { return '' }
}

const writeDismissed = (sig: string): void => {
  try { window.localStorage.setItem(DISMISSED_KEY, sig) } catch { /* a private window keeps nothing */ }
}

const initial = (): ImportSyncState => ({ status: null, starting: false, watching: false, followed: '', dismissed: readDismissed(), error: '' })

const store = makeStore<ImportSyncState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<ImportSyncState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): ImportSyncSource => ds('importSync')

/* One run's identity: its request, its final counts and how its phases ended.
   Two runs of the same request that ended the same way are the same row to a
   reader. */
export const signature = (st: ImportStatus): string =>
  `${st.tier ?? ''}|${(st.platforms ?? []).join(',')}|${st.total}|${st.submitted}|${st.failed}|${st.phases?.status ?? ''}`

/* Whether the run on file can be asked for again: only when the file says
   exactly what was asked. A run the CLI started records no request, and a
   guess at one (say, "full") could turn a minutes-long import into hours. */
export const resumable = (st: ImportStatus): boolean => !!st.tier && (st.platforms?.length ?? 0) > 0

export function view(s: ImportSyncState): RowView {
  const st = s.status
  const total = st?.total ?? 0
  const settled = st ? st.submitted + st.failed : 0
  /* The share of the source the pass is on: a large source is many batches
     and many minutes, and the per-source counts stand still for all of them. */
  const within = st?.running && st.current && st.current.total ? Math.min(1, st.current.sent / st.current.total) : 0
  /* 100 percent is every source settled. Rounding up to it while one is still
     being sent is the 100-percent-then-wait this row exists to remove. */
  const pct = total ? Math.min(settled < total ? 99 : 100, Math.round(((settled + within) / total) * 100)) : 0
  if (s.starting && !st?.running) return { ...HIDDEN, kind: 'scan' }
  if (!st) return HIDDEN
  const phase = st.phase ?? null
  const phases = st.phases ?? null
  const again = resumable(st)
  if (st.running) {
    const phasePct = phase && phase.total ? Math.min(100, Math.round((phase.current / phase.total) * 100)) : pct
    return { kind: phase ? 'wrap' : 'run', pct: phase ? phasePct : pct, failed: st.failed, phase, source: phase ? null : st.current ?? null, clickable: false }
  }
  if (!total && !phases) return HIDDEN
  /* Short of the total: the message pass was stopped, or the gateway lost it. */
  if (settled < total) {
    return stale(s, st, again) ? HIDDEN : { kind: 'paused', pct, failed: st.failed, phase: null, source: null, clickable: again }
  }
  /* Settled, but the phases behind the pass never finished: no verdict on file
     for a run that recorded its request (lost before the phases began), or a
     verdict that says they were still running or were stopped. */
  const unfinished = phases === null ? again : phases.status === 'pending' || phases.status === 'cancelled'
  if (unfinished) {
    return stale(s, st, again) ? HIDDEN : { kind: 'paused', pct: 100, failed: st.failed, phase: null, source: null, clickable: again }
  }
  const failed = st.failed + (phases?.status === 'failed' ? phases.errors.length : 0)
  const retry = failed > 0 && again
  if (s.dismissed === signature(st)) return HIDDEN
  if (stale(s, st, retry)) return HIDDEN
  return { kind: 'done', pct: 100, failed, phase: null, source: null, clickable: retry }
}

const failure = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return (err && ((err.data && err.data.detail) || err.message)) || String(e)
}

let timer: ReturnType<typeof setInterval> | null = null

function poll(on: boolean): void {
  if (on && !timer) timer = setInterval(() => { void refresh() }, POLL_MS)
  if (!on && timer) { clearInterval(timer); timer = null }
}

/* One status read; keeps polling exactly while the gateway says a run is on. */
export async function refresh(): Promise<void> {
  try {
    const status = await source().status()
    /* A run in flight is this page's to follow; the read that finds it settled
       is where following turns into the one run whose row may be drawn. */
    store.set((prev) => ({
      ...prev,
      status,
      error: '',
      ...(status.running
        ? { watching: true }
        : prev.watching ? { watching: false, followed: signature(status) } : {}),
    }))
    poll(status.running)
  } catch (e) {
    set({ error: failure(e) })
    poll(false)
  }
}

/* Ask for a run and follow it. Every caller comes here -- the rail's own click
   and the wizard's sync step, which reaches it through the seam (src/app/
   install.ts) rather than calling the same method a second way. A run that
   starts is the run this page follows, which is what keeps its finished row on
   the rail afterwards. */
export async function start(platforms: string[], tier: ImportTier): Promise<ImportStarted> {
  set({ starting: true, error: '' })
  try {
    const r = await source().run(platforms, tier)
    /* Only a run that started is one to follow. A refusal ("nothing to
       import") leaves whatever settled run is already on file untouched, and
       the read below would otherwise adopt it as this page's. */
    if (r.started) set({ watching: true })
    /* Read after, then report: a refusal is this call's news, and the read
       that follows it clears the transport error slot as every read does. */
    await refresh()
    if (!r.started) set({ error: r.detail || 'import did not start' })
    return r
  } catch (e) {
    set({ error: failure(e) })
    return { started: false, total: 0, detail: failure(e) }
  } finally {
    set({ starting: false })
  }
}

/* The click on a stopped or partly failed row: the same request again. The
   importer skips what already landed and retries what failed, so this is both
   "resume" and "retry" with nothing to choose between. */
export async function resume(): Promise<void> {
  const st = get().status
  if (!st || st.running || !resumable(st)) return
  await start(st.platforms ?? [], st.tier as ImportTier)
}

export async function stop(): Promise<void> {
  try { await source().stop() } catch (e) { set({ error: failure(e) }) }
  await refresh()
}

export function dismiss(): void {
  const st = get().status
  if (!st) return
  const sig = signature(st)
  writeDismissed(sig)
  set({ dismissed: sig })
}

export function _resetForTests(): void {
  poll(false)
  store._resetForTests()
  store.set({ ...initial(), watching: false, followed: '', dismissed: '' })
}
