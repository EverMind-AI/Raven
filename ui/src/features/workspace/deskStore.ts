/** State and actions for the floating workspace desk. */

import { shell, t } from '../../shell/bridge'
import { slot } from '../../shell/persist'
import { current as currentSession } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import * as workspace from './store'

import type { AgentRow, InstanceRow } from '../subagents/types'
import type { DeskPane, DeskSplits, DeskState, DeskTab } from './deskTypes'
import type { WsChange } from './types'

const DESK_OPEN_KEY = 'raven.gui.desk.open'

function storedOpen(): boolean {
  try { return localStorage.getItem(DESK_OPEN_KEY) === 'true' } catch { return false }
}

/* What a reload needs to put the desk back: what the reader OPENED, in the
   order they opened it, and where the frame put it.
 *
 * An intent rather than the pane it produced -- a path rather than the `WsFile`,
 * an (agent, handle) rather than the row -- because a pane reads its own body
 * when it opens, and a stored copy of that body would come back as a file the
 * agent has since rewritten. Resuming is therefore replaying the opens, which
 * also means a resumed pane goes through exactly the code a clicked one does.
 *
 * A diff pane has no intent that can be replayed, and is left out. Its hunks are
 * the turn's own live tool events; the gateway cannot answer for them after the
 * fact, so the only way to bring one back would be to store the hunks -- the one
 * place this would keep content instead of a pointer. The change list a diff
 * pane belongs beside comes back empty for the same reason, so a lone restored
 * diff would be the odd one out on the desk rather than the reader's screen. */
export type DeskIntent =
  | { k: 'file'; path: string; dl?: string }
  | { k: 'agent'; agent: string; handle: string; run?: string; node?: string }
  | { k: 'record'; id?: string; run?: string; node?: string }

export interface DeskSaved {
  tab: DeskTab
  open: DeskIntent[]
  solo: string | null
  active: string | null
  splits: DeskSplits
}

const KEPT = slot<DeskSaved>('desk', 1)

/* The desk this conversation had before the page was replaced, if it had one. */
export const saved = (key: string): DeskSaved | null => KEPT.read(key)

const intentOf = (pane: DeskPane): DeskIntent | null => {
  if (pane.kind === 'file') {
    return { k: 'file', path: pane.file.path, ...(pane.file.downloadPath ? { dl: pane.file.downloadPath } : {}) }
  }
  if (pane.kind === 'agent') {
    return {
      k: 'agent',
      agent: pane.row.agent,
      handle: pane.row.handle,
      /* Carried when the instance is a graph node, because that is the pair the
         panel can reopen from nothing: the row itself may not be listed yet
         after a reload, and (run, node) names the work either way. */
      ...(pane.row.runId ? { run: pane.row.runId } : {}),
      ...(pane.row.nodeId ? { node: pane.row.nodeId } : {}),
    }
  }
  if (pane.kind === 'agent-record') {
    const row = pane.row
    if (row.kind === 'dag') {
      return row.run_id && row.node ? { k: 'record', run: row.run_id, node: row.node } : null
    }
    return row.id ? { k: 'record', id: row.id } : null
  }
  return null
}

/* Conversations whose desk is being replayed right now.
 *
 * A replay opens each window through the verb a click goes through, so it goes
 * through `commit` as well -- and the note must not be rewritten from the panes
 * that happen to be up part-way through it. Some intents wait on a list the
 * gateway has not answered yet, so a note taken mid-replay drops every one of
 * them, and the next reload then has nothing left to bring back: a page reloaded
 * twice in quick succession, or once while the gateway was unreachable, lost the
 * windows for good. A replay is therefore not a reader's mutation, and the note
 * it is replaying FROM stays exactly as it is until the reader touches the desk
 * themselves.
 *
 * A set keyed by conversation, not one flag: the reader can open another
 * conversation while a list is still in flight, and that one's own mutations
 * still have to be recorded. */
const REPLAYING = new Set<string>()

export function replaying(key: string, on: boolean): void {
  if (on) REPLAYING.add(key)
  else REPLAYING.delete(key)
}

/* Recorded from the reader's own mutations, and from nowhere else.
 *
 * `reset` is the counter-example that decides this: it empties the desk on every
 * session switch, and it runs BEFORE the session pointer moves, so a record
 * written from there would file an empty desk under the conversation being left
 * -- erasing what it is tearing down, at the one moment the reader most expects
 * to come back to it. */
