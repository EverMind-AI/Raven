import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { KbBase, KbDoc, KbHit, KbSettings, KbStatus, KnowledgeSource } from './types'

interface State {
  bases: KbBase[]
  status: KbStatus | null
  /* Distinct from `bases.length === 0`: nothing has been asked yet, so the page
     shows neither a list nor an empty note. Without it the first paint claims
     there are no bases before the answer has landed. */
  loaded: boolean
  /* The read failed. Kept rather than swallowed, because a page that shows an
     empty list when the engine is unreachable is telling the reader something
     untrue about their own data. */
  failed: string | null
  /* The base whose documents are on screen, and those documents. Null is the
     list view; the two are set together so a stale panel cannot outlive the
     row it belonged to. */
  openId: string | null
  docs: KbDoc[]
  /* A write is in flight. One at a time, because two creates from a
     double-click are two bases. */
  busy: boolean
  /* The last search and its hits. Kept on the base rather than in the drawer
     so going back and returning does not silently drop what was asked. */
  query: string
  hits: KbHit[] | null
  /* What the last search cost, for the line above the results. Null until one
     has been run, which is a different state from one that found nothing. */
  cost: { search_ms: number; embed_ms: number } | null
  /* A search is in flight. Its own flag rather than `busy`: that one gates
     every write on the page, and a search changes nothing. */
  searching: boolean
  /* The recall panel is up. */
  recall: boolean
  /* The settings panel is up. */
  settings: boolean
  /* Which rows are ticked, by document id. A set rather than a flag on each
     row: the rows are re-read from the engine on every reload, and a flag
     would be lost with them. */
  picked: string[]
  /* The document whose original file is on screen, or null for the table.
     Held as the record rather than an id: the viewer needs the name and the
     media type to decide what it is showing, and a row that is deleted while
     open should not leave the panel looking up an id that is gone. */
  viewing: KbDoc | null
  /* Which add-a-source dialog is up, if any. One field rather than a boolean
     each, because two of them open at once is not a state this page has. */
  dialog: Dialog | null
  /* How far a multi-file add has got. Null when nothing is being added; a
     folder is uploaded one file at a time, and without this the tab looks
     frozen for as long as that takes. */
  adding: { done: number; total: number } | null
  /* A drag is over the panel. Counted rather than set, because dragging across
     a child element fires leave-then-enter and a boolean flickers off. */
  dragDepth: number
}

/* A dialog, and what it was opened on: `doc` is the note being rewritten, and
   its absence means a new one. */
export interface Dialog {
  kind: 'note' | 'url'
  doc?: KbDoc
}

const EMPTY: State = {
  bases: [],
  status: null,
  loaded: false,
  failed: null,
  openId: null,
  docs: [],
  busy: false,
  query: '',
  hits: null,
  cost: null,
  searching: false,
  recall: false,
  settings: false,
  picked: [],
  viewing: null,
  dialog: null,
  adding: null,
  dragDepth: 0,
}

let state: State = EMPTY
const listeners = new Set<() => void>()

function set(patch: Partial<State>): void {
  state = { ...state, ...patch }
  listeners.forEach((fn) => fn())
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getState(): State {
  return state
}

function source(): KnowledgeSource {
  const ds = (globalThis as { DS?: { knowledge?: KnowledgeSource } }).DS
  if (!ds || !ds.knowledge) throw new Error('no knowledge source installed')
  return ds.knowledge
}

export async function load(): Promise<void> {
  let src: KnowledgeSource
  try {
    src = source()
  } catch (e) {
    set({ loaded: true, failed: (e as Error).message })
    return
  }
  try {
    /* Both together: the page cannot say "no bases yet, create one" without
       knowing whether creating one is even possible. */
    const [status, bases] = await Promise.all([src.status(), src.bases()])
    set({ status, bases, loaded: true, failed: null })
  } catch (e) {
    set({ loaded: true, failed: (e as Error)?.message || String(e) })
  }
}

export async function create(name: string, description = '', embedding = true): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed || state.busy) return
  set({ busy: true })
  try {
    await source().create(trimmed, description, embedding)
    await load()
  } catch (e) {
    /* Creating measures the model's width against the endpoint, so it reaches
       the network and can fail. Said out loud: a base that is not there is not
       something to discover later. */
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
  }
}

