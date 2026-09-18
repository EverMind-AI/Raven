import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type {
  KbBase,
  KbChunk,
  KbChunkQuery,
  KbDoc,
  KbHit,
  KbProvider,
  KbSettings,
  KbStatus,
  KnowledgeSource,
} from './types'

/* What an RPC failure actually said.
 *
 * The transport rejects with the JSON-RPC error frame itself, not an `Error`,
 * so `.message` is the code's name -- `internal_error` -- and the sentence a
 * reader needs is in `data.detail`. Reading only `.message` turned "your
 * account balance is insufficient" from the embedding endpoint into the word
 * "internal_error" on a toast, which is a dead end for whoever has to fix it.
 * Every other live layer reads the detail first; this one did not. */
const said = (e: unknown): string => {
  const frame = e as { message?: string; data?: { detail?: string; reason?: string } } | null
  const detail = frame?.data?.detail || frame?.data?.reason
  return detail || frame?.message || String(e)
}

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
  /* The open document's indexed pieces, beside its original. Null while they
     are still being read, which is a different thing from the empty list a
     document with nothing indexed answers with -- one is "wait", the other is
     "there is nothing here", and a panel that showed the same for both would
     be lying half the time. */
  chunks: KbChunk[] | null
  /* Why the pieces could not be read. The panel says so rather than showing an
     empty list, which a reader would take for a document that indexed to
     nothing. */
  chunksFailed: string | null
  /* How many pieces the current filter admits, which is what the pager counts
     -- not how many are on screen. */
  chunksTotal: number
  /* Which page of the reading order is shown, 1-based. */
  chunkPage: number
  /* The query the list is answering. Non-empty replaces the page with what
     matched, best first, so the pager stands down: relevance has no second
     page the engine was asked for. */
  chunkQuery: string
  /* What is in the search box, which is not the same thing. A search is a
     round trip to the embedding endpoint, so it waits for the typing to stop
     (or for Enter) -- and every other reload in between has to keep using the
     query that is actually in effect, not the half-typed one. */
  chunkTyped: string
  /* Which state the list is filtered to, or null for both. */
  chunkFilter: boolean | null
  /* Whether a chunk is shown whole or cut to a few lines. A reading preference,
     so it outlives the document being closed. */
  chunkView: 'full' | 'ellipse'
  /* Which pieces are ticked, by id. Cleared whenever the list under them
     changes, because a tick is a claim about rows that are on screen. */
  chunkPicked: string[]
  /* A write to the chunks is in flight. Its own flag rather than `busy`: that
     one gates the document list, and these two panels are used at once. */
  chunkBusy: boolean
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
  kind: 'note' | 'url' | 'rename' | 'chunk'
  doc?: KbDoc
  /* The piece being rewritten. Absent means a new one is being written. */
  chunk?: KbChunk
  /* The base being renamed. Only `rename` carries one. */
  base?: KbBase
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
  chunks: null,
  chunksFailed: null,
  chunksTotal: 0,
  chunkPage: 1,
  chunkQuery: '',
  chunkTyped: '',
  chunkFilter: null,
  chunkView: 'ellipse',
  chunkPicked: [],
  chunkBusy: false,
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
    set({ loaded: true, failed: said(e) })
    return
  }
  try {
    /* Both together: the page cannot say "no bases yet, create one" without
       knowing whether creating one is even possible. */
    const [status, bases] = await Promise.all([src.status(), src.bases()])
    set({ status, bases, loaded: true, failed: null })
  } catch (e) {
    set({ loaded: true, failed: said(e) })
  }
}

export async function create(
  name: string,
  description = '',
  embedding = true,
  model = '',
  provider = '',
): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed || state.busy) return
  set({ busy: true })
  try {
    await source().create(trimmed, description, embedding, model, provider)
    await load()
  } catch (e) {
    /* Creating measures the model's width against the endpoint, so it reaches
       the network and can fail. Said out loud: a base that is not there is not
       something to discover later. */
    toast(said(e))
  } finally {
    set({ busy: false })
  }
}

