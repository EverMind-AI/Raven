/* The knowledge page's state: the bases, which tab is showing, and what is
 * open on top of them.
 *
 * One store for the page rather than one per card. A card's marks are written
 * server-side and the answer replaces the row here, so every card reads the
 * same list and none of them holds a copy that can drift from the registry.
 */

import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'
/* The one place the page size is written down. This module needs it only to
   work out how many pages there are, and two copies of a number that has to
   agree is a number that will not. */
export { CHUNK_PAGE } from './source'
import { CHUNK_PAGE } from './source'

import type {
  KbBase,
  KbChunk,
  KbDoc,
  KbFolder,
  KbModel,
  KbPage,
  KbSettings,
  KnowledgeSource,
} from './types'

/** Which pieces the list is filtered to. */
export type ChunkFilter = 'all' | 'on' | 'off'

/** Which of the three tabs is showing. */
export type Tab = 'all' | 'starred' | 'recent'

/** How a base's documents are drawn. */
export type View = 'grid' | 'list'

export interface KnowledgeState {
  /** Null while the first read is in flight, which is not the empty list a
   *  machine with no bases answers with. */
  bases: KbBase[] | null
  /** Why the list could not be read, as the server's own sentence. */
  failed: string | null
  /** Every embedding model the install can reach, for a base whose model can
   *  still be moved. Null until the settings have been opened once: it is a
   *  read of every provider's catalogue, and a reader who never opens the
   *  panel should not pay for it. */
  models: KbModel[] | null
  tab: Tab
  /** A write is in flight; the page's controls are disabled meanwhile. */
  busy: boolean
  /** The base whose documents are showing, or null for the grid of cards.
   *  Held as the row rather than an id: the breadcrumb needs its name, and a
   *  base deleted while it is open should not leave the path looking up an id
   *  that is gone. */
  opened: KbBase | null
  /** That base's documents. Null while the first read is in flight. */
  documents: KbDoc[] | null
  /** Rows or cards. Rows to begin with: a document is read by its name, its
   *  size and when it was touched, and those are columns -- a grid of cards
   *  shows fewer files in more space and puts the name of each in a different
   *  place. A reader's choice about how they read, so it holds across bases
   *  rather than resetting every time one is opened. */
  view: View
  /** The open base's folders. Root is not among them. */
  folders: KbFolder[]
  /** Which folder the list is showing, or '' for Root. */
  folder: string
  /** Whether the folder panel is showing. The `<<` control folds it away for
   *  a reader who files nothing and does not want the column. */
  tree: boolean
  /** The document whose folder is being picked, or null. */
  moving: KbDoc | null
  /** The document being read, over the whole page. Held as the row rather
   *  than an id: the viewer reads its media type to decide what it is
   *  showing, and a row deleted while it is open should not leave the panel
   *  looking up an id that is gone. */
  viewing: KbDoc | null
  /** Its indexed pieces. Null while they are being read, which is not the
   *  empty list a document with nothing indexed answers with: one is "wait",
   *  the other is "there is nothing here", and a panel that showed the same
   *  for both would be lying half the time. */
  chunks: KbChunk[] | null
  /** Why they could not be read. Said out loud rather than shown as an empty
   *  list, which a reader would take for a document that indexed to nothing. */
  chunksFailed: string | null
  /** The file's own pages, for the half of the panel that draws it. Null while
   *  they are being read; empty for a format that has none, which is the
   *  panel's signal to frame the file instead. */
  pages: KbPage[] | null
  /** How many pieces the filter admits, which is what the pager counts --
   *  not how many are on screen. */
  chunkTotal: number
  /** Which page of them is showing, 1-based. */
  chunkPage: number
  /** What the search box holds. Empty means the reading order rather than an
   *  answer to a query. */
  chunkQuery: string
  chunkFilter: ChunkFilter
  /** Whether a piece shows all of its text or the first few lines. A reader
   *  scanning for one piece wants the short form; a reader checking where a
   *  cut landed wants the whole thing. */
  chunkFull: boolean
  /** The pieces ticked for a batch, by chunk id. Emptied whenever the list
   *  underneath them is re-read: a tick on a row that is no longer on screen
   *  is a batch nobody can see the size of. */
  picked: string[]
  /** The piece being rewritten, by chunk id, or '' for none. */
  editing: string
  /** The piece the preview is showing, by chunk id. Its row is marked, so a
   *  reader who has scrolled the list can see which one the page beside them
   *  belongs to. */
  aimed: string
  /** Where in the file that piece sits, for the viewer to open at. Null for a
   *  format with no pages, which cannot be scrolled to a position.
   *
   *  `nth` counts the presses rather than saying anything about the place: the
   *  browser's PDF viewer reads the page and position once, when it loads, so
   *  the frame is loaded again to move it, and a reader who has scrolled away
   *  and pressed the same piece again means to be taken back. Two presses on
   *  one piece are two different asks with the same answer. */
  aim: { page: number; top: number; nth: number } | null
  /** Whether the blank piece at the top of the list is open. */
  drafting: boolean
  /** The sheet on top of the page, if any. */
  sheet:
    | { kind: 'create' }
    | { kind: 'settings'; base: KbBase }
    | { kind: 'url' }
    | { kind: 'note' }
    | null
}