function remember(): void {
  const key = currentSession()
  /* A draft has no id to file under and cannot be reopened, so its desk has
     nowhere to come back to. */
  if (!key || REPLAYING.has(key)) return
  KEPT.write(key, {
    tab: state.tab,
    open: state.panes.map(intentOf).filter((intent): intent is DeskIntent => !!intent),
    solo: state.solo,
    active: state.active,
    splits: state.splits,
  })
}

function initialState(): DeskState {
  return {
    tab: 'diff',
    paletteOpen: storedOpen(),
    panes: [],
    solo: null,
    active: null,
    splits: { column: 50, left: 50, right: 50 },
  }
}

let state = initialState()
const listeners = new Set<() => void>()

export const getState = (): DeskState => state

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function update(patch: Partial<DeskState>): void {
  state = { ...state, ...patch }
  listeners.forEach((listener) => listener())
}

/* One change the reader made, recorded so a reload can replay it. */
function commit(patch: Partial<DeskState>): void {
  update(patch)
  remember()
}

/* Only when it is not already showing. Opening the workspace is not a cheap
   setter: it runs a full workspace draw, which unmounts and rebuilds the legacy
   panel's islands. Every pane opened, and every click on a row whose pane was
   already up, paid for that -- invisibly, since in desk mode the panel it
   rebuilds is display:none. The split's own flag is the honest answer to "is it
   open", the same way the rail reads the app's page flag. */
function revealWorkspace(): void {
  const split = document.getElementById('split')
  if (split && split.dataset.open === 'true') return
  shell().workspaceSetOpen?.(true)
}

/* `supersedes` names a pane this one REPLACES rather than joins: the same work
   reached a second way must land in the slot the first one took, or the reader
   ends up with two panes showing one node. */
function addPane(pane: DeskPane, supersedes?: string | null): void {
  const same = state.panes.findIndex((item) => item.id === pane.id)
  /* Already on the desk, and holding nothing a replacement could refresh: bring
     it forward and stop. Re-opening it replaced the pane object and re-tiled the
     grid to arrive at exactly the screen that was already there, remounting the
     pane body on the way -- a click that looked inert but was not.

     Narrowed to these two kinds because only their payload is re-read: an
     `agent` pane re-finds its row in `state.instances` on every render and an
     `agent-record` pane in `state.rows` (SubagentsPage.tsx), so the object held
     here is never staler than the one that would replace it. A `file` pane is
     the opposite -- `FileView` renders `workspace.makeFile(...)` directly, so
     the replacement is how a re-open picks up a download path the first caller
     did not pass and how the body re-reads a file the agent has since rewritten
     (`FileBody` is keyed on `seq` and fetches only while `text` is null). A
     `diff` pane likewise holds its own change. Both fall through. */
  if (same >= 0 && !supersedes && (pane.kind === 'agent' || pane.kind === 'agent-record')) {
    /* The fullscreen still has to give way when it is showing a DIFFERENT pane,
       or the one just asked for stays hidden behind it with no way out but the
       toggle -- `DeskSurface` draws only the soloed pane while solo is set.
       Same rule the full path applies below, and it has to be applied here too:
       returning before it is what made the click genuinely inert. */
    const solo = state.solo === pane.id ? state.solo : null
    if (state.active !== pane.id || solo !== state.solo) commit({ active: pane.id, solo })
    revealWorkspace()
    return
  }
  const slot = same >= 0
    ? same
    : supersedes ? state.panes.findIndex((item) => item.id === supersedes) : -1
  let panes: DeskPane[]
  if (slot >= 0) panes = state.panes.map((item, index) => index === slot ? pane : item)
  else {
    if (state.panes.length >= 4) {
      toast(t('gui.ws.desk_limit'))
      return
    }
    panes = [...state.panes, pane]
  }
  commit({
    panes,
    /* A pane taking the fullscreen pane's own slot keeps the fullscreen: the
       reader did not ask to come back out. Anything else is a new thing to
       look at, and looking at it means leaving the one screen that hides it. */
    solo: state.solo && (state.solo === pane.id || state.solo === supersedes) ? pane.id : null,
    active: pane.id,
  })
  revealWorkspace()
}

export function toggleDesk(): void {
  const paletteOpen = !state.paletteOpen
  try { localStorage.setItem(DESK_OPEN_KEY, String(paletteOpen)) } catch {}
  update({ paletteOpen })
}

