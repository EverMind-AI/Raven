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
 */

import { current as sessionCurrent } from '../../lib/session'
import { has, servesTrajectory } from '../../rpc/capabilities'
import { isFresh, onFresh } from '../../state/session/conversation'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import { snapThreshold } from './geometry'
import { indexOf, insertSorted, removeIds } from './order'
import { isAbsent, isCursorExpired, isDisabled } from './source'

import type {
  SelectSource, TrajectoryChangesResult, TrajectoryEntry, TrajectoryIndexState, TrajectoryListResult,
  TrajectorySource, View,
} from './types'

/* Where the reader was in the list when they were not following its tail:
   the entry at the top of the viewport and how far into it they had scrolled.
   An id rather than an index, because the feed inserts above as well as below. */
export interface Anchor {
  id: string
  offset: number
}

/* The duration bar's view of the entries: how far it is zoomed, where it is
   scrolled to, whether it still fits the bar (and so re-fits as rows
   arrive), the fit's unit it froze on the first zoom, the place at its left
   edge, and the dense block the reader has opened a pick list for. */
export interface Timeline {
  scale: number
  offset: number
  fit: boolean
  frozenUnit: number | null
  anchor: { id: string; frac: number } | null
  bucket: { ids: string[]; x: number } | null
  /** The duration filter's popover is up. */
  threshold: boolean
}

export const initialTimeline: Timeline = {
  scale: 1, offset: 0, fit: true, frozenUnit: null, anchor: null, bucket: null, threshold: false,
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
  /** Within reach of the tail, so new rows pull the viewport down. */
  follow: boolean
  anchor: Anchor | null
  timeline: Timeline
  /** The reader's two switches for the list and the bar, kept in the browser. */
  prefs: Prefs
  /** Hidden rows shown for now because something pointed at them; cleared with the conversation. */
  revealed: string[]
  /** The rows the list draws: `entries` less the hidden ones, unless the switch or `revealed` says otherwise. */
  visible: TrajectoryEntry[]
  /** `entry_id` to position in `visible`, rebuilt with it. */
  visibleIndex: Record<string, number>
  /** `trace_id:span_id` to the first entry of that span, for the links a detail carries. */
  spanIndex: Record<string, string>
  /** A turn's whole charged time, from its reply entry, by `turn_span_id`. */
  turnTotals: Record<string, number>
}

/** What the reader can set: the bar's duration threshold (0 filters nothing), and showing the entries the list hides. */
export interface Prefs {
  minChargedMs: number
  showHidden: boolean
}

export const PREFS_KEY = 'raven.gui.trajectory.prefs'
const DEFAULT_PREFS: Prefs = { minChargedMs: 20, showHidden: false }

/** The switches as the browser last kept them; the defaults where it kept nothing readable. */
export function readPrefs(): Prefs {
  return loadPrefs()
}

function loadPrefs(): Prefs {
  try {
    const raw: unknown = typeof localStorage === 'undefined' ? null : JSON.parse(localStorage.getItem(PREFS_KEY) || 'null')
    if (raw && typeof raw === 'object') {
      /* The switches an earlier build kept (`hideShort`, `showInternal`) are read into the current two. */
      const value = raw as Partial<Prefs> & { hideShort?: unknown; showInternal?: unknown }
      const minChargedMs = typeof value.minChargedMs === 'number'
        ? snapThreshold(value.minChargedMs)
        : typeof value.hideShort === 'boolean' ? (value.hideShort ? 20 : 0) : DEFAULT_PREFS.minChargedMs
      const showHidden = typeof value.showHidden === 'boolean'
        ? value.showHidden
        : typeof value.showInternal === 'boolean' ? value.showInternal : DEFAULT_PREFS.showHidden
      return { minChargedMs, showHidden }
    }
  } catch {
    /* Storage may be unavailable or hold something else; the defaults stand. */
  }
  return { ...DEFAULT_PREFS }
}