export function remove(base: KbBase): void {
  if (state.busy) return
  shell().confirmAsk(
    shell().T('gui.kb.delete'),
    shell().T('gui.kb.delete_body', { name: base.name, n: base.documents }),
    shell().T('gui.kb.delete'),
    () => {
      set({ busy: true })
      void source()
        .remove(base.id)
        .then(() => {
          /* The open panel belonged to the base that just went. */
          if (state.openId === base.id) set({ openId: null, docs: [] })
          return load()
        })
        .catch((e: unknown) => toast((e as Error)?.message || String(e)))
        .finally(() => set({ busy: false }))
    },
  )
}

export async function open_(id: string): Promise<void> {
  /* A query typed in the base being left must not spend a request, nor land
     its hits in the base being opened. */
  cancelSearch()
  set({ openId: id, docs: [], query: '', hits: null, viewing: null, picked: [] })
  try {
    const docs = await source().documents(id)
    /* The reader may have gone back or opened another base while this was in
       flight; answering into the wrong panel is worse than not answering. */
    if (state.openId === id) set({ docs })
  } catch (e) {
    if (state.openId === id) toast((e as Error)?.message || String(e))
  }
}

export function back(): void {
  cancelSearch()
  set({ openId: null, docs: [], query: '', hits: null, viewing: null, picked: [] })
}

/* At most this many files out of one folder. A source tree holds tens of
   thousands, each upload is its own request, and starting that run because
   somebody picked the wrong directory is not a thing to do quietly. */
export const FOLDER_MAX = 100

/* Take one document in, then index it. The add and the index are separate
   calls because embedding outlives a request; between them the row shows as
   queued, which is what it is.

   `arrived` runs the moment the row exists. A dialog that created it has
   nothing left to say at that point -- the row's own status column is what
   reports the indexing -- so it closes there rather than holding the reader
   in front of a modal until the embedding finishes. */
async function takeIn(baseId: string, add: () => Promise<KbDoc>, arrived?: () => void): Promise<void> {
  const doc = await add()
  if (state.openId === baseId) set({ docs: [...state.docs, doc] })
  arrived?.()
  const indexed = await source().index(doc.id)
  if (state.openId === baseId) {
    set({ docs: state.docs.map((d) => (d.id === indexed.id ? indexed : d)) })
  }
}

export async function upload(file: File): Promise<void> {
  return uploadAll([file])
}

/* Files, in order, one at a time.

   Sequential rather than parallel: each upload carries its bytes base64 in a
   single websocket frame under a 25 MB ceiling, so N at once is N of those in
   memory at once and a frame ceiling nobody raised.

   One file's failure is reported at the end rather than thrown: a folder of
   forty where the third is unreadable should add the other thirty-nine, which
   is also how ragflow reports a partial upload. */
export async function uploadAll(files: File[]): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy || !files.length) return
  set({ busy: true, adding: { done: 0, total: files.length } })
  let failed = 0
  let firstError = ''
  try {
    for (const file of files) {
      if (state.openId !== baseId) break
      try {
        await takeIn(baseId, () => source().upload(baseId, file))
      } catch (e) {
        failed += 1
        if (!firstError) firstError = (e as Error)?.message || String(e)
      }
      set({ adding: { done: (state.adding?.done || 0) + 1, total: files.length } })
    }
  } finally {
    set({ busy: false, adding: null })
    if (failed === 1) toast(firstError)
    else if (failed > 1) toast(t('gui.kb.some_failed', { count: failed }))
    /* The base's own document count lives on the row behind this panel. */
    void load()
  }
}

