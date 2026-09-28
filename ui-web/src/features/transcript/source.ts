/* What the transcript reads that is not a turn event: how a tool result is
 * previewed and judged, the stored conversation a session open replays, and the
 * five verbs a delegated row offers.
 *
 * The previewing pair is wire knowledge -- the guards a tool result arrives
 * wrapped in, and what an old server's silence about success means -- and the
 * delegation verbs are chrome: which window a graph node or a spawn record
 * opens in. Neither is about the turn that is running, which is why neither is
 * in src/state/session/.
 */

import { t } from '../../i18n/t'
import { current as sessionCurrent, setCurrent as sessionSet } from '../../lib/session'
import { has } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'
import { unpitch } from '../../state/session/conversation'
import { open as sessionOpen, rows as sessionRows } from '../../state/session/rows'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'
import { openDeskTab, openDeskTask } from '../desk/store'
import { draw as sessionDraw } from '../rail/store'
import { history as drawHistory } from './mount'
import { actLabel as storeActLabel } from './store'

import type { SessRow } from '../rail/types'
import type { HistoryMessage } from './types'

/* Every delegation verb here is addressed to the conversation on screen, and
   the card that offers it only exists inside one. */
const openKey = (): string => sessionCurrent() as string

/* Tool results arrive wrapped in prompt-injection guards
   ([BEGIN UNTRUSTED ...] / [END UNTRUSTED ...]). Those markers protect the
   model, not the reader -- strip them from every preview. */
export function cleanPreview(text: unknown): string {
  return String(text || '')
    .split('\n')
    .filter((l) => !/^\s*\[(BEGIN|END) UNTRUSTED /.test(l))
    .join('\n')
    .trim()
}

/* A completed call's success is guessed from its result text until the wire
   carries a real flag (pending backend change): the registry stamps failed
   calls with its retry hint, error-shaped first lines count, and
   understand_media reports per-file failures inline. */
export function okOf(name: string, preview: string): boolean {
  if (preview.includes('[Analyze the error above')) return false
  if (/^\s*(error|traceback|failed)\b/i.test(preview)) return false
  if (name === 'understand_media' && preview.includes('[could not understand:')) return false
  return true
}

/* The same one-line-label table this island's own tool rows read, exposed so
   a sibling domain that draws its own tool calls (features/tasks) can ask for
   it through the seam rather than importing this island's private store. */
export const actLabel = (name: string, args: Record<string, unknown>, display?: string | null): string =>
  storeActLabel(name, args, display)

export function renderHistory(messages: HistoryMessage[]): void {
  /* Opening a stored conversation IS content: the new-task flag comes down
     before the island paints. Already down if the switch went through
     resetView; still needed for the reconnect replay, which repaints a
     conversation without leaving it. */
  unpitch()
  drawHistory(messages)
}

/* Opening the run's task pane on the desk. The tasks store rarely misses: a
   live run is inserted the moment `dag.run_started` announces it, and a
   session change re-reads `tasks.list` whole, so `openRun` below answers most
   of the time. The one-shot `one()` read covers a page that never opened the
   tasks tab at all; the tab itself is where a run this conversation does not
   own lands -- a branched conversation replays its parent's delivered row,
   and the run that row names belongs to a session whose `tasks.list` this
   one never reads. */
export function openDagRun(runId: string): void {
  const id = String(runId || '')
  if (id && ds('tasks').openRun?.(id)) return
  if (id) {
    ds('tasks').one('dag', id)
      .then((row) => { if (row) openDeskTask(row); else openDeskTab('tasks') })
      .catch(() => openDeskTab('tasks'))
    return
  }
  openDeskTab('tasks')
}

/* Per-node status for a card whose events are long gone: `dag.get` reads the
   run back off disk, reconciled against the registry, so a graph reopened from
   history shows what actually happened rather than a row of pending dots. */
/* The rows as the server sends them, unreduced. They used to be mapped down to
   four fields here, which is why a card restored from history could never show a
   dependency, a prompt template or an input -- a field this mapper did not name
   was a field the card could not have. The shape the card wants is decided by the
   adapter that reads it (ui-web/src/features/dag/nodes.ts), not by this seam. */
export const dagRun = (runId: string) =>
  gateway().call('dag.get', { run_id: runId, session_key: openKey() })
    .then((r) => (r && r.run) || {})

/* Every delegated call this conversation made. Read once per conversation, to
   find the record a restored card's run wrote (store.ts `resolveSpawn`). */
export function spawnList() {
  /* Guarded like sources.subagents.list is: a server without the subagent surface
   answers -32601, and a card that asked would then re-ask on every reopen for
   an answer that cannot arrive. */
  if (!has('subagent')) return Promise.resolve([])
  return gateway().call('subagent.list', { session_id: openKey() })
    .then((r) => (r && r.items) || [])
}

/* A spawn opens as its task -- the same pane a graph's card opens through
   `openDagRun` above, addressed by the record id the card and the delivered row
   both carry (the call's `node_id`).

   The row may not be in the tasks store yet when the card has just appeared:
   the pending frame files it under the run's task id, and only the running
   frame renames it to the record id. So the store is asked first, then the
   server for that one row, on a short ladder, and the tasks list is where a
   run that never turns up lands.

   Never the agents panel's instance window. Falling back to a label match there
   is what made the same click open two different windows depending on how soon
   after dispatch it came. */
export function openSpawn(nodeId: string): void {
  const id = String(nodeId || '')
  if (!id) { openDeskTab('tasks'); return }
  const attempt = (n: number): void => {
    if (ds('tasks').openByNode?.(id)) return
    ds('tasks').one('spawn', id)
      .then((row) => {
        if (row) { openDeskTask(row); return }
        if (n >= SPAWN_RETRIES) { openDeskTab('tasks'); return }
        setTimeout(() => attempt(n + 1), SPAWN_RETRY_MS)
      })
      .catch(() => openDeskTab('tasks'))
  }
  attempt(0)
}

const SPAWN_RETRIES = 4
const SPAWN_RETRY_MS = 700

/* Forking the open conversation. The island only offers it on the main lane, so
   a delegated run's pane never claims to fork a session it does not have. */
export function branch(): void {
  gateway().call('session.branch', { session_id: openKey() })
    .then((r) => {
      if (!r.session_id) { toast(t('gui.sess.branch_empty')); return }
      const s: SessRow = {
        id: r.session_id, title: r.title || t('gui.sess.branch_title'),
        last: t('gui.sess.branched'), when: t('gui.sess.just_now'),
        at: Math.floor(Date.now() / 1000), run: null, live: true,
      }
      sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s)
      toast(t('gui.sess.branched_n', { n: r.message_count || 0 }))
    })
    .catch((e) => toast(t('gui.op.branch_failed', { detail: (e as Error).message || e })))
}