function savePrefs(prefs: Prefs): void {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
  } catch {
    /* Private mode or quota: the switch still holds for this page. */
  }
}

/** The index's reason for leaving a row out of the list, or null for a row it shows. */
export const hiddenOf = (entry: TrajectoryEntry): string | null => {
  const value = entry.meta?.hidden
  return typeof value === 'string' ? value : null
}

const spanKey = (trace: string, span: string): string => `${trace}:${span}`

/* Everything the list and the bar read that follows from `entries`: the
   visible rows and their positions, the span lookup and the turn totals. */
function derived(entries: TrajectoryEntry[], prefs: Prefs, revealed: string[]) {
  const visible: TrajectoryEntry[] = []
  const spanIndex: Record<string, string> = {}
  const turnTotals: Record<string, number> = {}
  for (const entry of entries) {
    const key = spanKey(entry.trace_id, entry.span_id)
    if (!(key in spanIndex)) spanIndex[key] = entry.entry_id
    if (entry.slot === 'turn.output' && entry.turn_span_id && typeof entry.charged_ms === 'number') {
      turnTotals[entry.turn_span_id] = entry.charged_ms
    }
    if (hiddenOf(entry) === null || prefs.showHidden || revealed.includes(entry.entry_id)) visible.push(entry)
  }
  return { entries, index: indexOf(entries), visible, visibleIndex: indexOf(visible), spanIndex, turnTotals }
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
  follow: true,
  anchor: null,
  timeline: initialTimeline,
  prefs: loadPrefs(),
  revealed: [],
  visible: [],
  visibleIndex: {},
  spanIndex: {},
  turnTotals: {},
}

const store = makeStore<TrajectoryState>(initial)

export const { get, set, subscribe } = store

const patch = (p: Partial<TrajectoryState>): void => { store.set({ ...store.get(), ...p }) }

/** The fields that change together with the rows: the whole set, its positions, the visible set and the lookups. */
export const rowsOf = (entries: TrajectoryEntry[], s: TrajectoryState = store.get()) => derived(entries, s.prefs, s.revealed)
const rows = rowsOf

export const source = (): TrajectorySource | null => sources.trajectory ?? null

/* What a conversation keeps while another is on screen: its view, its
   selection and where its list was scrolled to. Light on purpose -- the
   entries themselves are read back from the gateway on return, which is what
   keeps a page that visits many conversations from holding all of them. */
interface Remembered {
  view: View
  selectedId: string | null
  follow: boolean
  anchor: Anchor | null
  timeline: Timeline
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
    const s = store.get()
    patch({ view: 'chat', listing: false, timeline: { ...s.timeline, bucket: null, threshold: false }, revealed: [], ...rows(s.entries, { ...s, revealed: [] }) })
  }
}

/* ── the reader's switches ────────────────────────────────────────────── */

/** Change a setting: kept in the browser for the next visit, and the rows reconsidered now. */
export function setPrefs(next: Partial<Prefs>): void {
  const s = store.get()
  const prefs = { ...s.prefs, ...next, minChargedMs: snapThreshold(next.minChargedMs ?? s.prefs.minChargedMs) }
  if (prefs.minChargedMs === s.prefs.minChargedMs && prefs.showHidden === s.prefs.showHidden) return
  savePrefs(prefs)
  patch({ prefs, revealed: [], ...derived(s.entries, prefs, []) })
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
  const revealed = view === 'chat' ? [] : s.revealed
  patch({
    view, listing: false, timeline: view === 'chat' ? { ...s.timeline, bucket: null, threshold: false } : s.timeline,
    revealed, ...rows(s.entries, { ...s, revealed }),
  })
}

export const toggleView = (): void => { setView(store.get().view === 'trajectory' ? 'chat' : 'trajectory') }

/* ── the selection ────────────────────────────────────────────────────── */

