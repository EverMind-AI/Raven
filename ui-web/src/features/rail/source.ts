/* The rail's own data: what a listed session looks like as a row, and the
 * things a reader can do TO one.
 *
 * The shaping is pure -- a stamp with four branches, a title with four
 * fallbacks, a scheduler wrapper the row has to see past -- and the two writes
 * here are the ones a row makes for itself: a pin and a rename, which move the
 * row optimistically and put it back when the server refuses. The writes that
 * also move the reader somewhere else are ./leave.ts, and nothing about the
 * turn running inside a conversation is here at all: that belongs to its
 * `SessionRuntime` (src/state/session/).
 */

import { t } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { switchTo } from '../../state/session/registry'
import { replace as sessionReplace, rows as sessionRows, sess } from '../../state/session/rows'
import { show as toast } from '../../state/toast'
import { busy } from '../composer/turn'
import { draw as sessionDraw } from './store'
import { plainTitle } from './title'

import type { ResultOf } from '../../rpc/generated'
import type { RailSource, SessRow } from './types'

const DAY = 86400000

/* A row's stamp says when its visible conversation last changed, so it carries a
   clock -- a bare date cannot tell two of yesterday's sessions apart. The year
   only appears once it is not this one; inside the current year it is noise. */
/* A clock only earns its place on today's rows: further back, the day is what
   the reader is placing the session by, and "yesterday 22:07" spends four
   characters saying something they did not ask. */
export function whenLabel(epochS: number): string {
  const d = new Date(epochS * 1000)
  const now = new Date()
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const at = d.getTime()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const parts = { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() }
  const dated = d.getFullYear() === now.getFullYear()
    ? t('gui.time.md', parts)
    : t('gui.time.ymd', parts)
  if (at >= day0) return hm
  if (at >= day0 - DAY) return t('gui.time.yest')
  if (at >= day0 - 2 * DAY) return t('gui.time.dbyest')
  return dated
}

/* Scheduled runs live in cron:<job_id> sessions with no title of their own;
   the job's name is what the user recognises, so the list borrows it. */
let cronNames: Record<string, string> = {}

export async function loadCronNames(): Promise<void> {
  try {
    const r = await gateway().call('cron.list', {})
    const next: Record<string, string> = {}
    ;(r.jobs || []).forEach((j) => { next[j.id] = j.name })
    cronNames = next
  } catch { /* keep whatever we had */ }
}

interface ListedSession {
  id: string
  title?: string | null
  preview?: string | null
  last_message_preview?: string | null
  message_count?: number
  source?: string | null
  started_at?: number
  updated_at?: number
  pinned?: boolean
  workdir?: string | null
}

export function rowFrom(it: ListedSession): SessRow {
  /* Conversation activity, not creation: human messages, assistant replies,
     and runtime-injected visible content all move the row by the same clock. */
  const at = it.updated_at || it.started_at || 0
  const when = whenLabel(at)
  const cron = it.source === 'cron'
  const jobId = cron ? String(it.id).split(':').pop()! : null
  /* Cron turns open with the scheduler's "[Scheduled Task] Timer ..." wrapper;
     the task's own words start after "Task '". Rows for deleted jobs (no name
     to borrow) fall back to that inner text rather than the wrapper. */
  let prev = (it.preview || '').trim()
  if (cron) {
    const m = prev.match(/Task '([^']+)'/)
    prev = m ? m[1]! : prev.replace(/^\[Scheduled Task\]\s*/, '')
  }
  return {
    id: it.id,
    title: (cron && jobId ? cronNames[jobId] : '') || it.title || prev.slice(0, 24)
      || t('gui.sess.fallback_title', { id: String(it.id).split(':').pop()!.slice(0, 15) }),
    last: (it.last_message_preview || it.preview)
      ? (it.last_message_preview || it.preview)!.slice(0, 60)
      : t('gui.sess.n_messages', { n: it.message_count }),
    when, at, run: null, live: true, from: cron ? 'cron' : undefined,
    pin: !!it.pinned, persisted: true,
    workdir: it.workdir || null,
  }
}

export const rowPreview = (text: unknown): string =>
  String(text || '').trim().split('\n')[0]!.trim().slice(0, 60)