const store = makeStore<KnowledgeState>({
  bases: null,
  failed: null,
  models: null,
  tab: 'all',
  busy: false,
  opened: null,
  documents: null,
  view: 'list',
  folders: [],
  folder: '',
  tree: true,
  moving: null,
  viewing: null,
  chunks: null,
  chunksFailed: null,
  pages: null,
  chunkTotal: 0,
  chunkPage: 1,
  chunkQuery: '',
  chunkFilter: 'all',
  chunkFull: false,
  picked: [],
  editing: '',
  aimed: '',
  aim: null,
  drafting: false,
  sheet: null,
})

/* The whole shape, as scripts/gates/store-shape.test.mjs holds every
   subscribable module to: a reader gets `get`/`subscribe`, and a test gets the
   reset that keeps one case's bases out of the next. */
export const { get, set, subscribe, _resetForTests } = store

/* Every writer here changes a field or two, so they merge rather than
   replace; `set` itself stays exported whole, which is the shape
   scripts/gates/store-shape.test.mjs holds every store to. */
const patch = (next: Partial<KnowledgeState>): void => set((prev) => ({ ...prev, ...next }))

const source = (): KnowledgeSource => ds('knowledge')

/* The server's sentence where there is one. A thrown Error carries the
   gateway's message; anything else is stringified rather than swallowed, so a
   failure never reads as an empty list. */
const reasonOf = (e: unknown): string => (e as { message?: string })?.message || String(e)

export function open(): void {
  page.show('knowledgePage')
  void load()
}

export function close(): void {
  page.show(null)
}

export async function load(): Promise<void> {
  try {
    patch({ bases: await source().bases(), failed: null })
  } catch (e) {
    patch({ bases: [], failed: reasonOf(e) })
  }
}

/* Into one base, and back out to the grid.

   The documents are read on the way in rather than held with the base: a base
   row says how many there are, and which they are is a second question only
   asked when a reader asks it. */
export function openBase(base: KbBase): void {
  /* Back to Root on the way in: a folder chosen inside one base means nothing
     in the next, and a list filtered by a folder that is not there reads as a
     base with no documents. */
  patch({ opened: base, documents: null, folders: [], folder: '' })
  void loadDocuments(base.id)
  void loadFolders(base.id)
}

export function closeBase(): void {
  patch({ opened: null, documents: null, folders: [], folder: '', viewing: null, chunks: null, chunksFailed: null })
}

/* One document, over the whole page rather than beside the list.

   The widest thing on the screen should be the thing being read, and picking
   another folder is not a move anyone makes mid-document. */
export function openDoc(doc: KbDoc): void {
  /* Every filter back to its default on the way in: a query typed against one
     document says nothing about the next, and a list filtered by a search a
     reader cannot see reads as a document with three pieces in it. */
  patch({
    viewing: doc,
    chunks: null,
    chunksFailed: null,
    chunkTotal: 0,
    chunkPage: 1,
    chunkQuery: '',
    chunkFilter: 'all',
    picked: [],
    editing: '',
    aimed: '',
    aim: null,
    drafting: false,
    pages: null,
  })
  void loadChunks(doc)
  void loadPages(doc)
}

export function closeDoc(): void {
  patch({
    viewing: null,
    chunks: null,
    chunksFailed: null,
    pages: null,
    picked: [],
    editing: '',
    aimed: '',
    aim: null,
    drafting: false,
  })
}

