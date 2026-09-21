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
 * A finished run stays on the rail until dismissed, and the dismissal is
 * remembered per run (its request and its final counts) so a reload does not
 * bring the same finished row back. */

import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'

import type { ImportPhase, ImportStarted, ImportStatus, ImportSyncSource, ImportTier } from './types'

export const POLL_MS = 3000
const DISMISSED_KEY = 'raven.importSync.dismissed'

export type RowKind = 'hidden' | 'scan' | 'run' | 'wrap' | 'paused' | 'done'

export interface ImportSyncState {
  status: ImportStatus | null
  /* Between asking for a run and hearing that it started: the scan the gateway
     does before it answers is the one stretch with no counts to show. */
  starting: boolean
  dismissed: string
  error: string
}

export interface RowView {
  kind: RowKind
  pct: number
  failed: number
  phase: ImportPhase | null
  /* Whether a click asks for the run again: a stopped run resumes, a finished
     run with failures retries them, and both are the same call. */
  clickable: boolean
}

const HIDDEN: RowView = { kind: 'hidden', pct: 0, failed: 0, phase: null, clickable: false }

const readDismissed = (): string => {
  try { return window.localStorage.getItem(DISMISSED_KEY) ?? '' } catch { return '' }
}

const writeDismissed = (sig: string): void => {
  try { window.localStorage.setItem(DISMISSED_KEY, sig) } catch { /* a private window keeps nothing */ }
}

const initial = (): ImportSyncState => ({ status: null, starting: false, dismissed: readDismissed(), error: '' })

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
  const within = st?.current && st.current.total ? Math.min(1, st.current.sent / st.current.total) : 0
  const pct = total ? Math.min(100, Math.round(((settled + within) / total) * 100)) : 0
  if (s.starting && !st?.running) return { ...HIDDEN, kind: 'scan' }
  if (!st) return HIDDEN
  const phase = st.phase ?? null
  const phases = st.phases ?? null
  const again = resumable(st)
  if (st.running) {
    const phasePct = phase && phase.total ? Math.min(100, Math.round((phase.current / phase.total) * 100)) : pct
    return { kind: phase ? 'wrap' : 'run', pct: phase ? phasePct : pct, failed: st.failed, phase, clickable: false }
  }
  if (!total && !phases) return HIDDEN
  /* Short of the total: the message pass was stopped, or the gateway lost it. */
  if (settled < total) return { kind: 'paused', pct, failed: st.failed, phase: null, clickable: again }
  /* Settled, but the phases behind the pass never finished: no verdict on file
     for a run that recorded its request (lost before the phases began), or a
     verdict that says they were still running or were stopped. */
  const unfinished = phases === null ? again : phases.status === 'pending' || phases.status === 'cancelled'
  if (unfinished) return { kind: 'paused', pct: 100, failed: st.failed, phase: null, clickable: again }
  const failed = st.failed + (phases?.status === 'failed' ? phases.errors.length : 0)
  if (s.dismissed === signature(st)) return HIDDEN
  return { kind: 'done', pct: 100, failed, phase: null, clickable: failed > 0 && again }
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
    set({ status, error: '' })
    poll(status.running)
  } catch (e) {
    set({ error: failure(e) })
    poll(false)
  }
}

/* Ask for a run and follow it. The wizard's sync step goes through its own
   source for the same call and lets the close hook bring this store up to date;
   the rail's own click comes here. */
export async function start(platforms: string[], tier: ImportTier): Promise<ImportStarted> {
  set({ starting: true, error: '' })
  try {
    const r = await source().run(platforms, tier)
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
  store.set({ ...initial(), dismissed: '' })
}