/** The one way `selectedId` changes. `source` says which door it came through. */
export function select(entryId: string | null, opts: { source: SelectSource }): void {
  const s = store.get()
  if (entryId !== null && !(entryId in s.index)) return
  /* A hidden row that something pointed at is shown for now, so the selection has a row to land on --
     also when it was the selection already and a switch has since hidden it. */
  const reveal = entryId !== null && !(entryId in s.visibleIndex)
  if (s.selectedId === entryId && !reveal) return
  const revealed = reveal ? [...s.revealed, entryId as string] : s.revealed
  patch({
    selectedId: entryId, selectedBy: entryId === null ? null : opts.source,
    ...(reveal ? { revealed, ...rows(s.entries, { ...s, revealed }) } : {}),
  })
}

/** The list reports where the reader is: at the tail, or anchored to a row. */
export function setPlace(follow: boolean, anchor: Anchor | null): void {
  const s = store.get()
  if (s.follow === follow && s.anchor?.id === anchor?.id && s.anchor?.offset === anchor?.offset) return
  patch({ follow, anchor })
}

/* ── the duration bar's view ──────────────────────────────────────────── */

/** The bar's zoom and scroll, written by the bar after every move. */
export function setTimeline(next: Partial<Timeline>): void {
  patch({ timeline: { ...store.get().timeline, ...next } })
}

/** A dense block's pick list is up, for these entries, at this content x. */
export function openBucket(ids: string[], x: number): void {
  if (!ids.length) return
  setTimeline({ bucket: { ids, x } })
}

export function closeBucket(): void {
  if (store.get().timeline.bucket !== null) setTimeline({ bucket: null })
}

/** The duration filter's popover, opened or closed by its button, Escape or a click elsewhere. */
export function setThresholdOpen(open: boolean): void {
  if (store.get().timeline.threshold !== open) setTimeline({ threshold: open })
}

/* ── the conversation on screen ───────────────────────────────────────── */

function remember(key: string): void {
  const s = store.get()
  remembered.delete(key)
  remembered.set(key, {
    view: s.view, selectedId: s.selectedId, follow: s.follow, anchor: s.anchor, timeline: { ...s.timeline, bucket: null, threshold: false },
  })
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
    follow: back?.follow ?? true,
    anchor: back?.anchor ?? null,
    timeline: back?.timeline ?? initialTimeline,
    revealed: [],
    visible: [],
    visibleIndex: {},
    spanIndex: {},
    turnTotals: {},
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
async function pages(src: TrajectorySource, key: string, g: number, progressive: boolean): Promise<Walked | null> {
  let cursor: string | null = null
  let entries: TrajectoryEntry[] = []
  for (;;) {
    const page: TrajectoryListResult = await src.list(key, cursor)
    if (g !== generation || store.get().sessionKey !== key) return null
    entries = cursor === null ? [...page.entries] : [...entries, ...page.entries]
    cursor = page.next_cursor ?? null
    if (progressive) patch({ ...rows(entries), indexState: page.index_state })
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
  let progressive = store.get().entries.length === 0
  let restarts = 0
  for (;;) {
    const g = bump()
    patch({ listing: true })
    try {
      const got = await pages(src, key, g, progressive)
      if (got === null) return
      const { entries, last } = got
      patch({
        ...rows(entries),
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
        progressive = false
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
  if (!s.snapshotReady || batch.epoch !== s.epoch) return
  let entries = removeIds(s.entries, batch.removed.map((r) => r.entry_id))
  for (const entry of batch.upserts) entries = insertSorted(entries, entry)
  const index = indexOf(entries)
  let selectedId = s.selectedId
  if (selectedId !== null) {
    const gone = batch.removed.find((r) => r.entry_id === selectedId)
    if (gone) selectedId = gone.replaced_by && gone.replaced_by in index ? gone.replaced_by : null
    else if (!(selectedId in index)) selectedId = null
  }
  patch({
    ...rows(entries, s),
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
