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
import { gateway } from '../../state/gateway'
import { forget as forgetSubscription, switchToDraft } from '../../state/session/registry'
import { sources } from '../../state/sources'
import { islands } from '../../islands'
import { T } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { dropDraft } from '../composer/mount'
import { ask as confirmAsk } from '../../state/confirm'
import { forget as sheetsForget } from '../../state/sheetRack'
import { open as sessionOpen, replace as sessionReplace, rows as sessionRows } from '../../state/session/rows'
import { draw as sessionDraw } from './store'
import { renamedSession } from './source'

import type { SessRow } from './types'

/* ── leaving a row that is no longer on the rail ─────────────────────────── */

export async function leaveDeletedSession(sessionId: string): Promise<void> {
  dropDraft(sessionId)
  forgetSubscription(sessionId)
  sheetsForget(sessionId)
  islands.dag.forget(sessionId)
  const transition = islands.rail.removeRow(sessionRows(), sessionCurrent(), sessionId)
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
  const transition = islands.rail.removeRow(sessionRows(), sessionCurrent(), sessionId)
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

export function removeSession(s: SessRow): void {
  confirmAsk(T('gui.sess.delete_title'), T('gui.sess.delete_body', { title: s.title }), T('gui.sess.delete'), async () => {
    try {
      const r = await gateway().call('session.delete', { session_id: s.id })
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
      if (r.deleted !== s.id && !removed) throw new Error(T('gui.sess.delete_kept'))
      await leaveDeletedSession(s.id)
      toast(r.deleted === s.id
        ? T('gui.sess.deleted_x', { title: s.title })
        : T('gui.sess.delete_absent', { title: s.title }))
    } catch (e) { toast(T('gui.sess.delete_failed', { title: s.title, err: (e as Error).message || e })) }
  })
}

export async function archiveSession(s: SessRow): Promise<void> {
  try {
    const at = sessionRows().findIndex((row: SessRow) => row.id === s.id)
    const result = await gateway().call('session.archive', { session_id: s.id, archived: true })
    if (!result.archived || result.session_key !== s.id) throw new Error(`session ${s.id} was not archived`)
    await leaveArchivedSession(s.id)
    toast(T('gui.sess.archived', { title: s.title }), {
      label: T('gui.undo'),
      fn: async () => {
        try {
          const restored = await gateway().call('session.archive', { session_id: s.id, archived: false })
          if (restored.archived || restored.session_key !== s.id) {
            throw new Error(`session ${s.id} was not restored`)
          }
          if (!sessionRows().some((row: SessRow) => row.id === s.id)) {
            sessionRows().splice(Math.max(0, Math.min(at, sessionRows().length)), 0, s)
          }
          sessionDraw()
        } catch (e) {
          toast(T('gui.sess.restore_failed', { detail: detailOf(e) }))
        }
      },
    })
  } catch (e) {
    toast(T('gui.sess.archive_failed', { detail: detailOf(e) }))
  }
}

export async function deleteAllSessions(): Promise<void> {
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
      const r = await gateway().call('session.delete', { session_id: s.id })
      const removed = hasStillOnDisk(r) && r.still_on_disk === false
      if (r.deleted !== s.id && !removed) continue
      gone.push(s.id); dropDraft(s.id)
    } catch { /* counted by what is left below */ }
  }
  sessionReplace(sessionRows().filter((s: SessRow) => !gone.includes(s.id)))
  sessionSet(null)
  switchToDraft()
  islands.settings.redraw()
  toast(sessionRows().length
    ? T('gui.set.dat.del_partial', { n: gone.length, left: sessionRows().length })
    : T('gui.set.dat.del_done', { n: gone.length }))
}

/* The three the override layer installs together, and the two the turn and
   settings layers install beside them. Assigned onto the source rather than
   replacing it: the boot guard builds the object, with the rows it holds. */
export function installSessionActions(): void {
  const target = sources.sessions
  if (!target) return
  target.remove = removeSession
  target.archive = archiveSession
  target.renamed = renamedSession
}
