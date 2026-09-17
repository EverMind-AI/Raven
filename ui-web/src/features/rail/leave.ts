/* Leaving a conversation that is no longer on the rail, and the three writes
 * that can cause one: delete, archive with an undo, and the settings page's
 * wipe.
 *
 * Apart from ./source.ts because each of these ends somewhere else: a row that
 * goes takes the reader with it when it was the open one, which is the session
 * registry's business and not the rail's own data.
 */

import { hasStillOnDisk } from '../../rpc/capabilities'
import { current as sessionCurrent, setCurrent as sessionSet } from '../../lib/session'
import { show as toast } from '../../state/toast'
import { forget as forgetSubscription, switchToDraft } from '../../state/session/registry'
import { sources } from '../../state/sources'
import { forget as forgetDagRuns } from '../dag/mount'
import { redraw as redrawSettings } from '../settings/store'
import { t } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { dropDraft } from '../composer/mount'
import { ask as confirmAsk } from '../../state/confirm'
import { forget as sheetsForget } from '../../state/sheetRack'
import { open as sessionOpen, replace as sessionReplace, rows as sessionRows } from '../../state/session/rows'
import { draw as sessionDraw, removeSessionRow } from './store'
import { deleteSession, renamed, setArchived } from './source'

import type { SessRow } from './types'

/* ── leaving a row that is no longer on the rail ─────────────────────────── */

export async function leaveDeletedSession(sessionId: string): Promise<void> {
  dropDraft(sessionId)
  forgetSubscription(sessionId)
  sheetsForget(sessionId)
  forgetDagRuns(sessionId)
  const transition = removeSessionRow(sessionRows(), sessionCurrent(), sessionId)
  sessionReplace(transition.rows)
  if (transition.kind === 'unchanged') { sessionDraw(); return }
  if (transition.kind === 'open') {
    sessionSet(transition.next!.id)
    await sessionOpen(transition.next!)
    return
  }
  const ta = $('#ta') as HTMLTextAreaElement | null
  if (ta) ta.value = ''
  switchToDraft()
}

export async function leaveArchivedSession(sessionId: string): Promise<void> {
  const transition = removeSessionRow(sessionRows(), sessionCurrent(), sessionId)
  sessionReplace(transition.rows)
  if (transition.kind === 'unchanged') { sessionDraw(); return }
  if (transition.kind === 'open') {
    sessionSet(transition.next!.id)
    await sessionOpen(transition.next!)
    return
  }
  const ta = $('#ta') as HTMLTextAreaElement | null
  if (ta) ta.value = ''
  switchToDraft()
}

/* ── the writes the rail offers ──────────────────────────────────────────── */

const detailOf = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string } | null
  return (o && ((o.data && o.data.detail) || o.message)) || String(e)
}

export function remove(s: SessRow): void {
  confirmAsk(t('gui.sess.delete_title'), t('gui.sess.delete_body', { title: s.title }), t('gui.sess.delete'), async () => {
    try {
      const r = await deleteSession(s.id)
      /* A null `deleted` is two answers and only one of them may drop the row.
       Nothing left to remove -- an unknown key, or a conversation whose first
       turn never saved a file -- is the reader's own goal, so the row goes; a
       file that survived its removal is still there to list, and claiming it
       is gone is the one thing this rail must never do.

       `hasStillOnDisk` is the version half: a server too old to carry the
       field says nothing at all, and "nothing at all" is not "nothing was
       there" -- reading it as the second would drop a row whose file may
       have survived. */
      const removed = hasStillOnDisk(r) && r.still_on_disk === false
      if (r.deleted !== s.id && !removed) throw new Error(t('gui.sess.delete_kept'))
      await leaveDeletedSession(s.id)
      toast(r.deleted === s.id
        ? t('gui.sess.deleted_x', { title: s.title })
        : t('gui.sess.delete_absent', { title: s.title }))
    } catch (e) { toast(t('gui.sess.delete_failed', { title: s.title, err: (e as Error).message || e })) }
  })
}

export async function archive(s: SessRow): Promise<void> {
  try {
    const at = sessionRows().findIndex((row: SessRow) => row.id === s.id)
    const result = await setArchived(s.id, true)
    if (!result.archived || result.session_key !== s.id) throw new Error(`session ${s.id} was not archived`)
    await leaveArchivedSession(s.id)
    toast(t('gui.sess.archived', { title: s.title }), {
      label: t('gui.undo'),
      fn: async () => {
        try {
          const restored = await setArchived(s.id, false)
          if (restored.archived || restored.session_key !== s.id) {
            throw new Error(`session ${s.id} was not restored`)
          }
          if (!sessionRows().some((row: SessRow) => row.id === s.id)) {
            sessionRows().splice(Math.max(0, Math.min(at, sessionRows().length)), 0, s)
          }
          sessionDraw()
        } catch (e) {
          toast(t('gui.sess.restore_failed', { detail: detailOf(e) }))
        }
      },
    })
  } catch (e) {
    toast(t('gui.sess.archive_failed', { detail: detailOf(e) }))
  }
}

export async function deleteAll(): Promise<void> {
  const gone: string[] = []
  for (const s of sessionRows().slice() as SessRow[]) {
    try {
      /* A refusal is a SUCCESSFUL response, not a rejection: `session.delete`
       answers `{deleted: null, still_on_disk: true}` for a removal the
       filesystem refused. Awaiting alone caught only the transport failures,
       so a refusal counted as a removal -- which is exactly what the note
       above forbids, and the row came back on the next reload.

       The predicate is `removeSession`'s, character for character, because
       the two must not disagree about one answer: drop the row when the file
       was removed, and when there was nothing to remove; keep it when the
       file survived, or when a server too old to carry the field leaves the
       question open. */
      const r = await deleteSession(s.id)
      const removed = hasStillOnDisk(r) && r.still_on_disk === false
      if (r.deleted !== s.id && !removed) continue
      gone.push(s.id); dropDraft(s.id)
    } catch { /* counted by what is left below */ }
  }
  sessionReplace(sessionRows().filter((s: SessRow) => !gone.includes(s.id)))
  sessionSet(null)
  switchToDraft()
  redrawSettings()
  toast(sessionRows().length
    ? t('gui.set.dat.del_partial', { n: gone.length, left: sessionRows().length })
    : t('gui.set.dat.del_done', { n: gone.length }))
}

/* The three writes that also move the reader somewhere else. Assigned onto the
   source rather than replacing it: ./source.ts builds the object, with the rows
   it holds, and this module is what imports that one rather than the reverse. */
export function installSessionActions(): void {
  const target = sources.sessions
  if (!target) return
  target.remove = remove
  target.archive = archive
  target.deleteAll = deleteAll
}