/* Every file in a dropped or picked folder that this build can actually index.

   Filtered here rather than uploaded and left to fail: a source tree is mostly
   things no parser claims -- binaries, images, lockfiles -- and forty failed
   rows is not a file list. The extensions come from the engine, which is the
   only thing that knows what its parsers were built with.

   A base created without an embedding model indexes nothing at all, so there
   is no such thing as an unparseable file in one: it keeps documents to open
   and to hand to a turn, and filtering by what a parser claims would throw
   away exactly what it is for. */
export function indexable(files: File[]): File[] {
  const base = state.bases.find((b) => b.id === state.openId)
  if (base && !base.embedding_model) return files
  const allowed = state.status?.extensions
  if (!allowed || !allowed.length) return files
  return files.filter((f) => allowed.includes(suffix(f.name)))
}

function suffix(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot).toLowerCase() : ''
}

/* A folder, as the browser hands it over: already walked, already flat. */
export async function uploadFolder(files: File[]): Promise<void> {
  const supported = indexable(files)
  if (!supported.length) {
    toast(t('gui.kb.none_supported', { kinds: (state.status?.extensions || []).join(' ') }))
    return
  }
  if (supported.length > FOLDER_MAX) {
    toast(t('gui.kb.too_many', { count: supported.length, limit: FOLDER_MAX }))
    return
  }
  return uploadAll(supported)
}

export function openDialog(kind: 'note' | 'url', doc?: KbDoc): void {
  set({ dialog: { kind, doc } })
}

export function closeDialog(): void {
  set({ dialog: null })
}

/* Counted, not set: dragging over a child fires leave on the parent, and a
   boolean would flicker the highlight off mid-drag. */
export function dragEnter(): void {
  set({ dragDepth: state.dragDepth + 1 })
}

export function dragLeave(): void {
  set({ dragDepth: Math.max(0, state.dragDepth - 1) })
}

export function dragEnd(): void {
  set({ dragDepth: 0 })
}

export async function addNote(title: string, text: string): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy || !text.trim()) return
  set({ busy: true })
  try {
    await takeIn(baseId, () => source().addNote(baseId, title, text), () => set({ dialog: null }))
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
    void load()
  }
}

export async function saveNote(doc: KbDoc, title: string, text: string): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy || !text.trim()) return
  set({ busy: true })
  try {
    const written = await source().updateNote(doc.id, title, text)
    /* Replaced in place rather than appended: this is the row the reader
       opened, and a second copy of it is not what editing means. */
    if (state.openId === baseId) {
      set({ docs: state.docs.map((d) => (d.id === written.id ? written : d)) })
    }
    set({ dialog: null })
    const indexed = await source().index(written.id)
    if (state.openId === baseId) {
      set({ docs: state.docs.map((d) => (d.id === indexed.id ? indexed : d)) })
    }
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
    void load()
  }
}

export async function addUrl(url: string): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy || !url.trim()) return
  set({ busy: true })
  try {
    await takeIn(baseId, () => source().addUrl(baseId, url.trim()), () => set({ dialog: null }))
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
    void load()
  }
}

/* ── picking rows ──────────────────────────────────────────────────────

   Two things a reader does to a row -- index it again, be rid of it -- read
   the same on twenty rows as on one, and doing either twenty times through a
   per-row menu is the same click twenty times. */
export function togglePick(id: string): void {
  set({
    picked: state.picked.includes(id)
      ? state.picked.filter((p) => p !== id)
      : [...state.picked, id],
  })
}

/* The header tick. On when every row is picked, and pressing it then clears
   rather than re-picking, which is what every list of checkboxes does. */
export function pickAll(on: boolean): void {
  set({ picked: on ? state.docs.map((d) => d.id) : [] })
}

export function clearPicks(): void {
  set({ picked: [] })
}

