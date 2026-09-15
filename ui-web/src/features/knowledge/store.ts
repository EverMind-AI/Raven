import { shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { KbBase, KbDoc, KbHit, KbStatus, KnowledgeSource } from './types'

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
  /* The document whose original file is on screen, or null for the table.
     Held as the record rather than an id: the viewer needs the name and the
     media type to decide what it is showing, and a row that is deleted while
     open should not leave the panel looking up an id that is gone. */
  viewing: KbDoc | null
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
  viewing: null,
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

export async function create(name: string, description = ''): Promise<void> {
  const trimmed = name.trim()
  if (!trimmed || state.busy) return
  set({ busy: true })
  try {
    await source().create(trimmed, description)
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
  set({ openId: id, docs: [], query: '', hits: null, viewing: null })
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
  set({ openId: null, docs: [], query: '', hits: null, viewing: null })
}

export async function upload(file: File): Promise<void> {
  const baseId = state.openId
  if (!baseId || state.busy) return
  set({ busy: true })
  try {
    const doc = await source().upload(baseId, file)
    /* Shown queued straight away, then indexed: embedding a document outlives
       a request, and a panel that waits for it looks broken. */
    if (state.openId === baseId) set({ docs: [...state.docs, doc] })
    const indexed = await source().index(doc.id)
    if (state.openId === baseId) {
      set({ docs: state.docs.map((d) => (d.id === indexed.id ? indexed : d)) })
    }
  } catch (e) {
    toast(said(e))
  } finally {
    set({ busy: false })
    /* The base's own document count lives on the row behind this panel. */
    void load()
  }
}

/* One embedding request per keystroke is what the plain handler cost: every
   character ran `knowledge.search`, which embeds the query through the
   configured endpoint -- billed, unthrottled, and on a client whose timeout is
   measured in minutes. Typing a two-word question spent seventeen of them.
   Debounced like the skill hub's query, which had the same problem. */
const SEARCH_DEBOUNCE_MS = 250
let debounce: ReturnType<typeof setTimeout> | null = null

/* Monotonic, and checked before an answer is kept. The debounce alone does not
   fix the ordering: the requests that do go out race, and the guard on `openId`
   passes for every one of them, so a slow prefix landing last used to paint its
   hits under the text the reader had already finished typing. */
let seq = 0

function cancelSearch(): void {
  if (debounce) clearTimeout(debounce)
  debounce = null
  /* Bumped, not just cleared: a request already in flight has to be disowned
     too, or it repaints a panel the reader has left or emptied. */
  seq += 1
}

async function run(baseId: string, text: string, mine: number): Promise<void> {
  try {
    const hits = await source().search([baseId], text)
    if (state.openId === baseId && mine === seq) set({ hits })
  } catch (e) {
    if (state.openId === baseId && mine === seq) toast(said(e))
  }
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
    if (state.openId === baseId) set({ docs })
  } catch {
    /* The row list stays as the optimistic removal left it; the next open
       corrects it. Toasting twice for one failure helps nobody. */
  }
}

/* What the box calls on every keystroke. */
export function search(query: string): void {
  const baseId = state.openId
  const text = query.trim()
  set({ query })
  cancelSearch()
  if (!baseId || !text) {
    set({ hits: null })
    return
  }
  const mine = seq
  debounce = setTimeout(() => {
    debounce = null
    void run(baseId, text, mine)
  }, SEARCH_DEBOUNCE_MS)
}

/* The same search without the wait, for a caller that already knows the reader
   is done typing. Mirrors `searchNow` on the skill hub's store. */
export async function searchNow(query: string): Promise<void> {
  const baseId = state.openId
  const text = query.trim()
  set({ query })
  cancelSearch()
  if (!baseId || !text) {
    set({ hits: null })
    return
  }
  await run(baseId, text, seq)
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

export function previewKind(doc: KbDoc): 'native' | 'converted' | 'none' {
  const ext = suffixOf(doc)
  if (CONVERTED.has(ext)) return 'converted'
  return NATIVE.has(ext) ? 'native' : 'none'
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