/* The open document's pieces, read once per opening.

   Guarded on it still being the open one: a reader clicking down a list faster
   than the engine answers would otherwise see the pieces of a file they have
   already moved on from, under the name of the one they are looking at. */
async function loadChunks(doc: KbDoc): Promise<void> {
  const s = get()
  const query = s.chunkQuery.trim()
  try {
    const found = await source().chunks(doc.id, {
      page: s.chunkPage,
      available: s.chunkFilter === 'all' ? null : s.chunkFilter === 'on',
      query,
    })
    if (get().viewing?.id !== doc.id) return
    /* Sorted here, where the panel's claim is made, rather than trusted from
       the wire: the engine orders its answer, but "reading order" is what the
       list says it shows, and a guarantee is worth holding at the place that
       states it. Copied rather than sorted in place -- the array is the
       source's, and sorting it would reach back through the seam.

       Not for a query: what a search answers is ordered by how well each
       piece answered it, and renumbering that into reading order would throw
       away the only thing the ranking said. */
    const rows = query ? found.chunks : [...found.chunks].sort((a, b) => a.chunk_index - b.chunk_index)
    patch({ chunks: rows, chunkTotal: found.total, chunksFailed: null, picked: [] })
  } catch (e) {
    if (get().viewing?.id === doc.id) patch({ chunks: [], chunkTotal: 0, chunksFailed: reasonOf(e) })
  }
}

/* The file's pages, read once per opening.

   Guarded on the document still being the open one, for the reason the chunks
   are: a reader clicking down a list faster than the engine answers would
   otherwise see one file's pages under another file's name.

   A failure is the empty list rather than a message. There is nothing here a
   reader can act on -- the file is still shown, framed instead of drawn -- and
   a panel that said so would be reporting the absence of a picture as a fault. */
async function loadPages(doc: KbDoc): Promise<void> {
  try {
    const found = await source().pages(doc.id)
    if (get().viewing?.id === doc.id) patch({ pages: found })
  } catch {
    if (get().viewing?.id === doc.id) patch({ pages: [] })
  }
}

/** Where the gateway draws one page of a document. */
export function pageUrl(documentId: string, page: number): string {
  return `/knowledge/page?document=${encodeURIComponent(documentId)}&page=${page}`
}

/** Read the list again as it now stands, for a write that changed it. */
async function reloadChunks(): Promise<void> {
  const doc = get().viewing
  if (doc) await loadChunks(doc)
}

/* The toolbar's four controls. Each puts the list back to its first page: a
   reader on page three of everything who then asks for the disabled ones is
   not asking for page three of those. */
export function setChunkQuery(query: string): void {
  patch({ chunkQuery: query, chunkPage: 1, chunks: null })
  void reloadChunks()
}

export function setChunkFilter(filter: ChunkFilter): void {
  patch({ chunkFilter: filter, chunkPage: 1, chunks: null })
  void reloadChunks()
}

export function setChunkFull(full: boolean): void {
  patch({ chunkFull: full })
}

export function setChunkPage(page: number): void {
  const last = Math.max(1, Math.ceil(get().chunkTotal / CHUNK_PAGE))
  const want = Math.min(Math.max(1, page), last)
  if (want === get().chunkPage) return
  patch({ chunkPage: want, chunks: null })
  void reloadChunks()
}

/* Which pieces a batch acts on.

   A piece written before chunk ids existed has nothing to address it, so it
   can be read but not acted on -- it is never ticked, and the row's own
   controls are off. */
export function pick(chunk: KbChunk): void {
  if (!chunk.chunk_id) return
  const has = get().picked.includes(chunk.chunk_id)
  patch({ picked: has ? get().picked.filter((id) => id !== chunk.chunk_id) : [...get().picked, chunk.chunk_id] })
}

export function pickAll(on: boolean): void {
  patch({ picked: on ? (get().chunks ?? []).map((row) => row.chunk_id).filter(Boolean) : [] })
}

/* Turn pieces on or off. A disabled piece is never searched and never reaches
   the agent; it stays in the list, dimmed, because it is still part of the
   document and a gap in the numbering would read as a fault. */
