/* The details pane's state: what is open, which tab, and every descriptor
 * and block body the page has read, each filed under the identity it was read
 * for.
 *
 * Its own store beside the list's rather than more fields on it, because the
 * two move at different rhythms -- the list on every feed batch, this one on
 * a click -- and a component reading one should not redraw for the other.
 * It follows the list's store for the facts it depends on: which
 * conversation and epoch are current, which entry is selected, and that
 * entry's revision.
 *
 * Four rules, each the answer to a way this pane could lie:
 *   - everything is filed by its whole identity -- session, epoch, entry,
 *     revision, block -- and a key is a serialised tuple, so a rebuilt index
 *     whose revisions start over again cannot hand an old body to a new
 *     entry, and no separator inside a name can alias two keys.
 *   - an answer may touch the pane only under the ticket it was asked with.
 *     The ticket moves on every selection, open, close, tab change, session
 *     or view switch and on every accepted descriptor; an answer from an
 *     older ticket may still be filed in the cache under its own identity,
 *     but it changes nothing the reader is looking at -- so a failure from
 *     the entry they left cannot close the one they picked.
 *   - what is kept has a budget, measured in bytes the way the wire would
 *     carry them, and the entry on screen is not exempt: its other tabs and
 *     its earlier pages go before anything is refused, and a record that is
 *     evicted leaves no "loading" behind it.
 *   - a revision or epoch change is one flow. The descriptor is re-read and
 *     accepted under the identity it answers with; the blocks and cursors of
 *     the revision before it are dropped, and the tab the reader was on is
 *     read again from its first page, or the overview takes over when the
 *     new descriptor no longer has that tab.
 */

import { onTrajectoryEscape } from '../../state/escapeOrder'
import { makeStore } from '../../state/store'
import { isDisabled, isEntryGone, revisionChange, saysAbsent } from './source'
import * as list from './store'

import type { JsonValue, Renderer, TrajectoryBlockResult, TrajectoryDetailResult } from './types'

export interface Identity {
  sessionKey: string
  epoch: string
  entryId: string
  revision: number
}

export interface BlockPage {
  offset: number
  data: JsonValue | null
  availability: TrajectoryBlockResult['availability']
  reason: string | null
  integrity: string[]
  truncated: boolean
  total: number | null
}

export interface BlockRecord {
  identity: Identity
  blockId: string
  renderer: Renderer
  pages: BlockPage[]
  nextCursor: string | null
  /** Items of earlier pages let go to stay inside the page window. */
  droppedBefore: number
  bytes: number
  at: number
}

export interface DescriptorRecord {
  identity: Identity
  value: TrajectoryDetailResult
  bytes: number
  at: number
}

/* One row of a message list's outline, as the gateway's `outline` block
   spells it: enough to draw the row folded, and the cursor of the page its
   body starts on. */
export interface OutlineItem {
  index: number
  role: string
  bytes: number | null
  chars: number | null
  preview: string
  partial: boolean
  missing: boolean
  cursor: string
}

/* The outline of the current entry's message list: every row, gathered page
   by page in the background, so the pane can draw the whole list folded
   before any body is read. */
export interface OutlineRecord {
  identity: Identity
  items: OutlineItem[]
  total: number | null
  nextCursor: string | null
  /** A page is on its way. */
  loading: boolean
  fault: string | null
  bytes: number
  at: number
}

export type Tab = 'overview' | string

export interface DetailsState {
  open: boolean
  /** The reader's own width in pixels, or null for the default. */
  width: number | null
  /** The trajectory area's width, as the island measured it. */
  areaWidth: number
  /** The identity of the descriptor the pane is showing, once one was accepted. */
  current: Identity | null
  /** The list says the selected entry moved on; the descriptor is to be read again. */
  stale: boolean
  /** Consecutive revision changes inside one reader action went past the limit. */
  unstable: boolean
  /** The gateway answered for an epoch the list has not reached; reads wait for the list. */
  waitingEpoch: string | null
  /* A page said the entry is already at this revision of this epoch; a
     descriptor of the same epoch older than it cannot end the wait. Bound to
     the epoch because a rebuilt index counts revisions from the start again,
     so a bound from the epoch before says nothing about the one after. */
  pending: { epoch: string; revision: number } | null
  /** The list is not in a state the pane may read in (view, switch or snapshot away); mirrored so the pane redraws when it changes. */
  paused: boolean
  tabByEntry: Record<string, Tab>
  /** Scroll offsets by entry and tab: an intention, kept across revisions. */
  scrollByEntryTab: Record<string, number>
  descriptors: Record<string, DescriptorRecord>
  blocks: Record<string, BlockRecord>
  /** Message outlines by descriptor key: one per entry identity. */
  outlines: Record<string, OutlineRecord>
  /** Reads in the air, by key, each owned by the request's own token. */
  loading: Record<string, number>
  faults: Record<string, string>
}