/* "Recent" means the latest visible conversation change. Human sends stamp
   immediately; completions stamp again when their visible result arrives. */
export function touchSession(id: string | null, preview?: string): void {
  const s = sess(id)
  if (!s) return
  const last = rowPreview(preview)
  if (last) s.last = last
  s.at = Math.floor(Date.now() / 1000)
  s.when = whenLabel(s.at)
  sessionRows().sort((a: SessRow, b: SessRow) => (b.at || 0) - (a.at || 0))
  sessionDraw()
}

export const SESS_CHANNELS = ['tui', 'cron']

export async function loadSessions(): Promise<void> {
  await loadCronNames()
  const r = await gateway().call('session.list', { channels: SESS_CHANNELS })
  sessionReplace((r.sessions || []).map(rowFrom).sort((a, b) => (b.at || 0) - (a.at || 0)))
  /* Replacing the rows is not showing them, which every other writer here
     already knew: leave.ts and the registry both draw after their replace. A
     restore from the archive page reaches the rail only through this function,
     so without the draw the row came back on the server and on disk and stayed
     off the screen until the page was reloaded. At boot the rail is still held,
     where a draw is the skeleton and `releaseRail` paints the rows. */
  sessionDraw()
}

/* ── the two writes a row makes for itself ───────────────────────────────── */

const detailOf = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string } | null
  return (o && ((o.data && o.data.detail) || o.message)) || String(e)
}

/* A refused persist must not stay quiet. The row moves optimistically, but a
   pin the server never accepted looks identical to one it did until the page
   is reloaded and the group is simply gone -- which is exactly how an older
   resident gateway, with no session.pin to call at all, presents itself. Put
   the row back and say so. */
export const pin = (id: string, pinned: boolean): Promise<void> =>
  gateway().call('session.pin', { session_id: id, pinned: !!pinned })
    .then(() => undefined)
    .catch((e) => {
      const s = sess(id)
      if (s) { s.pin = !pinned; sessionDraw() }
      toast(t('gui.sess.pin_failed', { detail: detailOf(e) }))
    })

/** Delete one conversation. What the answer means is ./leave.ts's decision. */
export const deleteSession = (id: string): Promise<ResultOf<'session.delete'>> =>
  gateway().call('session.delete', { session_id: id })

/** Hide it from the rail, or put it back. */
export const setArchived = (id: string, archived: boolean): Promise<ResultOf<'session.archive'>> =>
  gateway().call('session.archive', { session_id: id, archived })

/* Persist a manual rename made through the title editor. The editor is the
   rail island's, and this used to wrap its entry point to hang a blur listener
   off the input it had just created -- reaching into another layer's DOM, and
   missing an Enter, which replaces that input while it still has focus. The
   island tells us instead. */
export function renamed(id: string, title: string, previous: string): void {
  /* A refused rename must not stay quiet -- same reason `pin` puts its
   flag back. The row moved optimistically, so a name the server rejected (too
   long for the metadata record) looks identical to one it took until the page
   is reloaded and the old name is simply back. */
  gateway().call('session.title', { session_id: id, title }).catch((e) => {
    const s = sess(id)
    if (s) { s.title = previous; sessionDraw() }
    if (id === sessionCurrent()) {
      const h = $('#title')
      if (h) h.textContent = plainTitle(previous)
    }
    toast(t('gui.sess.rename_failed', { detail: detailOf(e) }))
  })
}

/* Test seam only: the job names read once at boot, and the rows the seam
   hands out, are both the module's. */
export function _resetForTests(): void {
  cronNames = {}
  rows = []
}

/* The rows the rail draws, held here because the page holds them: the list is
   one answer to `session.list`, read back by every draw and replaced whole by
   the next answer. */
let rows: SessRow[] = []

/* The seam object, built as one thing rather than grown by five installs: every
   verb on it has a home of its own in this domain. The three writes that also
   move the reader somewhere else are ./leave.ts's, and it assigns those
   (installSessionActions) rather than this file importing it back. */
export const sessionsSource: RailSource = {
  snapshot: () => ({ rows, cur: sessionCurrent(), busy: busy() }),
  replace: (next) => { rows = next },
  open: (s) => switchTo(s),
  pin,
  renamed,
}
