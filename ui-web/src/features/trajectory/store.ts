/* The trajectory view's state, outside React.
 *
 * One store for the chat column's second view: whether the gateway offers it,
 * which view is up, the entries of the open conversation in the index's own
 * order, the committed snapshot the live feed continues from, and the one
 * selection every surface reads. Imperative callers drive it -- the session
 * switch, the header's toggle, the poller -- so the state lives where they can
 * reach it and the island subscribes.
 *
 * Four rules the rest of the domain leans on:
 *   - the picture and the commitment are two things. `entries` is what the
 *     list shows and may be a partial preview while a snapshot is still being
 *     read; `epoch`, `revision` and `snapshotReady` move only when a walk has
 *     read the snapshot's last page. The feed carries only revisions above the
 *     snapshot's own, so continuing from a half-read snapshot would lose the
 *     unread half for good -- `applyChanges` refuses until the walk is whole.
 *   - `select` is the only writer of `selectedId`. A data update never selects
 *     and never deselects on its own; the one exception, an entry the index
 *     withdrew, goes through `select` too, under the `migrate` door, so a
 *     reader's row cannot silently become another row.
 *   - every answer is checked against the generation it was asked under. The
 *     generation moves on a session switch, on either direction of the view
 *     toggle, at the start of every snapshot walk and on a new handshake, and
 *     an answer from an older generation writes nothing at all: A -> B -> A
 *     can otherwise bring A's first answer back over A's second.
 *   - a batch of changes is one `set`. The feed may carry hundreds of upserts,
 *     and the list repaints once per batch rather than once per row.
 *
 * The page draws every agent's rows at once, straight from `entries`; the
 * store also keeps a current Sub-agent Scope -- the main session, or one
 * sub-agent run. `entries`, `index` and `spanIndex` hold every row, since links
 * and the feed cross scopes. Moving between scopes leaves the generation alone,
 * and `select` on a row of another scope moves to that scope. A scope coming
 * back brings its own selection back under the `migrate` door.
 */

import { current as sessionCurrent } from '../../lib/session'
import { has, servesTrajectory } from '../../rpc/capabilities'
import { isFresh, onFresh } from '../../state/session/conversation'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import { indexOf, insertSorted, removeIds } from './order'
import { isAbsent, isCursorExpired, isDisabled } from './source'

import type {
  SelectSource, TrajectoryChangesResult, TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult,
  TrajectorySource, View,
} from './types'

/** What a scope keeps while another is the current one. */
export interface ScopeView {
  selectedId: string | null
}

/** An open sub-agent tab: its Sub-agent Scope and the entry that opened it, as it was then. */
export interface OpenScope {
  id: string
  door: TrajectoryEntry | null
}

/** The stretch the reader picked on the ruler: its two ends in real time, so a
 *  live axis growing past them leaves the stretch where it was picked. */
export interface RulerPick {
  a: number
  b: number
}

export interface TrajectoryState {
  /** The gateway announced the surface and has not refused it since. */
  served: boolean
  /** `trajectory.state` said the view is on for this process. */
  enabled: boolean
  /** `trajectory.state` said tracing is recording, so the list can grow. */
  recording: boolean
  /** Whether `trajectory.state` has answered at all since the last handshake. */
  stateKnown: boolean
  /** The conversation on screen, as lib/session names it; null for a draft. */
  sessionKey: string | null
  /** The chat column is in its empty state (state/session/conversation.ts). */
  fresh: boolean
  view: View
  /** The committed snapshot's index generation; null until a walk completes. */
  epoch: string | null
  /** Every change up to this revision is in the picture; the feed continues from it. */
  revision: number
  /** The picture is a whole snapshot plus every batch since, not a preview. */
  snapshotReady: boolean
  /** In the index's order (`sort_key`), never mutated in place. */
  entries: TrajectoryEntry[]
  /** `entry_id` to position in `entries`, rebuilt with it. */
  index: Record<string, number>
  indexState: TrajectoryIndexState | null
  /** The index itself had the whole session when the snapshot was taken. */
  complete: boolean
  /** A snapshot walk is in progress. */
  listing: boolean
  /** The last read's failure, cleared by the next success. */
  fault: string | null
  selectedId: string | null
  /** Which door the selection came through, for a surface that treats its own differently. */
  selectedBy: SelectSource | null
  /** Bumped by every selection `select` makes, so a surface that answers each one once tells it from a re-render. */
  selections: number
  /** The stretch picked on the ruler, in real time; the rows show only what ran
   *  inside it. Kept beside the selection so Escape can take it back from the
   *  one ordered table (`state/escapeOrder.ts`). */
  picked: RulerPick | null
  /** The current scope: a Sub-agent Scope's id, or null for the main session. */
  scope: string | null
  /** The sub-agent tabs the reader opened, in the order they were opened. */
  scopes: OpenScope[]
  /** The tabs not on screen, by scope id (`scopeKey`). */
  saved: Record<string, ScopeView>
  /** The length `entries` had before the last purely-appending change, or -1
   *  when that change rewrote the list. A surface that caches a tree over
   *  `entries` reads this to know it may extend rather than rebuild. */
  grownFrom: number
  /** `trace_id:span_id` to the first entry of that span, for the links a detail carries. */
  spanIndex: Record<string, string>
  /** A Sub-agent Scope's id to the first entry that opens it. */
  doors: Record<string, string>
}