export const DEFAULT_WIDTH = 480
export const MIN_WIDTH = 320
export const NARROW_BELOW = 800
export const RESIZE_STEP = 16
/** Bytes of descriptors and block bodies kept at once. */
export const BUDGET = 8 * 1024 * 1024
/** Pages of one block kept at once; the earliest go as more arrive. */
export const PAGE_WINDOW = 10
/** How many messages the gateway puts on one page of a message list (`MESSAGES_PAGE` in details.py). */
export const MESSAGES_PAGE = 20
export const DESCRIPTORS_MAX = 20
/** Revision changes followed inside one reader action before giving up. */
export const REVISION_RETRIES = 3

const initial: DetailsState = {
  open: false,
  width: null,
  areaWidth: 0,
  current: null,
  stale: false,
  unstable: false,
  waitingEpoch: null,
  pending: null,
  paused: true,
  tabByEntry: {},
  scrollByEntryTab: {},
  descriptors: {},
  blocks: {},
  outlines: {},
  loading: {},
  faults: {},
}

const store = makeStore<DetailsState>(initial)

export const { get, set, subscribe } = store

const patch = (p: Partial<DetailsState>): void => { store.set({ ...store.get(), ...p }) }

const without = <T>(record: Record<string, T>, key: string): Record<string, T> => {
  if (!(key in record)) return record
  const next = { ...record }
  delete next[key]
  return next
}

/* ── keys and sizes ───────────────────────────────────────────────────── */

/** A cache key: a serialised tuple, so no name can alias another. */
export const keyOf = (...parts: Array<string | number | null>): string => JSON.stringify(parts)

export const descriptorKey = (id: Identity): string => keyOf(id.sessionKey, id.epoch, id.entryId, id.revision)
export const blockKey = (id: Identity, blockId: string): string =>
  keyOf(id.sessionKey, id.epoch, id.entryId, id.revision, blockId)
export const scrollKey = (entryId: string, tab: Tab): string => keyOf(entryId, tab)

const sameIdentity = (a: Identity | null, b: Identity | null): boolean =>
  a !== null && b !== null && a.sessionKey === b.sessionKey && a.epoch === b.epoch
  && a.entryId === b.entryId && a.revision === b.revision

/* Bytes as the wire would carry the value: UTF-8 of its JSON. The estimate
   for a page with no encoder is two bytes a code unit, which overstates
   rather than understates. */
export function bytesOf(value: unknown): number {
  const text = JSON.stringify(value) ?? ''
  if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text).length
  return text.length * 2
}

/* ── tickets ──────────────────────────────────────────────────────────── */

let gen = 0
let clock = 0
let tokens = 0
let revisionHops = 0
let unfollow: (() => void) | null = null
/** Reads in the air, by key, each owned by one request's token. */
const inflight = new Map<string, number>()

/** The pane's ticket: an answer asked under an older one changes nothing on screen. */
export const ticket = (): number => gen

const bump = (): void => { gen += 1 }

/* A request's own marks, and the one way they come off: only the request
   that put a mark there takes it away, so a newer request for the same key
   is never cleared by an older one landing late. */
function begin(key: string): number {
  tokens += 1
  inflight.set(key, tokens)
  patch({ loading: { ...store.get().loading, [key]: tokens } })
  return tokens
}

/* Takes the request off the in-flight map when it is the one there, and
   says so; the caller folds the loading mark's removal into whatever it
   writes next, so a page lands and its mark lifts in one write -- a write
   between the two would show the tab "not loading, nothing held" for one
   render, and that render would ask again. */
function settle(key: string, token: number): boolean {
  if (inflight.get(key) !== token) return false
  inflight.delete(key)
  return true
}

/** The state without this request's loading mark, when the mark is still its own. */
function unmarked(s: DetailsState, key: string, token: number): DetailsState {
  return s.loading[key] === token ? { ...s, loading: without(s.loading, key) } : s
}

/* The selected entry's identity as the LIST knows it: the conversation, the
   committed epoch and the row's revision. Null while any part is missing. */
function listed(): Identity | null {
  const s = list.get()
  if (s.sessionKey === null || s.epoch === null || s.selectedId === null) return null
  const entry = list.entry(s.selectedId)
  if (!entry) return null
  return { sessionKey: s.sessionKey, epoch: s.epoch, entryId: s.selectedId, revision: entry.revision }
}

/* Whether an answer may touch the pane: asked under the current ticket and
   the list's current generation, about the entry on screen, with the pane
   open. The list's generation moves on a handshake too, so an error from the
   connection before cannot act on the one after. */
const live = (t: number, g: number, entryId: string): boolean =>
  t === gen && g === list.gen() && store.get().open && list.get().selectedId === entryId

/** An answer's conversation and epoch are the ones on screen. */
const currentEpoch = (sessionKey: string, epoch: string): boolean => {
  const s = list.get()
  return s.sessionKey === sessionKey && s.epoch === epoch
}

