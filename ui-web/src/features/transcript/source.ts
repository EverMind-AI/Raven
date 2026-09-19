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
import { session as sheetSession } from '../../state/sheetRack'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'
import { pane } from '../../state/wsPane'
import { run as dagRunOf } from '../dag/mount'
import { dagOpenNode } from '../dag/open'
import { openDeskTab } from '../desk/store'
import { draw as sessionDraw } from '../rail/store'
import { plainTitle } from '../rail/title'
import * as subagents from '../subagents/store'
import { history as drawHistory } from './mount'

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

export function renderHistory(messages: HistoryMessage[]): void {
  /* Opening a stored conversation IS content: the new-task flag comes down
     before the island paints. Already down if the switch went through
     resetView; still needed for the reconnect replay, which repaints a
     conversation without leaving it. */
  unpitch()
  drawHistory(messages)
}

/* Opening a delegated graph: the last node if this page already holds the run's
   own record, the agents panel otherwise. A source verb rather than a line
   inside the delivered handler, because the row a RELOAD draws has to open the
   same thing the live row does. */
export function openDagRun(runId: string): void {
  const id = String(runId || '')
  if (id) {
    const d = dagRunOf(sheetSession())
    const last = d && d.run_id === id ? d.order[d.order.length - 1] : null
    if (last) {
      /* With what the node is FOR, not only its id. The run holds every node's
       summary and this row handed over the slug alone, so a pane opened from
       the trail was headed by a name for the machine while the same node opened
       from the sheet was headed by what it did. */
      const n = d && d.nodes && d.nodes.get ? d.nodes.get(last) : null
      dagOpenNode(id, { id: last, summary: (n && n.node_summary) || null })
      return
    }
  }
  pane().setOpen(true, 'agents')
}

export const openDagNode = (runId: string, nodeId: string, summary?: string | null): void =>
  dagOpenNode(runId, { id: nodeId, summary })

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

/* One spawned run's messages so far. Answers a MOVING stream while the run is
   live: the record's own transcript.jsonl is only written when the run finishes,
   and until then `subagent.context` serves the activity collector's copy, which
   the acp backend republishes on every update it receives. */
export const spawnRecord = (callId: string) =>
  gateway().call('subagent.context', { id: callId, session_id: openKey() })

/* Every delegated call this conversation made. Read once per conversation, to
   turn a restored card's task id into the record id its stream is read by: a
   record's directory is `<stamp>-<task_id>`, so the row is found by suffix. */
export function spawnList() {
  /* Guarded like sources.subagents.list is: a server without the subagent surface
   answers -32601, and a card that asked would then re-ask on every reopen for
   an answer that cannot arrive. */
  if (!has('subagent')) return Promise.resolve([])
  return gateway().call('subagent.list', { session_id: openKey() })
    .then((r) => (r && r.items) || [])
}

/* "View in workspace" on a spawn row: open the panel on the run's own record,
   not just on the list. The list may not have caught the new run yet, so a
   couple of short retries cover the gap between the call and its row. */
export function openSpawn(agent: string, label: string, nodeId?: string): void {
  /* The tasks source's own row, when the wire named one: opening it directly
     is exact, where the label match below is a guess. The seam answers
     whether it opened, so there is nothing to fall back to once it does. */
  if (nodeId && ds('tasks').openByNode?.(nodeId)) return
  /* Same rule as dagOpenNode: `openRow` below raises the window, and in desk
   mode that is the whole answer. The panel's agents view is only needed where
   there are no windows. */
  if (!document.documentElement.classList.contains('desk-ready')) pane().setOpen(true, 'agents')
  const match = () => subagents.rows().find((x) => x.kind !== 'dag'
    && (!label || plainTitle(x.label) === plainTitle(label))
    && (!agent || (x.agent || 'raven') === (agent || 'raven')))
  const attempt = (n: number): void => {
    const it = match()
    if (it) { subagents.openRow(it); return }
    if (n >= 4) {
      /* Nothing was found, so nothing was opened -- and a click that opens
       nothing reads as broken. `refresh` keeps the drawn list on a failed
       read rather than emptying it, so a gateway hiccup or a label the
       registry spells differently lands here, and the reader is left with a
       list they can search by hand. Only on this branch: the palette beside
       a window the reader did get is the thing this whole change removes. */
      openDeskTab('tasks')
      return
    }
    subagents.refresh(true)
    setTimeout(() => attempt(n + 1), 700)
  }
  attempt(0)
}

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