export function openDeskTab(tab: DeskTab): void {
  commit({ paletteOpen: true, tab })
}

export function openDeskFile(path: string, downloadPath?: string): void {
  addPane({ id: `file:${path}`, kind: 'file', file: workspace.makeFile(path, downloadPath) })
}

export function openDeskDiff(change: WsChange): void {
  addPane({ id: `diff:${change.key}:${change.turn}`, kind: 'diff', change })
}

/* The id openDeskAgentRecord gives a graph node's own record, so an instance
   that turns out to BE that node can take its place. */
const recordIdOfNode = (runId: string, node: string): string => `agent-record:${runId}:${node}`

export function openDeskAgent(row: InstanceRow, recordId?: string | null): void {
  /* A node opened before its instance row was known shows as the node's record;
     when the row arrives the panel promotes it, and the promoted view is the
     same work -- so it replaces that pane instead of opening beside it.

     A plain spawn gets the same promotion but cannot be derived here: its
     record is identified by the call id, which `InstanceRow` has no field for
     (`runId` and `nodeId` name a graph node, and a spawn has neither). The
     promoting store knows it, so it hands it over. Without this the record
     pane -- the composer-less one the reader was being moved off -- stayed on
     the desk beside the instance pane, which is the whole thing this
     promotion exists to avoid. */
  const supersedes = recordId
    ? `agent-record:${recordId}`
    : (row.runId && row.nodeId ? recordIdOfNode(row.runId, row.nodeId) : null)
  addPane({ id: `agent:${row.agent}:${row.handle}`, kind: 'agent', row }, supersedes)
}

export function openDeskAgentRecord(row: AgentRow): void {
  const identity = row.kind === 'dag' ? `${row.run_id}:${row.node}` : row.id
  addPane({ id: `agent-record:${identity || row.label || 'unknown'}`, kind: 'agent-record', row })
}

export function closePane(id: string): void {
  const panes = state.panes.filter((item) => item.id !== id)
  commit({
    panes,
    solo: state.solo === id ? null : state.solo,
    active: state.active === id ? panes[panes.length - 1]?.id || null : state.active,
  })
  if (!panes.length) shell().workspaceSetOpen?.(false)
}

export function replacePaneFile(id: string, path: string): void {
  const nextId = `file:${path}`
  const panes = state.panes
    .filter((pane) => pane.id === id || pane.id !== nextId)
    .map((pane) => pane.id === id && pane.kind === 'file'
      ? { ...pane, id: nextId, file: workspace.makeFile(path) }
      : pane)
  commit({
    panes,
    active: state.active === id ? nextId : state.active,
    solo: state.solo === id ? nextId : state.solo,
  })
}

export function setActive(id: string): void {
  if (state.active !== id) commit({ active: id })
}

export function toggleSolo(id: string): void {
  commit({ solo: state.solo === id ? null : id })
}

export function updateSplits(patch: Partial<DeskSplits>): void {
  commit({ splits: { ...state.splits, ...patch } })
}

/* A stored split is a percentage the surface hands straight to a CSS
   template, so a value that is not one is not a layout to argue with. */
const pct = (v: unknown, fallback: number): number =>
  typeof v === 'number' && isFinite(v) && v >= 10 && v <= 90 ? v : fallback

/* Where the frame was, applied once the opens have been replayed: every
   `addPane` makes itself the active pane, so the reader's own front pane and
   fullscreen can only be put back after the last of them is up.
   Records nothing, like the opens it follows: it is applying a note, not
   writing one. */
export function applyLayout(kept: DeskSaved): void {
  const here = (id: string | null): boolean => !!id && state.panes.some((pane) => pane.id === id)
  update({
    tab: kept.tab,
    /* Only onto a pane that actually came back. A solo id naming a pane whose
       intent could not be replayed would leave the desk fullscreen on nothing --
       `DeskSurface` draws the soloed pane and only it. */
    solo: here(kept.solo) ? kept.solo : null,
    active: here(kept.active) ? kept.active : state.active,
    splits: {
      column: pct(kept.splits?.column, 50),
      left: pct(kept.splits?.left, 50),
      right: pct(kept.splits?.right, 50),
    },
  })
}

export function notifyDesk(): void {
  update({})
}

export function reset(): void {
  state = initialState()
  listeners.forEach((listener) => listener())
}

export function _resetForTests(): void {
  state = initialState()
  KEPT.clear()
  REPLAYING.clear()
  listeners.clear()
}