/* Rename one base. The only field of it a reader can edit: the embedding model
   and its width are what the collection was built to, so those are a rebuild
   rather than an edit, and the document count is a fact rather than a setting.

   The engine refuses a name another base already holds -- the same rule
   creation applies, or renaming would be the way around it -- so the answer is
   reported rather than assumed. */
export async function renameBase(base: KbBase, name: string): Promise<void> {
  const wanted = name.trim()
  if (state.busy || !wanted || wanted === base.name) {
    set({ dialog: null })
    return
  }
  set({ busy: true })
  try {
    const renamed = await source().rename(base.id, wanted)
    set({ bases: state.bases.map((b) => (b.id === renamed.id ? renamed : b)), dialog: null })
  } catch (e) {
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
        .catch((e: unknown) => toast(said(e)))
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
    if (state.openId === id) toast(said(e))
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

/* The add dialog with no chunk, the edit dialog with one. One dialog either
   way: what a reader does in it is the same, and two would drift. */
export function openChunkDialog(chunk?: KbChunk): void {
  set({ dialog: { kind: 'chunk', chunk } })
}

export function openRename(base: KbBase): void {
  set({ dialog: { kind: 'rename', base } })
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
    toast(said(e))
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
    toast(said(e))
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
    toast(said(e))
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
        .catch((e: unknown) => toast(said(e)))
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
  /* Off. Overlap repeats text between neighbouring chunks, and every repeated
     passage is retrieved twice and reads as two findings. */
  chunk_overlap: 0,
  /* About a sentence either side of a table or a figure. What the engine
     defaults to, so the panel and Restore Defaults say the same thing. */
  table_context_size: 64,
  image_context_size: 64,
  file_processing: '',
  /* Empty means the configured endpoint, which is where a base built today
     gets its model. */
  embedding_provider: '',
  /* Never restored to a default: the base's own model is what its vectors were
     made by, and Restore Defaults is about chunking, not about dropping an
     index. Present because the type is exhaustive. */
  embedding_model: '',
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
    table_context_size: base.table_context_size ?? DEFAULTS.table_context_size,
    image_context_size: base.image_context_size ?? DEFAULTS.image_context_size,
    file_processing: base.file_processing ?? DEFAULTS.file_processing,
    embedding_provider: base.embedding_provider ?? DEFAULTS.embedding_provider,
    embedding_model: base.embedding_model,
  }
}

/* Every embedding model this install can reach, grouped by provider: what the
   picker offers, both for a base being created and for one being moved onto
   another model.

   Read once, when a dialog carrying the picker opens, and kept: the list
   changes when somebody edits their providers, which is not something that
   happens while a dialog is up. An install whose providers cannot be read
   offers a picker holding only what the base already has rather than failing
   the dialog -- the rest of it is still usable. */
let models: KbProvider[] = []

export function embeddingChoices(): KbProvider[] {
  return models
}

/* Fetched on the way into a dialog that shows the picker, rather than with the
   page: it is a round trip per open at worst and none at all for a reader who
   never opens one. */
export function loadEmbeddingModels(): void {
  if (models.length) return
  void (async () => {
    try {
      models = await source().embeddingModels()
      set({})
    } catch {
      /* The picker falls back to what the base already names; nothing else on
         the panel depends on it. Inside the try rather than on a `.catch`
         because a source too old to have the call at all throws here rather
         than rejecting, and that would take the dialog down with it. */
    }
  })()
}

export function openSettings(): void {
  set({ settings: true })
  loadEmbeddingModels()
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
    /* A model change rebuilds the base: every row is back in the queue with no
       chunks, and a file list still saying `ready` beside a count that is now
       zero describes the base as it was a second ago. */
    if (values.embedding_model !== undefined) await reopen(baseId)
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
  /* The model list too, for the same reason as the state: it is cached for the
     life of the page, so a test would otherwise pick a picker whose options
     came from the source the test before it installed. */
  models = []
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
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif', 'tif', 'tiff'].includes(ext)) return 'image'
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

/* How many pieces a page holds. Twenty is what a reader scans without the page
   becoming a scroll of its own; the engine caps what it will answer with. */
export const CHUNK_PAGE = 20

export function openDoc(doc: KbDoc): void {
  cancelChunkSearch()
  /* A fresh opening starts at the top with nothing ticked and no query. The
     view mode is deliberately left alone: it is how this reader likes to read,
     not a fact about the file they just opened. */
  set({
    viewing: doc,
    chunks: null,
    chunksFailed: null,
    chunksTotal: 0,
    chunkPage: 1,
    chunkQuery: '',
    chunkTyped: '',
    chunkFilter: null,
    chunkPicked: [],
  })
  void loadChunks(doc)
}

export function closeDoc(): void {
  cancelChunkSearch()
  set({ viewing: null, chunks: null, chunksFailed: null, chunksTotal: 0, chunkPicked: [] })
}

/* Read the page the current controls describe.

   One door for every control -- page, query, filter -- because they compose:
   searching while filtered has to ask for both, and a second path would be the
   one that forgets. */
function chunkRequest(): KbChunkQuery {
  const query = state.chunkQuery.trim()
  if (query) return { query, page_size: CHUNK_PAGE }
  return {
    page: state.chunkPage,
    page_size: CHUNK_PAGE,
    ...(state.chunkFilter === null ? {} : { available: state.chunkFilter }),
  }
}

export function chunkPages(): number {
  return Math.max(1, Math.ceil(state.chunksTotal / CHUNK_PAGE))
}

export function showChunkPage(page: number): void {
  const doc = state.viewing
  if (!doc) return
  set({ chunkPage: Math.min(Math.max(1, page), chunkPages()), chunkPicked: [] })
  void loadChunks(doc)
}

export function filterChunks(available: boolean | null): void {
  const doc = state.viewing
  if (!doc) return
  set({ chunkFilter: available, chunkPage: 1, chunkPicked: [] })
  void loadChunks(doc)
}

/* How long the typing has to stop before a search goes out.

   Three seconds, which is long for a keystroke debounce and right for this
   one: the request embeds the query at whatever endpoint the base was built
   with, so an eager search is a round trip per keystroke against somebody's
   rate limit. Enter skips the wait for anyone who does not want to serve it. */
const CHUNK_SEARCH_WAIT = 3000
let chunkTimer: ReturnType<typeof setTimeout> | null = null

function cancelChunkSearch(): void {
  if (chunkTimer !== null) {
    clearTimeout(chunkTimer)
    chunkTimer = null
  }
}

export function typeChunkSearch(query: string): void {
  set({ chunkTyped: query })
  cancelChunkSearch()
  /* An emptied box goes back at once. Waiting three seconds to see the list
     again after clearing the search reads as a page that has stopped
     answering, and there is no request to spare by waiting. */
  if (!query.trim()) {
    if (state.chunkQuery) searchChunks('')
    return
  }
  chunkTimer = setTimeout(() => {
    chunkTimer = null
    searchChunks(state.chunkTyped)
  }, CHUNK_SEARCH_WAIT)
}

/* Search now -- what Enter does, and what the timer above ends up calling. */
export function searchChunks(query: string): void {
  const doc = state.viewing
  if (!doc) return
  cancelChunkSearch()
  if (query === state.chunkQuery) return
  set({ chunkQuery: query, chunkTyped: query, chunkPage: 1, chunkPicked: [] })
  void loadChunks(doc)
}

export function setChunkView(view: 'full' | 'ellipse'): void {
  set({ chunkView: view })
}

export function pickChunk(chunkId: string, on: boolean): void {
  const picked = new Set(state.chunkPicked)
  if (on) picked.add(chunkId)
  else picked.delete(chunkId)
  set({ chunkPicked: [...picked] })
}

export function pickAllChunks(on: boolean): void {
  /* Only what is on screen, and only what can be acted on: a piece with no id
     was written before ids existed and cannot be addressed one at a time. */
  const ids = (state.chunks || []).map((c) => c.chunk_id || '').filter(Boolean)
  set({ chunkPicked: on ? ids : [] })
}

/* The open document's pieces, read once per opening.

   Guarded on the document still being the open one: a reader clicking down a
   list faster than the engine answers would otherwise see the pieces of a file
   they have already moved on from, under the name of the one they are looking
   at. */
async function loadChunks(doc: KbDoc): Promise<void> {
  const asked = chunkRequest()
  try {
    const page = await source().chunks(doc.id, asked)
    if (state.viewing?.id !== doc.id) return
    /* A page of the reading order is sorted here, where the panel's claim is
       made, rather than trusted from the wire: the engine orders its answer,
       but "reading order" is what this list says it shows, and a guarantee is
       worth holding at the place that states it. A search is not sorted -- its
       order is the ranking, and renumbering it by position in the document
       would throw away the only thing the query bought. */
    const rows = asked.query ? page.chunks : [...page.chunks].sort((a, b) => a.chunk_index - b.chunk_index)
    set({ chunks: rows, chunksTotal: page.total, chunksFailed: null })
  } catch (e) {
    if (state.viewing?.id !== doc.id) return
    set({ chunks: [], chunksTotal: 0, chunksFailed: said(e) })
  }
}

/* Reload the open document's pieces, after a write changed them. */
function refreshChunks(): void {
  const doc = state.viewing
  if (doc) void loadChunks(doc)
}

export async function switchChunks(chunkIds: string[], enabled: boolean): Promise<void> {
  const doc = state.viewing
  if (!doc || !chunkIds.length) return
  const wanted = new Set(chunkIds)
  /* Shown before the engine answers, and put back if it refuses: a toggle that
     waits for a round trip reads as a toggle that did not work. */
  const before = state.chunks
  set({
    chunkBusy: true,
    chunks: (before || []).map((c) => (wanted.has(c.chunk_id || '') ? { ...c, enabled } : c)),
  })
  try {
    await source().switchChunks(doc.id, chunkIds, enabled)
    refreshChunks()
  } catch (e) {
    set({ chunks: before })
    toast(said(e))
  } finally {
    set({ chunkBusy: false })
  }
}

export function deleteChunks(chunkIds: string[]): void {
  const doc = state.viewing
  if (!doc || !chunkIds.length) return
  shell().confirmAsk(
    t('gui.kb.chunk_delete'),
    t('gui.kb.chunk_delete_body', { count: chunkIds.length }),
    t('gui.kb.chunk_delete'),
    () => {
      set({ chunkBusy: true })
      void source()
        .deleteChunks(doc.id, chunkIds)
        .then(() => {
          set({ chunkPicked: [] })
          /* Back a page when the last one emptied out, so deleting the tail of
             a document does not leave the pager pointing past the end. */
          const left = Math.max(1, Math.ceil(Math.max(0, state.chunksTotal - chunkIds.length) / CHUNK_PAGE))
          if (state.chunkPage > left) set({ chunkPage: left })
          refreshChunks()
          if (state.openId) void reopen(state.openId)
        })
        .catch((e) => toast(said(e)))
        .finally(() => set({ chunkBusy: false }))
    },
  )
}

export async function saveChunk(chunk: KbChunk, text: string): Promise<void> {
  const doc = state.viewing
  const id = chunk.chunk_id
  if (!doc || !id || !text.trim()) return
  set({ chunkBusy: true })
  try {
    await source().updateChunk(doc.id, id, text)
    closeDialog()
    /* The id changed with the text, so a tick pointing at the old one is
       pointing at nothing. */
    set({ chunkPicked: state.chunkPicked.filter((p) => p !== id) })
    refreshChunks()
  } catch (e) {
    toast(said(e))
  } finally {
    set({ chunkBusy: false })
  }
}

export async function addChunk(text: string): Promise<void> {
  const doc = state.viewing
  if (!doc || !text.trim()) return
  set({ chunkBusy: true })
  try {
    await source().createChunk(doc.id, text)
    closeDialog()
    /* At the end of the reading order, so that is the page to be looking at. */
    set({ chunkQuery: '', chunkFilter: null, chunkPicked: [] })
    const total = state.chunksTotal + 1
    set({ chunksTotal: total, chunkPage: Math.max(1, Math.ceil(total / CHUNK_PAGE)) })
    refreshChunks()
    if (state.openId) void reopen(state.openId)
  } catch (e) {
    toast(said(e))
  } finally {
    set({ chunkBusy: false })
  }
}