/** The Sub-agent Scope the entry belongs to; null for the main session. */
export const scopeOf = (entry: TrajectoryEntry): string | null => entry.subagent_scope ?? null
/** The Sub-agent Scope the entry opens, on a boundary's own entries. */
export const opensOf = (entry: TrajectoryEntry): string | null => entry.opens_subagent_scope ?? null
/** The Sub-agent Scope whose report started the entry's turn, on that turn's root entries. */
export const reportedFromOf = (entry: TrajectoryEntry): string | null => entry.dispatched_by_subagent_scope ?? null

/* The main session's key among the saved tabs: a scope id is `trace:span`, never empty. */
const MAIN_KEY = ''
export const scopeKey = (scope: string | null): string => scope ?? MAIN_KEY

const spanKey = (trace: string, span: string): string => `${trace}:${span}`

/* Everything the page reads that follows from `entries`: their positions,
   the span lookup, and the entry that opens each scope. */
function derived(entries: TrajectoryEntry[]) {
  const spanIndex: Record<string, string> = {}
  const doors: Record<string, string> = {}
  for (const entry of entries) {
    const key = spanKey(entry.trace_id, entry.span_id)
    if (!(key in spanIndex)) spanIndex[key] = entry.entry_id
    const opens = opensOf(entry)
    if (opens !== null && !(opens in doors)) doors[opens] = entry.entry_id
  }
  return { entries, index: indexOf(entries), spanIndex, doors }
}

const initial: TrajectoryState = {
  served: false,
  enabled: false,
  recording: false,
  stateKnown: false,
  sessionKey: null,
  fresh: true,
  view: 'chat',
  epoch: null,
  revision: 0,
  snapshotReady: false,
  entries: [],
  index: {},
  indexState: null,
  complete: false,
  listing: false,
  fault: null,
  selectedId: null,
  selectedBy: null,
  selections: 0,
  picked: null,
  scope: null,
  scopes: [],
  saved: {},
  grownFrom: -1,
  spanIndex: {},
  doors: {},
}

const store = makeStore<TrajectoryState>(initial)

export const { get, set, subscribe } = store

const patch = (p: Partial<TrajectoryState>): void => { store.set({ ...store.get(), ...p }) }

/** The fields that change together with the rows: the whole set, its positions and the lookups. */
export const rowsOf = (entries: TrajectoryEntry[]) => derived(entries)
const rows = rowsOf

export const source = (): TrajectorySource | null => sources.trajectory ?? null

/* What a conversation keeps while another is on screen: its view and its
   selection. Light on purpose -- the entries themselves are read back from
   the gateway on return, which is what keeps a page that visits many
   conversations from holding all of them. */
interface Remembered {
  view: View
  selectedId: string | null
}

/** How many conversations' places are kept; the oldest goes when a new one arrives. */
export const REMEMBERED_MAX = 20

/** How many times a snapshot walk starts over on an expired cursor before it gives up. */
export const WALK_RETRIES = 3

const remembered = new Map<string, Remembered>()

let generation = 0
let handshakes = 0
let stateInflight: Promise<void> | null = null
let unwatchFresh: (() => void) | null = null

