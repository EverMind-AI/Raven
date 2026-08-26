import { shell } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { KbBase, KbDoc, KbHit, KbStatus, KnowledgeSource } from './types'

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
  set({ openId: id, docs: [] })
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
  set({ openId: null, docs: [], query: '', hits: null })
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
    toast((e as Error)?.message || String(e))
  } finally {
    set({ busy: false })
    /* The base's own document count lives on the row behind this panel. */
    void load()
  }
}

export async function search(query: string): Promise<void> {
  const baseId = state.openId
  const text = query.trim()
  set({ query })
  if (!baseId || !text) {
    set({ hits: null })
    return
  }
  try {
    const hits = await source().search([baseId], text)
    if (state.openId === baseId) set({ hits })
  } catch (e) {
    if (state.openId === baseId) toast((e as Error)?.message || String(e))
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
}