/* Whether the pane may ask the gateway anything right now: the trajectory
   is on screen for a conversation the toggle may show, the list holds a
   whole snapshot to name an epoch, the pane is open, and nothing has told
   it to wait -- not an epoch the list has yet to reach, not a descriptor
   being read again, not a run of revision changes past the limit. The
   reader's own retry (`force`) overrides the last two; only a new list
   identity lifts the first. */
export function mayRead(s: DetailsState = store.get(), opts: { force?: boolean; what?: 'descriptor' | 'block' } = {}): boolean {
  const l = list.get()
  if (!s.open || l.view !== 'trajectory' || !list.available(l) || !l.snapshotReady || l.sessionKey === null) return false
  if (s.waitingEpoch !== null) return false
  if (!opts.force && s.unstable) return false
  /* A stale descriptor is read again; a block waits for that read, since the
     new descriptor decides which blocks there are and at which revision. */
  if (!opts.force && s.stale && opts.what !== 'descriptor') return false
  return true
}

/* ── the view ─────────────────────────────────────────────────────────── */

export const detailsWidthPx = (s: DetailsState = store.get()): number => {
  const ceiling = s.areaWidth > 0 ? Math.floor(s.areaWidth / 2) : Number.POSITIVE_INFINITY
  return Math.max(MIN_WIDTH, Math.min(s.width ?? DEFAULT_WIDTH, Math.max(MIN_WIDTH, ceiling)))
}

export const narrow = (s: DetailsState = store.get()): boolean => s.areaWidth > 0 && s.areaWidth < NARROW_BELOW

export function openDetails(): void {
  if (store.get().open) return
  bump()
  patch({ open: true, unstable: false })
}

/** Closes the pane; the selection stays, so the same row reopens it. */
export function closeDetails(): void {
  if (!store.get().open) return
  bump()
  patch({ open: false })
}

export function setWidth(px: number): void {
  patch({ width: Math.round(px) })
}

export function setAreaWidth(px: number): void {
  if (store.get().areaWidth === px) return
  patch({ areaWidth: px })
}

export const tabOf = (entryId: string): Tab => store.get().tabByEntry[entryId] ?? 'overview'

export function setTab(entryId: string, tab: Tab): void {
  if (tabOf(entryId) === tab) return
  bump()
  revisionHops = 0
  patch({ tabByEntry: { ...store.get().tabByEntry, [entryId]: tab }, unstable: false })
}

export function setTabScroll(entryId: string, tab: Tab, top: number): void {
  const key = scrollKey(entryId, tab)
  if (store.get().scrollByEntryTab[key] === top) return
  patch({ scrollByEntryTab: { ...store.get().scrollByEntryTab, [key]: top } })
}

export const tabScroll = (entryId: string, tab: Tab): number => store.get().scrollByEntryTab[scrollKey(entryId, tab)] ?? 0

/* ── the cache and its budget ─────────────────────────────────────────── */

/* The pane's current tab's record, which the budget treats last. */
function activeBlockKey(s: DetailsState): string | null {
  const id = s.current
  if (!id) return null
  const tab = s.tabByEntry[id.entryId] ?? 'overview'
  return tab === 'overview' ? null : blockKey(id, tab)
}

const usage = (s: DetailsState): number =>
  Object.values(s.descriptors).reduce((n, r) => n + r.bytes, 0)
  + Object.values(s.blocks).reduce((n, r) => n + r.bytes, 0)
  + Object.values(s.outlines).reduce((n, r) => n + r.bytes, 0)

/* Brings the cache back under the budget, oldest first in three rounds:
   records of any other identity, then the current entry's other tabs, then
   the current tab's earliest pages. A record let go takes its loading mark
   with it, so a tab that needs it again asks again rather than waiting. */
function trim(s: DetailsState): DetailsState {
  let next = s
  const over = (): boolean => usage(next) > BUDGET
  if (!over()) return next
  const active = activeBlockKey(next)
  const byAge = (keys: string[], at: (k: string) => number): string[] => [...keys].sort((a, b) => at(a) - at(b))
  const drop = (key: string): void => {
    next = {
      ...next,
      descriptors: without(next.descriptors, key),
      blocks: without(next.blocks, key),
      outlines: without(next.outlines, key),
      loading: without(next.loading, key),
    }
  }
  const foreign = (id: Identity): boolean => !sameIdentity(id, next.current)
  for (const key of byAge(Object.keys(next.outlines).filter((k) => foreign(next.outlines[k]!.identity)), (k) => next.outlines[k]!.at)) {
    if (!over()) return next
    drop(key)
  }
  for (const key of byAge(Object.keys(next.blocks).filter((k) => foreign(next.blocks[k]!.identity)), (k) => next.blocks[k]!.at)) {
    if (!over()) return next
    drop(key)
  }
  for (const key of byAge(Object.keys(next.descriptors).filter((k) => foreign(next.descriptors[k]!.identity)), (k) => next.descriptors[k]!.at)) {
    if (!over()) return next
    drop(key)
  }
  for (const key of byAge(Object.keys(next.blocks).filter((k) => k !== active), (k) => next.blocks[k]!.at)) {
    if (!over()) return next
    drop(key)
  }
  if (active && next.blocks[active]) {
    let record = next.blocks[active]!
    while (over() && record.pages.length > 1) {
      record = dropEarliest(record)
      next = { ...next, blocks: { ...next.blocks, [active]: record } }
    }
  }
  return next
}