/** The generation every conversation-bound answer is checked against. */
export const gen = (): number => generation

const bump = (): number => { generation += 1; return generation }

/** The header's toggle appears exactly when this holds. */
export const available = (s: TrajectoryState = store.get()): boolean =>
  s.served && s.enabled && s.sessionKey !== null && !s.fresh

/* ── wiring ───────────────────────────────────────────────────────────── */

/* Mirrors the chat column's empty-state flag. Called once by
   src/app/install.ts. A conversation cleared while its trajectory was up has
   nothing left to show a trajectory of, and the toggle that would bring the
   reader back is gone with the content, so the column goes back to the
   conversation view itself. */
export function install(): void {
  if (unwatchFresh) return
  patch({ fresh: isFresh() })
  unwatchFresh = onFresh(() => {
    const fresh = isFresh()
    if (fresh !== store.get().fresh) patch({ fresh })
    if (!available()) leaveView()
  })
}

/* A new connection has shaken hands. Everything asked of the old one is
   now an answer about a gateway that may be gone, so the generation moves and
   a `state` answer still in the air is dropped when it lands. */
export function handshake(): void {
  handshakes += 1
  bump()
  patch({ stateKnown: false })
}

/* ── the gateway's verdict on the surface ─────────────────────────────── */

function leaveView(): void {
  if (store.get().view === 'trajectory') {
    bump()
    patch({ view: 'chat', listing: false, picked: null })
  }
}

/** The surface is switched off for this process; the toggle goes, the data stays. */
export function disabledByServer(): void {
  patch({ enabled: false, stateKnown: true })
  leaveView()
}

/** The gateway does not serve the surface at all. */
export function absent(): void {
  patch({ served: false, stateKnown: true })
  leaveView()
}

/* Every failed read lands here. The two verdicts about the surface change
   what the page offers; anything else is a fault the status line says and
   the next successful read clears. */
function failed(e: unknown): void {
  if (isDisabled(e)) { disabledByServer(); return }
  if (isAbsent(e)) { absent(); return }
  patch({ fault: (e as Error)?.message || String(e) })
}

/* Reads `trajectory.state`. Bound to the handshake rather than the
   generation: it describes the process, not a conversation, but an answer
   from before a reconnect describes the process that was. One at a time, so
   the beat and a reconnect landing together ask once. */
export function refreshState(): Promise<void> {
  if (stateInflight) return stateInflight
  const h = handshakes
  stateInflight = (async () => {
    const src = source()
    if (!src || !servesTrajectory() || !has('trajectory')) {
      patch({ served: false, stateKnown: true })
      leaveView()
      return
    }
    try {
      const answer = await src.state()
      if (h !== handshakes) return
      patch({ served: true, enabled: answer.enabled, recording: answer.recording_enabled, stateKnown: true })
      if (!answer.enabled) leaveView()
    } catch (e) {
      if (h !== handshakes) return
      failed(e)
    }
  })().finally(() => { stateInflight = null })
  return stateInflight
}

/* ── the view ─────────────────────────────────────────────────────────── */

/* Either direction moves the generation: the feed stops on the way out and
   starts afresh on the way in, so an answer from the earlier run of the same
   conversation cannot land in the later one. A walk cut short on the way out
   leaves its preview up and `listing` down; the next visit reads the snapshot
   over, because what is up is not something the feed can continue from. */
export function setView(view: View): void {
  const s = store.get()
  if (s.view === view) return
  if (view === 'trajectory' && !available(s)) return
  bump()
  /* A pick belongs to the ruler it was made on; leaving the view drops it. */
  patch({ view, listing: false, picked: view === 'trajectory' ? s.picked : null })
}

export const toggleView = (): void => { setView(store.get().view === 'trajectory' ? 'chat' : 'trajectory') }

/* ── the selection ────────────────────────────────────────────────────── */

/** The one way `selectedId` changes. `source` says which door it came through. */
export function select(entryId: string | null, opts: { source: SelectSource }): void {
  const s = store.get()
  if (entryId !== null && !(entryId in s.index)) return
  if (entryId !== null) {
    const target = scopeOf(s.entries[s.index[entryId]!]!)
    if (target !== s.scope) { enter(target, { select: { id: entryId, source: opts.source } }); return }
  }
  if (s.selectedId === entryId) return
  patch({ selectedId: entryId, selectedBy: entryId === null ? null : opts.source, selections: s.selections + 1 })
}