/* The picked rows, in the order the list shows them rather than the order they
   were ticked in: this is what the actions below report progress against. */
function pickedDocs(): KbDoc[] {
  return state.docs.filter((d) => state.picked.includes(d.id))
}

/* Index every picked row again, one at a time.

   Sequential for the same reason an upload is: each one embeds its chunks
   through the configured endpoint, and twenty at once is twenty of those in
   flight against a rate limit nobody raised. One failing is recorded on its
   own row and does not stop the rest. */
export async function reindexPicked(): Promise<void> {
  const baseId = state.openId
  const rows = pickedDocs()
  if (!baseId || state.busy || !rows.length) return
  set({
    busy: true,
    adding: { done: 0, total: rows.length },
    docs: state.docs.map((d) =>
      state.picked.includes(d.id) ? { ...d, status: 'indexing', error: '' } : d,
    ),
  })
  let failed = 0
  let firstError = ''
  try {
    for (const doc of rows) {
      if (state.openId !== baseId) break
      try {
        const indexed = await source().index(doc.id)
        if (state.openId === baseId) {
          set({ docs: state.docs.map((d) => (d.id === indexed.id ? indexed : d)) })
        }
      } catch (e) {
        failed += 1
        if (!firstError) firstError = (e as Error)?.message || String(e)
        /* Put the row back the way it was: the optimistic `indexing` above is
           a promise this call just failed to keep. */
        if (state.openId === baseId) {
          set({ docs: state.docs.map((d) => (d.id === doc.id ? doc : d)) })
        }
      }
      set({ adding: { done: (state.adding?.done || 0) + 1, total: rows.length } })
    }
  } finally {
    set({ busy: false, adding: null })
    if (failed === 1) toast(firstError)
    else if (failed > 1) toast(t('gui.kb.some_failed', { count: failed }))
    void load()
  }
}

/* Remove every picked row, after asking once for all of them.

   Once rather than per row: twenty confirmations is a dialog a reader clicks
   through without reading, which is worse than one that names the number. */
export function removePicked(): void {
  const baseId = state.openId
  const rows = pickedDocs()
  if (!baseId || !rows.length) return
  shell().confirmAsk(
    t('gui.kb.doc_delete'),
    t('gui.kb.docs_delete_body', { count: rows.length }),
    t('gui.kb.doc_delete'),
    () => {
      const gone = new Set(rows.map((d) => d.id))
      /* Off the list first, and un-picked with it: the rows are the thing the
         reader asked to be rid of, and the reload below is what corrects a
         delete that did not land. */
      set({ docs: state.docs.filter((d) => !gone.has(d.id)), picked: [] })
      void Promise.allSettled(rows.map((d) => source().removeDoc(d.id)))
        .then((results) => {
          const failed = results.filter((r) => r.status === 'rejected').length
          if (failed) toast(t('gui.kb.some_failed', { count: failed }))
        })
        .finally(() => {
          if (state.openId === baseId) void reopen(baseId)
          void load()
        })
    },
  )
}

/* Index a document again, for a row that is not `ready`.

   The same call `upload` makes; what is new is that it can be made a second
   time. `index_document` re-reads the record and re-embeds from the stored
   blob, so a failure that was the endpoint's -- a rate limit, a key that had
   expired -- clears on a retry with nothing else to do. A row left `pending` by
   a gateway restart mid-index is the same shape: nothing else ever picks it up,
   because `index_pending` has no production caller. */
export async function retry(doc: KbDoc): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy) return
  set({ busy: true, docs: state.docs.map((d) => (d.id === doc.id ? { ...d, status: 'indexing', error: '' } : d)) })
  try {
    const indexed = await source().index(doc.id)
    if (state.openId === baseId) {
      set({ docs: state.docs.map((d) => (d.id === indexed.id ? indexed : d)) })
    }
  } catch (e) {
    toast((e as Error)?.message || String(e))
    /* Put the row back the way it was: the optimistic `indexing` above is a
       promise this call just failed to keep. */
    if (state.openId === baseId) {
      set({ docs: state.docs.map((d) => (d.id === doc.id ? doc : d)) })
    }
  } finally {
    set({ busy: false })
    void load()
  }
}