/* The record without its earliest page: the window slides and the pane says
   how many items went before what it still holds. */
function dropEarliest(record: BlockRecord): BlockRecord {
  const [first, ...rest] = [...record.pages].sort((a, b) => a.offset - b.offset)
  if (!first || !rest.length) return record
  const second = rest[0]!
  const dropped = second.offset - first.offset
  const pages = rest
  return { ...record, pages, droppedBefore: record.droppedBefore + dropped, bytes: pages.reduce((n, p) => n + bytesOf(p.data), 0) }
}

/* Descriptors have a count as well as the shared byte budget: twenty, the
   current one exempt. */
function trimDescriptors(s: DetailsState): DetailsState {
  const keys = Object.keys(s.descriptors)
  if (keys.length <= DESCRIPTORS_MAX) return s
  const current = s.current ? descriptorKey(s.current) : null
  const victims = keys
    .filter((k) => k !== current)
    .sort((a, b) => s.descriptors[a]!.at - s.descriptors[b]!.at)
    .slice(0, keys.length - DESCRIPTORS_MAX)
  let descriptors = s.descriptors
  for (const k of victims) descriptors = without(descriptors, k)
  return { ...s, descriptors }
}

function commit(next: DetailsState): void {
  store.set(trim(trimDescriptors(next)))
}

const touch = (): number => { clock += 1; return clock }

/** The descriptor the pane shows, when the current identity's is cached. */
export const descriptor = (s: DetailsState = store.get()): TrajectoryDetailResult | null =>
  s.current ? (s.descriptors[descriptorKey(s.current)]?.value ?? null) : null

/** The current identity's record for a block, when cached. */
export const block = (blockId: string, s: DetailsState = store.get()): BlockRecord | null =>
  s.current ? (s.blocks[blockKey(s.current, blockId)] ?? null) : null

/* ── reading the descriptor ───────────────────────────────────────────── */

/* A descriptor arrived. Filed under the identity it answers with; then, if
   it is still about the entry on screen, accepted as the pane's identity. A
   changed revision drops what was read for the one before, and the reader's
   tab is kept where the new descriptor still has it. */
function accept(result: TrajectoryDetailResult, t: number, g: number, mark: string | null = null, token = 0): void {
  const id: Identity = {
    sessionKey: result.session_key, epoch: result.epoch, entryId: result.entry_id, revision: result.entry_revision,
  }
  const s = mark === null ? store.get() : unmarked(store.get(), mark, token)
  if (!currentEpoch(id.sessionKey, id.epoch)) {
    /* The gateway is ahead of the list. Nothing to show from this, and
       nothing to ask again until the list brings that epoch. */
    const waiting = live(t, g, id.entryId) && list.get().sessionKey === id.sessionKey
    if (waiting) store.set({ ...s, waitingEpoch: id.epoch })
    else if (s !== store.get()) store.set(s)
    return
  }
  const key = descriptorKey(id)
  let next: DetailsState = {
    ...s,
    descriptors: { ...s.descriptors, [key]: { identity: id, value: result, bytes: bytesOf(result), at: touch() } },
  }
  if (!live(t, g, id.entryId)) { commit(next); return }
  /* A page already said the entry is further on than this, in this very
     epoch: filed, but it is not the descriptor the pane is waiting for, and
     the wait goes on. A bound from another epoch has nothing to say. */
  if (s.pending !== null && s.pending.epoch === id.epoch && id.revision < s.pending.revision) { commit(next); return }
  next = { ...next, faults: without(next.faults, keyOf('descriptor', id.entryId)), stale: false, pending: null }
  const was = s.current
  if (!sameIdentity(was, id)) {
    bump()
    if (was && was.entryId === id.entryId) {
      /* The entry moved on under the reader: the bodies read for the old
         revision are not the new one's, and a cursor into them is dead. */
      const blocks = { ...next.blocks }
      for (const k of Object.keys(blocks)) {
        const r = blocks[k]!
        if (r.identity.entryId === id.entryId && !sameIdentity(r.identity, id)) delete blocks[k]
      }
      const outlines = { ...next.outlines }
      for (const k of Object.keys(outlines)) {
        const r = outlines[k]!
        if (r.identity.entryId === id.entryId && !sameIdentity(r.identity, id)) delete outlines[k]
      }
      next = { ...next, blocks, outlines }
      revisionHops += 1
      if (revisionHops > REVISION_RETRIES) next = { ...next, unstable: true }
    }
    const tab = next.tabByEntry[id.entryId] ?? 'overview'
    if (tab !== 'overview' && !result.blocks.some((b) => b.id === tab)) {
      next = { ...next, tabByEntry: { ...next.tabByEntry, [id.entryId]: 'overview' } }
    }
    next = { ...next, current: id }
  }
  commit(next)
}

