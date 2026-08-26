/** State and actions for the floating workspace desk. */

import { shell, t } from '../../shell/bridge'
import { slot } from '../../shell/persist'
import { current as currentSession } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { instanceState } from '../subagents/history'
import * as agents from '../subagents/store'
import * as deliveries from './deliveries'
import * as seen from './seen'
import * as palette from './palette'
import * as workspace from './store'

import type { AgentRow, InstanceRow } from '../subagents/types'
import type { DeskDuo, DeskPane, DeskSplits, DeskState, DeskTab } from './deskTypes'
import type { WsChange } from './types'

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
  /* Absent from a note written before the field existed, which reads back as
     the stack every desk was until then. */
  duo?: DeskDuo
}

/* Version 2: the `file` tab this note used to be able to name is retired, and a
   stored copy carries no way to say which bundle wrote it. Read back under v1 it
   restored a tab that no longer exists -- the palette then drew no tab as
   selected and fell through to the agents list. */
const KEPT = slot<DeskSaved>('desk', 2)

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
    duo: state.duo,
  })
}

function initialState(): DeskState {
  return {
    tab: 'deliverables',
    paletteOpen: palette.read(currentSession()),
    panes: [],
    solo: null,
    active: null,
    splits: { column: 50, left: 50, right: 50 },
    duo: 'rows',
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

/* The tabs, in the order they are drawn, which is also the order `pickTab`
   ranks them in. Deliverables first because a file handed over is what the
   session produced; then the work it delegated; then what it edited on the
   way. */
const TABS: readonly DeskTab[] = ['deliverables', 'agents', 'diff']

/* What each tab is counting, as the identity of every item in it -- read from
   the source that tab draws from, so "what is in it" and "what is new in it"
   can never disagree: the changes this session made, the files it delivered,
   the background work it started.

   Each id is the key that source already uses for the item, so a door that
   opens ONE thing can name the same thing this does: a change is its path and
   the turn that made it, a delivery is its path, an instance is its handle
   under its agent. */
export function idsOf(tab: DeskTab): string[] {
  if (tab === 'diff') return workspace.shared().changes.map((c) => `${c.key}:${c.turn}`)
  if (tab === 'deliverables') return deliveries.paths()
  return agents.instances().map((it) => `${it.agent}:${it.handle}`)
}

/* What is in this tab that the reader has not seen.

   Zero for the tab that is showing, said here rather than left to the record
   catching up: the record moves in an effect, so between something landing and
   that effect running there is a frame where the open tab could paint a bubble
   for what is already on screen.

   Qualified by `showing()`, which the count this replaced was not -- it argued
   that a shut palette draws no bubble, so the answer could not be observed. The
   launcher draws one now and the argument died with it: collapsed on the shelf
   while a file was delivered, this answered 0 for the one tab the reader is
   most likely to have left in front of them. */
export function unseen(tab: DeskTab): number {
  if (showing() && state.tab === tab) return 0
  const known = seen.of_(tab)
  return idsOf(tab).filter((id) => !known.has(id)).length
}

/* Everything the desk has to say, for the launcher: while the palette is down
   the launcher stands in for all three tabs, so it carries their sum. */
export const unseenAll = (): number => TABS.reduce((n, tab) => n + unseen(tab), 0)

/* Whether any delegated run is still going -- the launcher's other signal, and
   the one that is not a count: three running and one running ask the same thing
   of the reader. Through the subagents panel's own predicate rather than a
   second reading of `status`, because two spellings of "live" is how a row
   comes to breathe on one screen and sit still on another. */
export const working = (): boolean =>
  agents.instances().some((it) => instanceState(it.status ?? undefined) === 'live')

/* One item the reader opened, wherever they opened it from. The desk is not the
   only door: a delivered file has its own card in the conversation, and so does
   a delegated run. Opening the thing a tab counts is what makes that thing stop
   being new, so the doors say it here rather than each tab inventing its own
   idea of when to stop counting. */
export function readItem(tab: DeskTab, id: string): void {
  if (seen.mark(tab, [id])) update({})
}

/* The reader is looking at this tab, so nothing in it is new any more. Called
   while the palette is showing -- including as things land underneath it, which
   is why it runs on every render of the open palette and not only on a switch. */
export function seeTab(tab: DeskTab): void {
  /* `update`, not `commit`: the seen record keeps its own per-conversation
     store (seen.ts), and `remember()` would republish the whole desk note from
     whatever is on screen at that instant -- during a resume, nothing. */
  if (seen.mark(tab, idsOf(tab))) update({})
}

/* Whether the palette is on screen, which is not the same question as whether
   the reader left it open. A fullscreen pane IS the window -- there is no
   column for the desk to hang off and nothing of it should be over the pane --
   so the desk is not showing while one is up.
 *
 * One answer, because more than the rendering reads it: the marks are moved
 * from the same flag, and a bubble cleared behind a fullscreen pane is news the
 * reader never saw. */
export const showing = (): boolean => state.paletteOpen && !state.solo

/* Which tab the desk should come up on.
 *
 * Not the one it was left on: the reader collapsed the desk and went back to
 * the conversation, and what they want when they reach for it again is
 * whatever happened while it was down. The ladder is the tab order -- news
 * first, then anything at all, then the shelf as the answer for a conversation
 * that has produced nothing yet.
 *
 * Read BEFORE `paletteOpen` flips, and that ordering is load-bearing: `unseen`
 * exempts the tab that is showing, so deciding afterwards exempts whichever tab
 * was remembered from its own rung. With the shelf remembered -- the common
 * case, since it is also the fallback -- the desk still landed on the shelf and
 * the rung it took was the fallback, so a rule that never fired looked exactly
 * like one that always did. */
export function pickTab(): DeskTab {
  if (unseen('deliverables') > 0) return 'deliverables'
  if (unseen('agents') > 0) return 'agents'
  /* Existence, not news, and only on this rung: with nothing new anywhere, what
     the session has been editing is the most useful thing to be looking at.
     Above it the test is "unseen" twice, so an old delegated run does not keep
     winning the desk from a change that just landed. */
  if (idsOf('diff').length > 0) return 'diff'
  return 'deliverables'
}

export function toggleDesk(): void {
  const paletteOpen = !state.paletteOpen
  const tab = paletteOpen ? pickTab() : state.tab
  /* Filed under the conversation, so this conversation is how the reader left
     it the next time they open it (palette.ts). */
  palette.write(currentSession(), paletteOpen)
  /* `commit`, so the tab the ladder picked is the tab a reload comes back to.
     With `update` the note kept whatever the reader had LEFT the desk on, and a
     reload landed there instead of on what was in front of them -- self-
     correcting on the next pane they opened, which is worse than either
     answer for being intermittent. Safe from here in a way it is not from
     `seeTab`: this is the reader's own hand on their own desk, never a
     mark moving mid-replay. */
  commit({ paletteOpen, tab })
}

function applyPalette(key: string | null): void {
  const paletteOpen = palette.read(key)
  if (paletteOpen !== state.paletteOpen) update({ paletteOpen })
}

/* The conversation changed under the desk, so the palette's own answer did too.
 *
 * Driven from the session pointer (main.tsx) rather than from `reset`, and that
 * is the whole reason this exists: a reset runs on the way OUT -- before the
 * pointer moves, while `session.resume` is still in flight -- so it cannot know
 * whose desk is about to be on screen. The pointer moving is the one event that
 * does, and every way of arriving somewhere goes through it. */
export function sync(): void {
  applyPalette(currentSession())
}

/* The draft on screen just became this conversation.
 *
 * Announced by the page that performs it -- main.tsx binds this beside the
 * composer's claim on its own draft, the one legacy call that already says
 * this -- rather than inferred here. At the pointer, "the draft became this
 * session" and "the reader opened this session while a draft was up" are the
 * same move, null to an id, and three sites make the second one: opening a
 * conversation from the rail, forking one, and opening a cron run (the last two
 * move the pointer BEFORE the desk is reset, so no ordering rule separates
 * them either). Inferring it therefore carried an answer about the new-task
 * screen into a forked session and into a cron run. */
export function claimDraft(key: string | null): void {
  palette.adopt(key)
  applyPalette(key)
}

/* The callers are the legacy shell's untyped `wsPick`/`showWorkspace`, which
   forward whatever view name they were given. An unknown one used to fall
   through the palette's own switch and draw the agents list, so it is stopped
   here instead: the tab does not change and the palette still opens. */
export function openDeskTab(tab: DeskTab): void {
  /* Asking for a view of the desk is asking for the desk, so it outranks a
     collapse this conversation had on file. */
  palette.write(currentSession(), true)
  commit({ paletteOpen: true, ...(TABS.includes(tab) ? { tab } : {}) })
}

/* Every way of opening a file lands here -- the shelf's row, the delivery card
   in the conversation, a changed file's row there, an attachment chip, and the
   replay a reload does -- because they all go through `workspace.showFile`. So
   this is where a delivery stops being new, whichever of them the reader used.

   The shelf only, never the diff list, and that is a decision rather than an
   omission: what opens here is the file as it stands now, which is not the
   change some turn made to it. Clearing a change from this would quietly retire
   a hunk the reader never read, on the strength of them opening the file for
   some other reason. A change is marked read where a change is actually shown
   -- `openDeskDiff`. */
export function openDeskFile(path: string, downloadPath?: string): void {
  /* Only a path the shelf actually carries. This door is also every other way
     of opening a file -- an attachment chip, a changed file's row, the replay a
     reload does -- and a record of having "seen" a file no tab counts is a
     record that can only grow. Safe to gate on the shelf being loaded, because
     a card the reader can click is a card whose row the same turn event put
     there. */
  if (deliveries.paths().includes(path)) readItem('deliverables', path)
  addPane({ id: `file:${path}`, kind: 'file', file: workspace.makeFile(path, downloadPath) })
}

export function openDeskDiff(change: WsChange): void {
  readItem('diff', `${change.key}:${change.turn}`)
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
  /* Reached from the panel's own list and from the conversation's card alike --
     both come through `subagents.openInstance` -- so this is the one place that
     has to say the reader has now seen this run.

     `openDeskAgentRecord` needs no such line: a record is a spawn or a graph
     node, and neither is in the instance list the agents tab counts, so there
     is nothing there for opening one to clear. */
  readItem('agents', `${row.agent}:${row.handle}`)
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

export function setActive(id: string): void {
  if (state.active !== id) commit({ active: id })
}

export function toggleSolo(id: string): void {
  commit({ solo: state.solo === id ? null : id })
}

export function updateSplits(patch: Partial<DeskSplits>): void {
  commit({ splits: { ...state.splits, ...patch } })
}

/* The desk a drop leaves behind: the same panes, in the order and orientation
   the reader chose. Refused unless `order` names exactly the panes that are up,
   each once -- the caller is working from rects it measured a frame ago, and a
   pane that closed mid-drag must not make the drop scramble what is left. A
   commit, always: the drop indicator previews without touching the store, so
   the drop is the gesture's only mutation and a reload should replay it. */
export function arrange(order: string[], duo: DeskDuo): void {
  const by = new Map(state.panes.map((pane) => [pane.id, pane]))
  /* Counted as a list, not only as a set. `['a', 'b', 'b']` names two distinct
     panes, both of them up, and passed a set-size test while committing three
     entries with one pane object in it twice -- two windows sharing a pane's
     state, and a React key repeated. */
  if (order.length !== by.size || new Set(order).size !== order.length) return
  if (order.some((id) => !by.has(id))) return
  const panes = order.map((id) => by.get(id) as DeskPane)
  commit({ panes, duo })
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
    /* Checked as well as versioned: the version bump covers the bundle that
       wrote it, this covers a note that arrived any other way -- edited by hand,
       or written by a build the reader rolled back to and forward from. This is
       the one door into the tab that does not go through `openDeskTab`. */
    tab: TABS.includes(kept.tab) ? kept.tab : state.tab,
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
    duo: kept.duo === 'cols' ? 'cols' : 'rows',
  })
}

export function notifyDesk(): void {
  update({})
}

export function reset(): void {
  /* The palette is NOT shut here, and that is the point: a reset runs on the way
     out, before the pointer moves, so the conversation whose desk is about to be
     on screen is not known yet (`sync`). Shutting it now and opening it again a
     moment later is a flicker with no information in it. `initialState` reads
     the conversation being left, which is what is on screen. */
  state = initialState()
  listeners.forEach((listener) => listener())
}

export function _resetForTests(): void {
  palette._clearForTests()
  state = initialState()
  seen._clearForTests()
  KEPT.clear()
  REPLAYING.clear()
  listeners.clear()
}