/* Remove one document. Confirmed, because the blob and its chunks go with it
   and an upload is not always still on the reader's disk. */
export function removeDoc(doc: KbDoc): void {
  const baseId = state.openId
  if (!baseId) return
  shell().confirmAsk(
    t('gui.kb.doc_delete'),
    t('gui.kb.doc_delete_body', { name: doc.source }),
    t('gui.kb.doc_delete'),
    () => {
      /* Off the list first: the row is the thing the reader asked to be rid of,
         and the reload below is what corrects a delete that did not land. */
      set({ docs: state.docs.filter((d) => d.id !== doc.id) })
      void source()
        .removeDoc(doc.id)
        .catch((e: unknown) => toast((e as Error)?.message || String(e)))
        .finally(() => {
          if (state.openId === baseId) void reopen(baseId)
          void load()
        })
    },
  )
}

/* Re-read one base's documents without disturbing the panel around them. */
async function reopen(baseId: string): Promise<void> {
  try {
    const docs = await source().documents(baseId)
    /* Ticks that point at rows which are no longer there would keep counting
       towards "N selected" and towards what the two buttons act on. */
    const here = new Set(docs.map((d) => d.id))
    if (state.openId === baseId) set({ docs, picked: state.picked.filter((p) => here.has(p)) })
  } catch {
    /* The row list stays as the optimistic removal left it; the next open
       corrects it. Toasting twice for one failure helps nobody. */
  }
}

/* Every keystroke used to run `knowledge.search`, which embeds the query
   through the configured endpoint -- billed, unthrottled, and on a client whose
   timeout is measured in minutes. Typing a two-word question spent seventeen of
   them. The recall panel asks on a button instead, so a query costs one
   request when the reader says it is finished.

   What the debounce could never fix is still here: the requests race, and the
   guard on `openId` passes for every one of them, so a slow first search
   landing after a second would paint its hits under the newer question. */
let seq = 0

function cancelSearch(): void {
  /* Bumped, not just cleared: a request already in flight has to be disowned
     too, or it repaints a panel the reader has left or emptied. */
  seq += 1
}

/* What a base is configured with when nobody has touched it. The engine holds
   the same numbers; these are what the panel shows for a base answering from a
   gateway too old to report them, and what Restore Defaults puts back. */
export const DEFAULTS: Required<KbSettings> = {
  top_k: 6,
  smart_chunking: true,
  separator: '\n\n',
  chunk_size: 2048,
  chunk_overlap: 215,
  file_processing: '',
}

/* One base's settings, defaulted field by field rather than wholesale: a base
   that carries a top_k and nothing else should keep it. */
export function settingsOf(base: KbBase): Required<KbSettings> {
  return {
    top_k: base.top_k ?? DEFAULTS.top_k,
    smart_chunking: base.smart_chunking ?? DEFAULTS.smart_chunking,
    separator: base.separator ?? DEFAULTS.separator,
    chunk_size: base.chunk_size ?? DEFAULTS.chunk_size,
    chunk_overlap: base.chunk_overlap ?? DEFAULTS.chunk_overlap,
    file_processing: base.file_processing ?? DEFAULTS.file_processing,
  }
}

export function openSettings(): void {
  set({ settings: true })
}

export function closeSettings(): void {
  set({ settings: false })
}

export async function saveSettings(values: KbSettings): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy) return
  set({ busy: true })
  try {
    const saved = await source().settings(baseId, values)
    /* Replaced from the answer rather than from what was sent: the engine is
       what decides, and a field it refused or adjusted has to show as it is. */
    set({ bases: state.bases.map((b) => (b.id === saved.id ? saved : b)), settings: false })
    toast(t('gui.kb.set_saved'))
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
  }
}