/* Reads the selected entry's descriptor; one read at a time. The cache
   answers a visit back to an entry, or an answer that landed after the reader
   had moved on and was filed anyway -- unless the caller knows the entry has
   moved past what the list says (`fresh`), in which case only the gateway
   can say where it is now. */
export async function loadDescriptor(opts: { fresh?: boolean; force?: boolean } = {}): Promise<void> {
  const src = list.source()
  const id = listed()
  if (!src || !id || !mayRead(store.get(), { force: opts.force, what: 'descriptor' })) return
  const pending = store.get().pending
  const behind = pending !== null && pending.epoch === id.epoch && id.revision < pending.revision
  if (!opts.fresh && !behind) {
    const cached = store.get().descriptors[descriptorKey(id)]
    if (cached) { accept(cached.value, gen, list.gen()); return }
  }
  const mark = keyOf('descriptor', id.entryId)
  if (inflight.has(mark)) return
  const t = gen
  const g = list.gen()
  const token = begin(mark)
  try {
    const result = await src.detail(id.sessionKey, id.entryId)
    settle(mark, token)
    accept(result, t, g, mark, token)
  } catch (e) {
    settle(mark, token)
    const s = unmarked(store.get(), mark, token)
    if (!currentEpoch(id.sessionKey, id.epoch)) { store.set(s); return }
    if (isDisabled(e) || saysAbsent(e)) {
      /* A verdict about the surface acts only for the connection it came
         from: the list's generation moves on a handshake. */
      store.set(s)
      if (g === list.gen()) list.liveFailed(e)
      return
    }
    if (!live(t, g, id.entryId)) { store.set(s); return }
    if (isEntryGone(e)) {
      bump()
      store.set({ ...s, open: false })
      list.select(null, { source: 'migrate' })
      return
    }
    store.set({ ...s, faults: { ...s.faults, [mark]: (e as Error)?.message || String(e) } })
  }
}

/* ── reading a block ──────────────────────────────────────────────────── */

function pageOf(result: TrajectoryBlockResult, offset: number): BlockPage {
  return {
    offset,
    data: result.data ?? null,
    availability: result.availability,
    reason: result.reason ?? null,
    integrity: [...result.integrity],
    truncated: result.truncated,
    total: typeof result.total_items === 'number' ? result.total_items : null,
  }
}

const offsetOf = (data: JsonValue | null | undefined): number => {
  const o = data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as { offset?: unknown }).offset : undefined
  return typeof o === 'number' ? o : 0
}

/* A page arrived. Filed under the identity it was asked for, appended to the
   record it continues when that record is still there; the window slides
   when it is full. The pane itself changes only under the ticket. */
function filePage(
  id: Identity, blockId: string, result: TrajectoryBlockResult, t: number, g: number, continuing: boolean,
  mark: string, token: number,
): void {
  const s = unmarked(store.get(), mark, token)
  if (!currentEpoch(id.sessionKey, id.epoch)
    || result.epoch !== id.epoch || result.entry_revision !== id.revision || result.entry_id !== id.entryId) {
    if (s !== store.get()) store.set(s)
    return
  }
  const key = blockKey(id, blockId)
  const page = pageOf(result, offsetOf(result.data))
  const have = s.blocks[key]
  let record: BlockRecord = continuing && have
    ? {
      ...have,
      /* A page may land at any offset now that rows are opened by their own cursor; one offset, one page. */
      pages: [...have.pages.filter((p) => p.offset !== page.offset), page],
      nextCursor: result.next_cursor ?? null,
      at: touch(),
    }
    : {
      identity: id, blockId, renderer: result.renderer, pages: [page], nextCursor: result.next_cursor ?? null,
      droppedBefore: 0, bytes: 0, at: touch(),
    }
  while (record.pages.length > PAGE_WINDOW) record = dropEarliest(record)
  record = { ...record, bytes: record.pages.reduce((n, p) => n + bytesOf(p.data), 0) }
  let next: DetailsState = { ...s, blocks: { ...s.blocks, [key]: record } }
  if (live(t, g, id.entryId)) next = { ...next, faults: without(next.faults, mark) }
  commit(next)
}

