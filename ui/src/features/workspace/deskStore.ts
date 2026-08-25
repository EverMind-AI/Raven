/** State and actions for the floating workspace desk. */

import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'
import * as workspace from './store'

import type { AgentRow, InstanceRow } from '../subagents/types'
import type { DeskPane, DeskSplits, DeskState, DeskTab } from './deskTypes'
import type { WsChange } from './types'

const DESK_OPEN_KEY = 'raven.gui.desk.open'

function storedOpen(): boolean {
  try { return localStorage.getItem(DESK_OPEN_KEY) === 'true' } catch { return false }
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
    if (state.active !== pane.id || solo !== state.solo) update({ active: pane.id, solo })
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
  update({
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
  update({ paletteOpen: true, tab })
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

export function openDeskAgent(row: InstanceRow): void {
  /* A node opened before its instance row was known shows as the node's record;
     when the row arrives the panel promotes it, and the promoted view is the
     same work -- so it replaces that pane instead of opening beside it. */
  const supersedes = row.runId && row.nodeId ? recordIdOfNode(row.runId, row.nodeId) : null
  addPane({ id: `agent:${row.agent}:${row.handle}`, kind: 'agent', row }, supersedes)
}

export function openDeskAgentRecord(row: AgentRow): void {
  const identity = row.kind === 'dag' ? `${row.run_id}:${row.node}` : row.id
  addPane({ id: `agent-record:${identity || row.label || 'unknown'}`, kind: 'agent-record', row })
}

export function closePane(id: string): void {
  const panes = state.panes.filter((item) => item.id !== id)
  update({
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
  update({
    panes,
    active: state.active === id ? nextId : state.active,
    solo: state.solo === id ? nextId : state.solo,
  })
}

export function setActive(id: string): void {
  if (state.active !== id) update({ active: id })
}

export function toggleSolo(id: string): void {
  update({ solo: state.solo === id ? null : id })
}

export function updateSplits(patch: Partial<DeskSplits>): void {
  update({ splits: { ...state.splits, ...patch } })
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
  listeners.clear()
}
