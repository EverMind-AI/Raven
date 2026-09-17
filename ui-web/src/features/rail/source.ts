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

import { current as sessionCurrent, setCurrent as sessionSet } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { gateway } from '../../state/gateway'
import { T } from '../../i18n/t'
import { $ } from '../../shell/dom'
import { replace as sessionReplace, rows as sessionRows, sess } from '../../state/session/rows'
import { draw as sessionDraw } from './store'
import { plainTitle } from './title'

import type { SessRow } from './types'

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
  const t = d.getTime()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const parts = { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() }
  const dated = d.getFullYear() === now.getFullYear()
    ? T('gui.time.md', parts)
    : T('gui.time.ymd', parts)
  if (t >= day0) return hm
  if (t >= day0 - DAY) return T('gui.time.yest')
  if (t >= day0 - 2 * DAY) return T('gui.time.dbyest')
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
      || T('gui.sess.fallback_title', { id: String(it.id).split(':').pop()!.slice(0, 15) }),
    last: (it.last_message_preview || it.preview)
      ? (it.last_message_preview || it.preview)!.slice(0, 60)
      : T('gui.sess.n_messages', { n: it.message_count }),
    when, at, run: null, live: true, from: cron ? 'cron' : undefined,
    pin: !!it.pinned, persisted: true,
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
export const pinSession = (id: string, pinned: boolean): Promise<void> =>
  gateway().call('session.pin', { session_id: id, pinned: !!pinned })
    .then(() => undefined)
    .catch((e) => {
      const s = sess(id)
      if (s) { s.pin = !pinned; sessionDraw() }
      toast(T('gui.sess.pin_failed', { detail: detailOf(e) }))
    })

/* Persist a manual rename made through the title editor. The editor is the
   rail island's, and this used to wrap its entry point to hang a blur listener
   off the input it had just created -- reaching into another layer's DOM, and
   missing an Enter, which replaces that input while it still has focus. The
   island tells us instead. */
export function renamedSession(id: string, title: string, previous: string): void {
  /* A refused rename must not stay quiet -- same reason `pinSession` puts its
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
    toast(T('gui.sess.rename_failed', { detail: detailOf(e) }))
  })
}