export async function switchChunks(ids: string[], enabled: boolean): Promise<void> {
  const doc = get().viewing
  if (!doc || !ids.length || get().busy) return
  patch({ busy: true })
  try {
    await source().switchChunks(doc.id, ids, enabled)
    /* Patched rather than re-read, except under a filter that the change
       moves them out of -- then the list they belong in is a different list. */
    if (get().chunkFilter === 'all') {
      patch({
        chunks: (get().chunks ?? []).map((row) => (ids.includes(row.chunk_id) ? { ...row, enabled } : row)),
        picked: [],
      })
    } else {
      await reloadChunks()
    }
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* Take pieces out of the index. The whole list is re-read afterwards: what
   the numbering and the total look like once pieces are gone is the store's
   answer, not arithmetic worth doing here. */
export async function deleteChunks(ids: string[]): Promise<void> {
  const doc = get().viewing
  if (!doc || !ids.length || get().busy) return
  patch({ busy: true })
  try {
    await source().deleteChunks(doc.id, ids)
    await reloadChunks()
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* Point the preview at one piece.

   The first of its regions, not all of them: a piece that merged several can
   cross a page boundary, and a viewer opens at one place. The first is where
   reading it starts, which is where a reader wants to land.

   A piece with no regions still marks its row -- it is the one being read --
   but leaves the preview where it was rather than scrolling somewhere it
   cannot justify. */
export function aimAt(chunk: KbChunk): void {
  const at = chunk.regions[0]
  const nth = (get().aim?.nth ?? 0) + 1
  patch({
    aimed: chunk.chunk_id || String(chunk.chunk_index),
    aim: at ? { page: at.page_number, top: at.top, nth } : null,
  })
}

export function draft(on: boolean): void {
  patch({ drafting: on, editing: '' })
}

export function edit(chunkId: string): void {
  patch({ editing: chunkId, drafting: false })
}

/* A piece a person wrote, embedded like every other piece.

   It is appended rather than inserted: where in the reading order a written
   piece belongs is a question the document cannot answer, and putting it at
   the end says plainly that a person added it. It goes with every other piece
   when the document is reindexed. */
export async function createChunk(text: string): Promise<void> {
  const doc = get().viewing
  const written = text.trim()
  if (!doc || !written || get().busy) return
  patch({ busy: true })
  try {
    await source().createChunk(doc.id, written)
    patch({ drafting: false })
    await reloadChunks()
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* Rewrite one piece, which re-embeds it: the text is what the search matches
   against, so a piece whose words changed and whose vector did not would be
   found by the old wording and read as the new. */
export async function updateChunk(chunk: KbChunk, text: string): Promise<void> {
  const doc = get().viewing
  const written = text.trim()
  if (!doc || !chunk.chunk_id || !written || get().busy) return
  patch({ busy: true })
  try {
    const row = await source().updateChunk(doc.id, chunk.chunk_id, written)
    patch({
      chunks: (get().chunks ?? []).map((old) => (old.chunk_id === chunk.chunk_id ? row : old)),
      editing: '',
    })
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* Where the gateway serves the picture of one piece: the region of the page
   it was cut from, stored at index time. By document and chunk, the same way
   the file route takes an id and nothing else. */
export function cropUrl(documentId: string, chunkId: string): string {
  return `/knowledge/crop?document=${encodeURIComponent(documentId)}&chunk=${encodeURIComponent(chunkId)}`
}

export async function loadFolders(baseId: string): Promise<void> {
  try {
    const rows = await source().folders(baseId)
    if (get().opened?.id === baseId) patch({ folders: rows })
  } catch {
    /* A base with no folder list is a base showing Root, which is what every
       base looked like before folders existed. Not worth a failed page. */
    if (get().opened?.id === baseId) patch({ folders: [] })
  }
}

export function setFolder(folder: string): void {
  patch({ folder })
}

export function toggleTree(): void {
  patch({ tree: !get().tree })
}

export function openMove(doc: KbDoc | null): void {
  patch({ moving: doc })
}

export async function createFolder(name: string): Promise<void> {
  const base = get().opened
  const named = name.trim()
  if (!base || !named || get().busy) return
  patch({ busy: true })
  try {
    const made = await source().createFolder(base.id, named)
    patch({ folders: [...get().folders, made] })
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

export async function removeFolder(folder: KbFolder): Promise<void> {
  const base = get().opened
  if (!base || get().busy) return
  patch({ busy: true })
  try {
    await source().removeFolder(folder.id)
    /* Its documents came back to Root, so both lists are re-read rather than
       patched: which documents moved is the server's answer, not a guess. */
    patch({ folder: get().folder === folder.id ? '' : get().folder })
    await Promise.all([loadDocuments(base.id), loadFolders(base.id)])
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* File one document, and put the counts right.

   The row the server answers with replaces the row here; the folder counts are
   re-read, because a move changes two of them and working out which two is the
   kind of arithmetic that drifts. */
export async function move(doc: KbDoc, folderId: string): Promise<void> {
  const base = get().opened
  if (!base || get().busy) return
  patch({ busy: true, moving: null })
  try {
    const written = await source().move(doc.id, folderId)
    patch({ documents: (get().documents ?? []).map((row) => (row.id === written.id ? written : row)) })
    await loadFolders(base.id)
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/** One row into the open list, replacing the row it stands for. Only while
 *  the reader is still in the base it belongs to. */
function put(baseId: string, doc: KbDoc): void {
  if (get().opened?.id !== baseId) return
  const rows = get().documents ?? []
  const has = rows.some((row) => row.id === doc.id)
  patch({ documents: has ? rows.map((row) => (row.id === doc.id ? doc : row)) : [...rows, doc] })
}

/* What the Add Document menu's four rows do.

   Each adds one document and files it where the reader is standing: a file
   added while a folder is open belongs to that folder, which is the whole
   point of having opened it. The base's own counts are re-read after.

   Two calls, then a third: the server takes the file and queues it, because
   embedding outlives a request (raven/rpc/methods/knowledge.py says so at
   `knowledge.documents.add`), and nothing anywhere drains that queue on its
   own. The row appears as queued the moment it exists and is replaced by
   whatever the indexing made of it -- without the second call it would sit at
   queued for good. */
async function added(make: () => Promise<KbDoc>): Promise<void> {
  const base = get().opened
  if (!base || get().busy) return
  patch({ busy: true })
  try {
    let doc = await make()
    if (get().folder) doc = await source().move(doc.id, get().folder)
    put(base.id, doc)
    await loadFolders(base.id)
    /* Failure comes back as a row that says failed and why, not as a throw:
       the manager records it on the document. */
    put(base.id, await source().reindex(doc.id))
  } catch (e) {
    toast(reasonOf(e))
    /* A call that did not come back leaves this list's copy of the status a
       guess, and the status is the server's to say. */
    if (get().opened?.id === base.id) await loadDocuments(base.id)
  } finally {
    patch({ busy: false })
  }
}

export async function upload(name: string, contentB64: string): Promise<void> {
  await added(() => source().upload(get().opened!.id, name, contentB64))
}

export async function addUrl(url: string): Promise<void> {
  const trimmed = url.trim()
  if (trimmed) await added(() => source().addUrl(get().opened!.id, trimmed))
}

export async function addNote(title: string, text: string): Promise<void> {
  const named = title.trim()
  if (named) await added(() => source().addNote(get().opened!.id, named, text))
}

/** The documents the open folder holds, in the order they were added. */
export function shownDocs(s: KnowledgeState = get()): KbDoc[] {
  return (s.documents ?? []).filter((doc) => doc.folder_id === s.folder)
}

export async function loadDocuments(baseId: string): Promise<void> {
  try {
    const rows = await source().documents(baseId)
    /* Only if the reader is still in the base that was asked for: a switch
       while this was in flight would otherwise land one base's documents
       under another's name. */
    if (get().opened?.id === baseId) patch({ documents: rows, failed: null })
  } catch (e) {
    if (get().opened?.id === baseId) patch({ documents: [], failed: reasonOf(e) })
  }
}

/* Where the gateway serves a document's own bytes. By id and nothing else:
   the blobs sit under raven's state directory, and a request that names no
   location cannot be pointed at the rest of it (raven/rpc/knowledge_preview.py
   says why the route takes an id rather than a path). */
export function fileUrl(id: string): string {
  return `/knowledge/file?document=${encodeURIComponent(id)}`
}

/* Formats no browser draws, which the gateway renders to PDF with LibreOffice
   on the way out. Keyed on the upload's own name because that is what the
   record kept -- the stored blob has no suffix at all. */
const CONVERTED = new Set(['doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'odt', 'odp', 'ods', 'rtf'])

/* What a frame draws as it stands. Everything else is offered as a download
   rather than shown as a wall of bytes. */
const NATIVE = new Set([
  'pdf', 'txt', 'csv', 'tsv', 'json', 'xml', 'yaml', 'yml', 'html', 'htm', 'log', 'rst',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif',
])

/* Markdown is its own kind because framing it shows the source: the gateway
   serves .md as text/plain -- correctly, it is text -- and a frame then draws
   the hashes and the pipes, which is the file rather than the document. */
const MARKDOWN = new Set(['md', 'markdown', 'mdx'])

function suffixOf(doc: KbDoc): string {
  const dot = (doc.source || '').lastIndexOf('.')
  return dot < 0 ? '' : doc.source.slice(dot + 1).toLowerCase()
}

/** How the left half draws this file, or that it cannot. */
export function previewKind(doc: KbDoc): 'markdown' | 'native' | 'converted' | 'none' {
  const ext = suffixOf(doc)
  if (MARKDOWN.has(ext)) return 'markdown'
  if (CONVERTED.has(ext)) return 'converted'
  return NATIVE.has(ext) ? 'native' : 'none'
}

/* Where the left half reads the file from, and where in it to open.

   The fragment is the PDF open-parameters set, which the browser's own viewer
   reads: `page` is 1-based, and `view=FitH,<top>` fits the width and puts that
   point at the top of the frame. Its coordinates are points from the page's
   top-left, which is the space the parser recorded the regions in -- so the
   number goes straight through with no page height to look up.

   Only for what is drawn as a PDF, native or converted. A viewer that does not
   read the fragment lands on the file's first page, which is where it would
   have landed anyway. */
export function previewUrl(doc: KbDoc, aim: KnowledgeState['aim'] = null): string {
  const kind = previewKind(doc)
  const url = kind === 'converted' ? `${fileUrl(doc.id)}&render=pdf` : fileUrl(doc.id)
  if (!aim || (kind !== 'converted' && suffixOf(doc) !== 'pdf')) return url
  return `${url}#page=${aim.page}&view=FitH,${Math.max(0, Math.round(aim.top))}`
}

/** The file itself, for the one kind this page draws rather than frames. */
export async function readText(doc: KbDoc): Promise<string> {
  const res = await fetch(previewUrl(doc))
  if (!res.ok) throw new Error((await res.text()).trim() || `${res.status} ${res.statusText}`)
  return res.text()
}

export function setView(view: View): void {
  patch({ view })
}

/* Cut and embed one document again. The row the server answers with replaces
   the row here, the way every other write on this page works: reindexing
   changes the status and the piece count, and a page that guessed either would
   be showing a document as ready before it is. */
export async function reindex(doc: KbDoc): Promise<void> {
  if (get().busy) return
  patch({ busy: true })
  /* Marked as indexing the moment it is asked for, not when it answers. The
     call runs for as long as parsing and embedding the file takes -- tens of
     seconds on a long PDF -- and a row that does not move until it is over
     reads as a press that did nothing. */
  patch({
    documents: (get().documents ?? []).map((row) => (row.id === doc.id ? { ...row, status: 'indexing' } : row)),
  })
  try {
    const written = await source().reindex(doc.id)
    patch({ documents: (get().documents ?? []).map((row) => (row.id === written.id ? written : row)) })
    /* Its pieces are not the pieces it had. A rebuild deletes every one and
       cuts the file again by the settings as they now stand, so a panel still
       showing the old ones is showing pieces that are no longer in the index
       -- and the piece it was scrolled to addresses nothing. */
    if (get().viewing?.id === doc.id) {
      patch({ aimed: '', aim: null, picked: [], editing: '', chunkPage: 1, chunks: null })
      await loadChunks(doc)
    }
    /* And the base holds a different number of pieces than it did, which is
       what decides whether its embedding model can still be moved. */
    await load()
  } catch (e) {
    toast(reasonOf(e))
    /* The status is the server's to say, and this list's copy of it is now a
       guess: the row was marked indexing by this call, not by an answer. */
    const base = get().opened
    if (base) await loadDocuments(base.id)
  } finally {
    patch({ busy: false })
  }
}

/* The document goes. What asks first is the menu that raised this, the same
   way a base's does. */
export async function removeDoc(doc: KbDoc): Promise<void> {
  if (get().busy) return
  patch({ busy: true })
  try {
    await source().removeDoc(doc.id)
    patch({ documents: (get().documents ?? []).filter((row) => row.id !== doc.id) })
    /* Out of the viewer if it was what was open: a panel reading a document
       that is gone has nothing to show and no way to say why. */
    if (get().viewing?.id === doc.id) closeDoc()
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

export function setTab(tab: Tab): void {
  patch({ tab })
}

export function openCreate(): void {
  patch({ sheet: { kind: 'create' } })
}

/* The models a base could be moved onto, read once per page.

   Failure is the empty list rather than a message: the panel still opens, with
   the model it is on and nothing to change it to, and a reader who cannot see
   a catalogue can still read every other setting. */
async function loadModels(): Promise<void> {
  if (get().models !== null) return
  try {
    patch({ models: await source().models() })
  } catch {
    patch({ models: [] })
  }
}

export function openSettings(base: KbBase): void {
  patch({ sheet: { kind: 'settings', base } })
  /* Only where they could be used: a base that holds pieces cannot be moved,
     so reading every provider's catalogue for it would be a call whose answer
     the panel is not allowed to offer. */
  if (base.chunks === 0) void loadModels()
}

export function openSheet(sheet: KnowledgeState['sheet']): void {
  patch({ sheet })
}

export function closeSheet(): void {
  patch({ sheet: null })
}

export async function create(name: string, description: string): Promise<void> {
  const named = name.trim()
  if (!named || get().busy) return
  patch({ busy: true })
  try {
    const made = await source().create(named, description.trim())
    /* Inserted rather than refetched: create answers with the same row shape
       the list sends, which is what that shape is for. */
    patch({ bases: [...(get().bases ?? []), made], sheet: null })
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

/* One write path for every card control, so no caller has to remember to put
   the answer back. The row the server returns replaces the row here; nothing
   guesses what a write did to the record. */
export async function write(base: KbBase, settings: KbSettings): Promise<void> {
  if (get().busy) return
  patch({ busy: true })
  try {
    const written = await source().settings(base.id, settings)
    patch({
      bases: (get().bases ?? []).map((row) => (row.id === written.id ? written : row)),
      ...(get().opened?.id === written.id ? { opened: written } : {}),
    })
  } catch (e) {
    toast(reasonOf(e))
  } finally {
    patch({ busy: false })
  }
}

export function toggleStar(base: KbBase): void {
  void write(base, { starred: !base.starred })
}

export function togglePin(base: KbBase): void {
  void write(base, { pinned: !base.pinned })
}

/* The base goes, with its documents and its vectors. What asks first is the
   menu that raised this: it rewords its own row and takes a second press
   (KnowledgePage.tsx's `raise`), so the guard is one place rather than two --
   which it was, and the two disagreed: the menu armed, and this armed again,
   so the second press only re-armed. */
export function remove(base: KbBase): void {
  if (get().busy) return
  patch({ busy: true })
  void source()
    .remove(base.id)
    .then(() =>
      patch({
        bases: (get().bases ?? []).filter((row) => row.id !== base.id),
        /* Out of the base that just went, rather than leaving the path naming
           it and the list showing documents nothing holds any more. */
        ...(get().opened?.id === base.id ? { opened: null, documents: null } : {}),
      }),
    )
    .catch((e: unknown) => toast(reasonOf(e)))
    .finally(() => patch({ busy: false }))
}

/* The rows a tab shows, in the order it shows them.

   Pinned first on every tab: the pin is about order, and a reader who pinned a
   base meant it wherever they are looking. Within that the tab decides --
   `recent` by when the base was made, newest first, and the other two by name,
   which is the order a reader scanning for one reads in. */
export function shown(s: KnowledgeState = get()): KbBase[] {
  const rows = (s.bases ?? []).filter((row) => (s.tab === 'starred' ? row.starred : true))
  const order =
    s.tab === 'recent'
      ? (a: KbBase, b: KbBase) => b.created_at.localeCompare(a.created_at)
      : (a: KbBase, b: KbBase) => a.name.localeCompare(b.name)
  return [...rows].sort((a, b) => Number(b.pinned) - Number(a.pinned) || order(a, b))
}