/** Selects an entry whichever tab it belongs to, without moving the tab: the
 *  waterfall draws every agent at once, so a click on a worker's row must not
 *  filter the page down to that worker. */
export function selectAcross(entryId: string | null, opts: { source: SelectSource }): void {
  const s = store.get()
  if (entryId !== null && !(entryId in s.index)) return
  if (s.selectedId === entryId) return
  patch({ selectedId: entryId, selectedBy: entryId === null ? null : opts.source, selections: s.selections + 1 })
}

/** The stretch picked on the ruler; the rows are filtered to it. A pick of the
 *  same stretch is not a change, so a pointer that never moved writes nothing. */
export function setPicked(pick: RulerPick | null): void {
  const held = store.get().picked
  if (held === pick) return
  if (held !== null && pick !== null && held.a === pick.a && held.b === pick.b) return
  patch({ picked: pick })
}

/* ── the scopes ───────────────────────────────────────────────────────── */

const freshView = (): ScopeView => ({ selectedId: null })

const viewOf = (s: TrajectoryState): ScopeView => ({ selectedId: s.selectedId })

const doorOf = (s: TrajectoryState, id: string): TrajectoryEntry | null => {
  const door = s.doors[id]
  const at = door === undefined ? undefined : s.index[door]
  return at === undefined ? null : (s.entries[at] ?? null)
}

/* Puts another tab on screen in one write: the tab being left keeps its
   view (unless it is being closed), the arriving one gets its own back, or
   a fresh one, or the selection the caller brings. The generation stays
   where it is: moving it would make a snapshot walk in progress give up at
   its next page and void the details' answers in the air, and a tab is only
   another filter over the same picture. */
function enter(target: string | null, how: {
  select?: { id: string; source: SelectSource }
  discard?: boolean
  base?: Pick<TrajectoryState, 'scopes' | 'saved'>
} = {}): void {
  const s: TrajectoryState = { ...store.get(), ...how.base }
  const saved = { ...s.saved }
  if (!how.discard) saved[scopeKey(s.scope)] = viewOf(s)
  const back = saved[scopeKey(target)] ?? freshView()
  delete saved[scopeKey(target)]
  const scopes = target === null || s.scopes.some((o) => o.id === target) ? s.scopes : [...s.scopes, { id: target, door: doorOf(s, target) }]
  let selectedId = how.select ? how.select.id : back.selectedId
  if (selectedId !== null && !(selectedId in s.index)) selectedId = null
  const moved = how.select !== undefined || selectedId !== s.selectedId
  patch({
    scope: target, scopes, saved,
    selectedId, selectedBy: selectedId === null ? null : (how.select ? how.select.source : 'migrate'),
    selections: moved ? s.selections + 1 : s.selections,
  })
}

/** Opens a sub-agent tab, or goes to it when it is open; says whether it was opened just now. */
export function openScope(id: string): boolean {
  const s = store.get()
  const added = !s.scopes.some((o) => o.id === id)
  if (s.scope !== id) enter(id)
  return added
}

/** Goes to an open tab, or to the main session's. */
export function switchScope(id: string | null): void {
  const s = store.get()
  if (s.scope === id || (id !== null && !s.scopes.some((o) => o.id === id))) return
  enter(id)
}

/* Closes a sub-agent tab and forgets what it kept, so it opens afresh next
   time. The tab on screen being closed hands over to its right-hand
   neighbour, else its left, else the main session. */
export function closeScope(id: string): void {
  const s = store.get()
  const at = s.scopes.findIndex((o) => o.id === id)
  if (at < 0) return
  const scopes = s.scopes.filter((o) => o.id !== id)
  const saved = { ...s.saved }
  delete saved[scopeKey(id)]
  if (s.scope === id) {
    const neighbour = s.scopes[at + 1]?.id ?? s.scopes[at - 1]?.id ?? null
    enter(neighbour, { discard: true, base: { scopes, saved } })
    return
  }
  patch({ scopes, saved })
}

/* The rows after a change and what the change replaced, for carrying a
   saved scope's selection across it. */
interface Change {
  afterIndex: Record<string, number>
  replaced: ReadonlyMap<string, string | null>
}