async function read(blockId: string, cursor: string | null, continuing: boolean, force = false): Promise<void> {
  const src = list.source()
  const id = store.get().current
  if (!src || !id || !mayRead(store.get(), { force })) return
  const key = blockKey(id, blockId)
  const mark = keyOf('block', key, continuing ? 'more' : 'first')
  if (inflight.has(mark)) return
  const t = gen
  const g = list.gen()
  const token = begin(mark)
  patch({ faults: without(store.get().faults, mark) })
  try {
    const result = await src.block(id.sessionKey, id.entryId, id.revision, id.epoch, blockId, cursor)
    settle(mark, token)
    filePage(id, blockId, result, t, g, continuing, mark, token)
  } catch (e) {
    settle(mark, token)
    const s = unmarked(store.get(), mark, token)
    if (!currentEpoch(id.sessionKey, id.epoch)) { store.set(s); return }
    if (isDisabled(e) || saysAbsent(e)) {
      store.set(s)
      if (g === list.gen()) list.liveFailed(e)
      return
    }
    if (!live(t, g, id.entryId)) { store.set(s); return }
    const moved = revisionChange(e)
    if (moved) {
      /* The entry moved on: not the same block again at a guessed revision,
         but the descriptor, which brings the revision and the tabs with it.
         An epoch the list has not reached yet is the list's to bring. */
      if (moved.epoch !== id.epoch) { store.set({ ...s, waitingEpoch: moved.epoch }); return }
      if (revisionHops >= REVISION_RETRIES) { store.set({ ...s, unstable: true }); return }
      /* One write: stale, and the revision the pane now knows about. A
         subscriber that asks for the descriptor on this very notification
         cannot be answered from the cache at the list's older revision, so
         the first read anyone starts is the fresh one. */
      const floor = s.pending !== null && s.pending.epoch === moved.epoch ? Math.max(s.pending.revision, moved.revision) : moved.revision
      store.set({ ...s, stale: true, pending: { epoch: moved.epoch, revision: floor } })
      void loadDescriptor({ fresh: true })
      return
    }
    if (isEntryGone(e)) {
      bump()
      store.set({ ...s, open: false })
      list.select(null, { source: 'migrate' })
      return
    }
    store.set({ ...s, faults: { ...s.faults, [mark]: (e as Error)?.message || String(e) } })
  }
}

/** The first page of a block for the pane's current identity, unless cached. */
export function loadBlock(blockId: string): Promise<void> {
  if (block(blockId)) return Promise.resolve()
  return read(blockId, null, false)
}

/** The next page of a block, when there is one. */
export function loadMore(blockId: string): Promise<void> {
  const have = block(blockId)
  if (!have || have.nextCursor === null) return Promise.resolve()
  return read(blockId, have.nextCursor, true)
}

/* The page a message list opens at a given row: asked for by the cursor the
   outline carries for that row, kept beside whatever pages are already held.
   Nothing is asked when the row's page is in. */
export function loadPageAt(blockId: string, cursor: string, index: number): Promise<void> {
  const have = block(blockId)
  if (have && pageHolding(have, index) !== null) return Promise.resolve()
  return read(blockId, cursor, have !== null)
}

/** The held page whose items cover message `index`, or null. */
export function pageHolding(record: BlockRecord, index: number): BlockPage | null {
  for (const page of record.pages) {
    const data = page.data
    const items = data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as { items?: unknown }).items : undefined
    const n = Array.isArray(items) ? items.length : 0
    if (index >= page.offset && index < page.offset + n) return page
  }
  return null
}

/** The message at `index` from the held pages, or undefined when its page is not in. */
export function messageAt(record: BlockRecord, index: number): unknown {
  const page = pageHolding(record, index)
  if (!page) return undefined
  const items = (page.data as { items: unknown[] }).items
  return items[index - page.offset]
}

/* ── the outline of a message list ───────────────────────────────────── */

const outlineKey = (id: Identity): string => descriptorKey(id)

/** The current identity's outline, when any page of it is in. */
export const outline = (s: DetailsState = store.get()): OutlineRecord | null =>
  s.current ? (s.outlines[outlineKey(s.current)] ?? null) : null

const outlineItems = (data: JsonValue | null | undefined): OutlineItem[] => {
  const items = data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as { items?: unknown }).items : undefined
  if (!Array.isArray(items)) return []
  return items.filter((it): it is OutlineItem => it !== null && typeof it === 'object' && typeof (it as OutlineItem).index === 'number')
}

const emptyOutline = (id: Identity): OutlineRecord =>
  ({ identity: id, items: [], total: null, nextCursor: null, loading: false, fault: null, bytes: 0, at: touch() })

/* Walks every page of the outline block in order, in the background: each
   answer is filed under the identity it was asked for and the next page is
   asked for at once, until the gateway says the list ends. The walk stops
   when the pane moves on; a later visit to the same identity resumes from the
   cursor it holds. */