export function openRecall(): void {
  set({ recall: true })
}

export function closeRecall(): void {
  cancelSearch()
  set({ recall: false, searching: false })
}

/* Clear what was asked without closing the panel. Null hits rather than an
   empty list: "asked and found nothing" and "not asked yet" are different
   states, and only the first one has a count to report. */
export function clearRecall(): void {
  cancelSearch()
  set({ query: '', hits: null, cost: null, searching: false })
}

export function setQuery(query: string): void {
  set({ query })
}

/* Run one search, for a reader who has said they are finished typing. */
export async function searchNow(query: string): Promise<void> {
  const baseId = state.openId
  const text = query.trim()
  set({ query })
  cancelSearch()
  if (!baseId || !text) {
    set({ hits: null, cost: null })
    return
  }
  const mine = seq
  set({ searching: true })
  try {
    /* The base's own Top K, so the slider in its settings is visibly the thing
       that decides what comes back. Left to the engine when the base has not
       reported one. */
    const base = state.bases.find((b) => b.id === baseId)
    const found = await source().search([baseId], text, base?.top_k)
    if (state.openId !== baseId || mine !== seq) return
    set({ hits: found.hits, cost: { search_ms: found.search_ms, embed_ms: found.embed_ms } })
    remember(text)
  } catch (e) {
    if (state.openId === baseId && mine === seq) toast((e as Error)?.message || String(e))
  } finally {
    if (mine === seq) set({ searching: false })
  }
}

/* ── the queries this browser has asked ────────────────────────────────

   In localStorage and nowhere else: a recall query is a scratch question
   typed to see what the index does with it, and the answer to "have I tried
   this one" is only useful to the person who typed it. Nothing about it needs
   to reach the gateway, another device, or anybody else's browser. */
const HISTORY_KEY = 'raven.gui.kbq'
const HISTORY_MAX = 8

export function history(): string[] {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(HISTORY_KEY) || 'null')
    /* Every element checked, not just the array: this is storage anything on
       the origin can write, and one number in it would render as a blank row
       and throw on `.trim()`. */
    return Array.isArray(raw) ? raw.filter((q): q is string => typeof q === 'string' && !!q.trim()) : []
  } catch {
    /* Unreadable in private mode, cleared, or holding something else. An
       absent history is the same to this panel as an empty one. */
    return []
  }
}

/* Newest first, no duplicates, oldest dropped past the cap. Recorded only once
   a search has answered: a query that failed is not one the reader would want
   offered back to them as something they have asked. */
function remember(query: string): void {
  const kept = [query, ...history().filter((q) => q !== query)].slice(0, HISTORY_MAX)
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(kept))
  } catch {
    /* Storage may be unavailable in private mode or over quota. The panel
       works without it; only the list of past questions is lost. */
  }
}

export function forgetHistory(): void {
  try {
    localStorage.removeItem(HISTORY_KEY)
  } catch {
    /* Nothing to do: it was already unreachable. */
  }
}

export function open(): void {
  shell().showPage('kbPage')
  void load()
}

export function close(): void {
  shell().showPage(null)
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}

export function _resetForTests(): void {
  state = EMPTY
  listeners.clear()
  /* The timer and the token too: a test that types without waiting out the
     debounce would otherwise fire into the next test's source. */
  cancelSearch()
}

/* ── the original file behind a row ───────────────────────────────────
   The gateway serves it by document id, never by path: the blobs live under
   raven's state directory, which the viewer's own path policy refuses, and an
   id means nothing the page sends names a location. */

/* Formats no browser draws, which the gateway converts to PDF with
   LibreOffice. Keyed on the upload's own name because that is what the record
   kept -- the stored copy has no suffix at all. */