/* The saved scopes after a change: each one's selection, kept while the index
   has it or its replacement, whichever scope it is in now, the way the
   current scope keeps its own. Answers the same object when nothing moved. */
function carry(saved: Record<string, ScopeView>, c: Change): Record<string, ScopeView> {
  let out: Record<string, ScopeView> | null = null
  for (const [key, view] of Object.entries(saved)) {
    let selectedId = view.selectedId
    if (selectedId !== null && !(selectedId in c.afterIndex)) {
      const by = c.replaced.get(selectedId)
      selectedId = by && by in c.afterIndex ? by : null
    }
    if (selectedId === view.selectedId) continue
    out ??= { ...saved }
    out[key] = { ...view, selectedId }
  }
  return out ?? saved
}

const NOTHING_REPLACED: ReadonlyMap<string, string | null> = new Map()

/* ── the conversation on screen ───────────────────────────────────────── */

/* What is kept of a conversation is its main session's selection: the
   sub-agent tabs are let go with it, so one of theirs would be a selection
   nobody can return to. */
function remember(key: string): void {
  const s = store.get()
  const main = s.scope === null ? viewOf(s) : (s.saved[MAIN_KEY] ?? freshView())
  remembered.delete(key)
  remembered.set(key, { view: s.view, ...main })
  while (remembered.size > REMEMBERED_MAX) {
    const oldest = remembered.keys().next().value as string
    remembered.delete(oldest)
  }
}

/* A different conversation is a different trajectory. The one being left
   keeps its place; the one arriving gets its place back, and if it was being
   read as a trajectory the poller reads it again from the gateway. The
   entries are not carried across: they belong to the key they were read for. */
export function sessionChanged(key: string | null = sessionCurrent()): void {
  const s = store.get()
  if (s.sessionKey === key) return
  if (s.sessionKey !== null) remember(s.sessionKey)
  const back = key !== null ? remembered.get(key) : undefined
  bump()
  patch({
    sessionKey: key,
    view: back?.view ?? 'chat',
    epoch: null,
    revision: 0,
    snapshotReady: false,
    entries: [],
    index: {},
    indexState: null,
    complete: false,
    listing: false,
    fault: null,
    selectedId: back?.selectedId ?? null,
    selectedBy: back?.selectedId ? 'migrate' : null,
    picked: null,
    scope: null,
    scopes: [],
    saved: {},
    spanIndex: {},
    doors: {},
  })
  if (back?.view === 'trajectory' && !available()) patch({ view: 'chat' })
}

/* ── reading the snapshot ─────────────────────────────────────────────── */

interface Walked {
  entries: TrajectoryEntry[]
  last: TrajectoryListResult
}

/* Every page of one snapshot, cursor after cursor until the index says the
   snapshot is at its end. On a first read the pages land as they arrive, as a
   preview, so a long conversation starts to read before its last page is in;
   nothing about the commitment moves until the walk returns. Answers null
   when the walk was overtaken. */
async function pages(src: TrajectorySource, key: string, g: number, stream: boolean): Promise<Walked | null> {
  let cursor: string | null = null
  let entries: TrajectoryEntry[] = []
  for (;;) {
    const page: TrajectoryListResult = await src.list(key, cursor)
    if (g !== generation || store.get().sessionKey !== key) return null
    const before = entries.length
    entries = cursor === null ? [...page.entries] : [...entries, ...page.entries]
    cursor = page.next_cursor ?? null
    /* A first read puts every page on screen as it lands. A re-read does not:
       streaming it spared the wait for the whole snapshot, but each page put a
       shorter picture in place of the one up and the list shrank under the
       reader, so that picture stays until the new one is whole (`load`). A walk
       restarted after its cursor expired is not streamed either -- its pages
       belong to a different snapshot, and half of one is not a picture. */
    if (stream) patch({ ...rows(entries), grownFrom: cursor === null ? -1 : before, indexState: page.index_state })
    if (cursor === null) return { entries, last: page }
  }
}

/* The snapshot of the conversation on screen, read whole and then committed
   in one write. A re-read (the feed said the epoch moved, an earlier walk was
   cut short or failed) keeps the picture that is up until the new one is
   whole, so the list never shrinks and grows again under the reader. A walk
   that fails leaves the commitment where it was and says so in `fault`; the
   poller reads again on its slow beat. */