export async function loadOutline(): Promise<void> {
  const src = list.source()
  const id = store.get().current
  if (!src || !id || !mayRead(store.get())) return
  const key = outlineKey(id)
  const have = store.get().outlines[key]
  if (have && (have.loading || have.fault !== null || (have.nextCursor === null && have.items.length > 0))) return
  const mark = keyOf('outline', key)
  if (inflight.has(mark)) return
  const t = gen
  const g = list.gen()
  const token = begin(mark)
  const write = (apply: (record: OutlineRecord) => OutlineRecord, s: DetailsState = store.get()): void => {
    commit({ ...s, outlines: { ...s.outlines, [key]: apply(s.outlines[key] ?? emptyOutline(id)) } })
  }
  write((r) => ({ ...r, loading: true }))
  let cursor: string | null = have?.nextCursor ?? null
  try {
    for (;;) {
      const result: TrajectoryBlockResult = await src.block(id.sessionKey, id.entryId, id.revision, id.epoch, 'outline', cursor)
      if (!currentEpoch(id.sessionKey, id.epoch) || result.epoch !== id.epoch || result.entry_revision !== id.revision || result.entry_id !== id.entryId) break
      cursor = result.next_cursor ?? null
      const fresh = outlineItems(result.data)
      const total = typeof result.total_items === 'number' ? result.total_items : null
      write((r) => {
        const byIndex = new Map(r.items.map((it) => [it.index, it]))
        for (const it of fresh) byIndex.set(it.index, it)
        const items = [...byIndex.values()].sort((a, b) => a.index - b.index)
        return { ...r, items, total: total ?? r.total, nextCursor: cursor, bytes: bytesOf(items), at: touch() }
      })
      if (cursor === null || !live(t, g, id.entryId)) break
    }
    settle(mark, token)
    write((r) => ({ ...r, loading: false }), unmarked(store.get(), mark, token))
  } catch (e) {
    settle(mark, token)
    const s = unmarked(store.get(), mark, token)
    if (!currentEpoch(id.sessionKey, id.epoch) || !live(t, g, id.entryId)) { store.set(s); return }
    if (isDisabled(e) || saysAbsent(e)) {
      store.set(s)
      if (g === list.gen()) list.liveFailed(e)
      return
    }
    const moved = revisionChange(e)
    if (moved) {
      if (moved.epoch !== id.epoch) { store.set({ ...s, waitingEpoch: moved.epoch }); return }
      store.set({ ...s, stale: true, pending: { epoch: moved.epoch, revision: moved.revision } })
      void loadDescriptor({ fresh: true })
      return
    }
    write((r) => ({ ...r, loading: false, fault: (e as Error)?.message || String(e) }), s)
  }
}

/** The reader's retry after a failed outline walk. */
export function retryOutline(): Promise<void> {
  const id = store.get().current
  if (id) {
    const key = outlineKey(id)
    const s = store.get()
    if (s.outlines[key]) store.set({ ...s, outlines: { ...s.outlines, [key]: { ...s.outlines[key]!, fault: null } } })
  }
  return loadOutline()
}

/** The reader's own retry: drops what is held for a block and reads it from its first page. */
export function reloadBlock(blockId: string): Promise<void> {
  const id = store.get().current
  if (id) {
    const key = blockKey(id, blockId)
    const s = store.get()
    store.set({ ...s, blocks: without(s.blocks, key), faults: without(s.faults, keyOf('block', key, 'first')) })
  }
  return read(blockId, null, false, true)
}

/* The reader's own retry of the descriptor: the run of revision changes that
   stopped the pane is forgiven, and the gateway is asked where the entry is
   now. */
export function retryDescriptor(): Promise<void> {
  revisionHops = 0
  const s = store.get()
  store.set({ ...s, unstable: false, faults: without(s.faults, keyOf('descriptor', s.current?.entryId ?? list.get().selectedId ?? '')) })
  return loadDescriptor({ fresh: true, force: true })
}

/* Whether what is held for a block is less than the whole: the gateway cut
   it, pages are still to come, or the earliest pages were let go to stay in
   the window -- a last page with no cursor is still partial then. */
export const isPartial = (record: BlockRecord): boolean => {
  if (record.droppedBefore > 0 || record.pages.some((p) => p.truncated || p.availability === 'truncated')) return true
  const total = record.pages.find((p) => p.total !== null)?.total ?? null
  if (total === null) return record.nextCursor !== null
  return heldCount(record) < total
}

/** How many items the held pages carry between them. */
export const heldCount = (record: BlockRecord): number =>
  record.pages.reduce((n, p) => {
    const data = p.data
    const items = data !== null && typeof data === 'object' && !Array.isArray(data) ? (data as { items?: unknown }).items : undefined
    return n + (Array.isArray(items) ? items.length : 0)
  }, 0)

/** The lowest offset among the held pages, or null with none held. */
export const firstHeldOffset = (record: BlockRecord): number | null =>
  record.pages.length ? Math.min(...record.pages.map((p) => p.offset)) : null