const CONVERTED = new Set(['doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'odt', 'odp', 'ods', 'rtf'])
/* What a frame can draw as it stands. Everything else is offered as a
   download rather than shown as a wall of bytes. */
const NATIVE = new Set([
  'pdf', 'txt', 'md', 'markdown', 'mdx', 'csv', 'tsv', 'json', 'xml', 'yaml', 'yml',
  'html', 'htm', 'log', 'rst', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif',
])

function suffixOf(doc: KbDoc): string {
  const name = doc.source || ''
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/* Markdown is its own kind because framing it shows the source. The gateway
   serves .md as text/plain -- correctly, it is text -- and a frame then draws
   the hashes and the pipes, which is the file rather than the document. */
const MARKDOWN = new Set(['md', 'markdown', 'mdx'])

/* Which family a file belongs to, for the glyph beside its name.

   By family and not by extension, the way ragflow's own map folds xls, xlsx
   and csv onto one sheet icon: a reader scanning a list is asking "is this a
   document, a spreadsheet, a picture", and forty glyphs answer that no better
   than eight while costing a page that inlines every byte of itself. */
export function fileFamily(doc: KbDoc): string {
  /* Before the extension, because both are stored as markdown and a column of
     identical .md glyphs would hide the one thing that separates these rows
     from each other: where they came from. */
  if (doc.origin === 'note') return 'note'
  if (doc.origin === 'url') return 'link'
  const ext = suffixOf(doc)
  if (ext === 'pdf') return 'pdf'
  if (ext === 'txt') return 'txt'
  if (MARKDOWN.has(ext)) return 'md'
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return 'doc'
  if (['xls', 'xlsx', 'ods', 'csv', 'tsv'].includes(ext)) return 'sheet'
  if (['ppt', 'pptx', 'odp'].includes(ext)) return 'slide'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif'].includes(ext)) return 'image'
  if (['json', 'xml', 'yaml', 'yml', 'html', 'htm'].includes(ext)) return 'data'
  return 'file'
}

/* What a lettered glyph is badged with: the file's own extension, upper-cased
   and clipped to what fits a badge. Its own extension and not its family's, so
   a .doc is not badged DOCX and an .odt is not badged DOC.

   A document with no extension at all still gets a badge, because its family
   put it in the lettered set: `FILE` is what a desktop shows for one. */
export function formatLabel(doc: KbDoc): string {
  const ext = suffixOf(doc).toUpperCase()
  return ext.slice(0, 4) || 'FILE'
}

/* What the Type column says. The origin when there is one to name, the file's
   own family otherwise -- a reader scanning the column wants "is this mine or
   did it come off the web", and every row saying "Files" answered nothing. */
export function docType(doc: KbDoc): string {
  if (doc.origin === 'note') return t('gui.kb.doc_type_note')
  if (doc.origin === 'url') return t('gui.kb.doc_type_url')
  return t('gui.kb.doc_type_file')
}

export function previewKind(doc: KbDoc): 'markdown' | 'native' | 'converted' | 'none' {
  const ext = suffixOf(doc)
  if (MARKDOWN.has(ext)) return 'markdown'
  if (CONVERTED.has(ext)) return 'converted'
  return NATIVE.has(ext) ? 'native' : 'none'
}

/* The text behind a document, for the kinds the page renders itself rather
   than frames. Read through the same route the frame uses, so there is one
   answer to "where do these bytes come from". */
export async function readText(doc: KbDoc): Promise<string> {
  const res = await fetch(previewUrl(doc))
  if (!res.ok) throw new Error((await res.text()).trim() || `${res.status} ${res.statusText}`)
  return res.text()
}

export function previewUrl(doc: KbDoc): string {
  const base = `/knowledge/file?document=${encodeURIComponent(doc.id)}`
  return previewKind(doc) === 'converted' ? `${base}&render=pdf` : base
}

export function openDoc(doc: KbDoc): void {
  set({ viewing: doc })
}

export function closeDoc(): void {
  set({ viewing: null })
}