export async function load(): Promise<void> {
  const src = source()
  const key = store.get().sessionKey
  if (!src || !key) return
  let restarts = 0
  let stream = store.get().entries.length === 0
  for (;;) {
    const g = bump()
    patch({ listing: true })
    try {
      const got = await pages(src, key, g, stream)
      if (got === null) return
      const { entries, last } = got
      const was = store.get()
      /* The pages already put this exact list on screen; re-deriving it would
         only throw away the tree the reader is looking at. */
      const whole = was.entries === entries
      const next = whole ? null : rows(entries)
      const change: Change = { afterIndex: next?.index ?? was.index, replaced: NOTHING_REPLACED }
      patch({
        ...(next ?? {}),
        grownFrom: -1,
        saved: carry(was.saved, change),
        epoch: last.epoch,
        revision: last.snapshot_revision,
        snapshotReady: true,
        indexState: last.index_state,
        complete: last.complete,
        listing: false,
        fault: null,
      })
      const selected = store.get().selectedId
      if (selected !== null && !(selected in store.get().index)) select(null, { source: 'migrate' })
      return
    } catch (e) {
      if (g !== generation || store.get().sessionKey !== key) return
      /* The snapshot behind the cursor was let go (120 s idle, or the index
         itself moved on). Start over from a fresh snapshot, and finish that
         one whole: pages of the old snapshot may already be on screen. */
      if (isCursorExpired(e) && restarts < WALK_RETRIES) {
        restarts += 1
        stream = false
        continue
      }
      patch({ listing: false })
      failed(e)
      return
    }
  }
}

/** The feed said the index moved on: the picture stays, but it is a preview again until re-read. */
export function snapshotStale(): void {
  if (store.get().snapshotReady) patch({ snapshotReady: false })
}

/* ── the live feed ────────────────────────────────────────────────────── */

/* One batch, one write, and only onto a whole snapshot of the same epoch.
   Removals first, so a replaced row's selection moves to its replacement
   before the replacement is inserted; the watermark moves only after every
   row of the batch is in. An upsert of a row the list holds is placed again
   by its new key, so a row whose time was filled in late moves rather than
   sitting out of order. */
export function applyChanges(batch: TrajectoryChangesResult): void {
  const s = store.get()
  /* A walk in progress is replacing the list page by page; a batch merged into
     that half-old picture would belong to neither snapshot. */
  if (!s.snapshotReady || s.listing || batch.epoch !== s.epoch) return
  let entries = removeIds(s.entries, batch.removed.map((r) => r.entry_id))
  for (const entry of batch.upserts) entries = insertSorted(entries, entry)
  const index = indexOf(entries)
  let selectedId = s.selectedId
  if (selectedId !== null) {
    const gone = batch.removed.find((r) => r.entry_id === selectedId)
    if (gone) selectedId = gone.replaced_by && gone.replaced_by in index ? gone.replaced_by : null
    else if (!(selectedId in index)) selectedId = null
  }
  const change: Change = {
    afterIndex: index,
    replaced: new Map(batch.removed.map((r) => [r.entry_id, r.replaced_by ?? null])),
  }
  patch({
    ...rows(entries),
    grownFrom: -1,
    saved: carry(s.saved, change),
    revision: Math.max(s.revision, batch.to_revision),
    indexState: batch.index_state,
    fault: null,
    selectedId,
    selectedBy: selectedId === null ? null : selectedId === s.selectedId ? s.selectedBy : 'migrate',
  })
}

/** A live read failed; the status line says so and the data stays. */
export function liveFailed(e: unknown): void {
  failed(e)
}

/** The row, when the list still has it. */
export const entry = (id: string): TrajectoryEntry | null => {
  const s = store.get()
  const at = s.index[id]
  return at === undefined ? null : (s.entries[at] ?? null)
}

/** The first entry of a span, by the identity the details carry, when the list has one. */
export const entryOfSpan = (traceId: string, spanId: string, s: TrajectoryState = store.get()): string | null =>
  s.spanIndex[spanKey(traceId, spanId)] ?? null

export function _resetForTests(): void {
  store._resetForTests()
  remembered.clear()
  generation = 0
  handshakes = 0
  stateInflight = null
  if (unwatchFresh) { unwatchFresh(); unwatchFresh = null }
}