/** Whether a read is in the air for the pane's current identity. */
export const isLoading = (what: 'descriptor' | { blockId: string; more?: boolean }, s: DetailsState = store.get()): boolean => {
  if (what === 'descriptor') return s.current !== null ? keyOf('descriptor', s.current.entryId) in s.loading : (list.get().selectedId !== null && keyOf('descriptor', list.get().selectedId as string) in s.loading)
  if (!s.current) return false
  return keyOf('block', blockKey(s.current, what.blockId), what.more ? 'more' : 'first') in s.loading
}

export const fault = (what: 'descriptor' | { blockId: string; more?: boolean }, s: DetailsState = store.get()): string | null => {
  if (what === 'descriptor') {
    const entryId = s.current?.entryId ?? list.get().selectedId
    return entryId !== null ? (s.faults[keyOf('descriptor', entryId)] ?? null) : null
  }
  if (!s.current) return null
  return s.faults[keyOf('block', blockKey(s.current, what.blockId), what.more ? 'more' : 'first')] ?? null
}

/* ── following the list ───────────────────────────────────────────────── */

interface Seen {
  sessionKey: string | null
  epoch: string | null
  selectedId: string | null
  revision: number | null
  selectedBy: string | null
  /** The trajectory is on screen for a conversation the toggle may show, over a whole snapshot. */
  permitted: boolean
}

const nothingSeen: Seen = { sessionKey: null, epoch: null, selectedId: null, revision: null, selectedBy: null, permitted: false }

const seenNow = (): Seen => {
  const s = list.get()
  const entry = s.selectedId !== null ? list.entry(s.selectedId) : null
  return {
    sessionKey: s.sessionKey, epoch: s.epoch, selectedId: s.selectedId,
    revision: entry ? entry.revision : null, selectedBy: s.selectedBy,
    permitted: s.view === 'trajectory' && list.available(s) && s.snapshotReady,
  }
}

let seen: Seen = nothingSeen

/* The list's own writes drive this pane: a new conversation clears
   everything; a new selection moves the ticket and opens the pane (unless the
   selection was the list's own migration); the selected row's revision
   moving marks the descriptor stale. */
function follow(): void {
  const now = seenNow()
  const was = seen
  seen = now
  if (now.sessionKey !== was.sessionKey) {
    bump()
    inflight.clear()
    revisionHops = 0
    store.set({ ...initial, paused: !now.permitted, width: store.get().width, areaWidth: store.get().areaWidth })
    return
  }
  /* The view left or came back, the switch flipped, the snapshot was let go:
     whatever was in the air is about a pane that is not asking any more, and
     a pane that may ask again starts afresh. The switch and the bodies stay;
     the mirror flips so the pane's components look again. */
  if (now.permitted !== was.permitted) {
    bump()
    patch({ paused: !now.permitted })
  }
  if (now.selectedId !== was.selectedId) {
    bump()
    revisionHops = 0
    const d = store.get()
    const open = now.selectedId === null ? false : (now.selectedBy !== 'migrate' ? true : d.open)
    patch({ open, current: null, stale: false, unstable: false, waitingEpoch: null, pending: null })
    return
  }
  if (now.epoch !== was.epoch) {
    /* The list reached a new epoch: whatever the pane was waiting for, this
       is the answer, and what it shows is to be read again under it. The
       revision bound and the hop count were about the epoch before. */
    revisionHops = 0
    const d = store.get()
    if (d.waitingEpoch !== null || d.pending !== null || d.current !== null) {
      patch({ waitingEpoch: null, pending: null, unstable: false, stale: d.current !== null })
    }
    return
  }
  if (now.selectedId !== null && now.revision !== was.revision && store.get().current !== null) {
    patch({ stale: true })
  }
}

/** Wire the pane to the list's store, once. */
export function install(): void {
  if (unfollow) return
  seen = seenNow()
  if (store.get().paused === seen.permitted) patch({ paused: !seen.permitted })
  unfollow = list.subscribe(follow)
}

/* Escape closes the pane only while the pane holds the focus: a key pressed
   in the composer, or meant for a sheet above, is not the pane's to spend. */
export const hasFocus = (): boolean => {
  const pane = document.querySelector('.trajectory-details')
  return !!pane && pane.contains(document.activeElement)
}

/* The one layer the two surfaces share: the dense block's pick list, which
   is open or not, and the pane, which counts only while it holds the focus.
   The list goes first, being the thing on top. */
onTrajectoryEscape({
  id: 'trajectory.escapeOpen()',
  isOpen: () => list.get().timeline.bucket !== null || (store.get().open && hasFocus()),
  close: () => {
    if (list.get().timeline.bucket !== null) list.closeBucket()
    else closeDetails()
  },
})

export function _resetForTests(): void {
  store._resetForTests()
  gen = 0
  clock = 0
  tokens = 0
  revisionHops = 0
  inflight.clear()
  if (unfollow) { unfollow(); unfollow = null }
  seen = nothingSeen
}
