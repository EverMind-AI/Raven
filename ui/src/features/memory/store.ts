import { ds, shell } from '../../shell/bridge'

import type { MemItem, MemKind, MemStats, MemorySource } from './types'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (nav opens it, Esc closes it, a language flip redraws it),
 * so the state lives in a plain store the shims can call, and the
 * component subscribes.
 */

export const MEM_PAGE_SIZE = 20

export type MemPhase = 'idle' | 'loading' | 'ready' | 'error' | 'down'

export interface MemoryState {
  kind: MemKind
  page: number
  q: string
  items: MemItem[]
  total: number
  stats: MemStats | null
  /* 'error' is the live failure (inline line + retry, with the detail);
     'down' is the fixture source's answer, rendered as the demo's plain
     down note without the stat band. */
  phase: MemPhase
  err: string
  detail: MemItem | null
}

let state: MemoryState = {
  kind: 'episode',
  page: 1,
  q: '',
  items: [],
  total: 0,
  stats: null,
  phase: 'idle',
  err: '',
  detail: null,
}
const listeners = new Set<() => void>()
let debounce: ReturnType<typeof setTimeout> | undefined
let busy = false

export const getState = (): MemoryState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<MemoryState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): MemorySource => ds<MemorySource>('memory')

function failure(e: unknown): string {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err.data && err.data.detail) || err.message || String(e)
}

export async function load(): Promise<void> {
  set({ phase: 'loading', err: '' })
  try {
    const r = await source().list({ kind: state.kind, page: state.page, page_size: MEM_PAGE_SIZE, q: state.q || null })
    set({ items: r.items || [], total: r.total || 0, phase: 'ready' })
  } catch (e) {
    if ((e as { down?: boolean }).down) set({ phase: 'down' })
    else set({ phase: 'error', err: failure(e) })
  }
}

export function refreshStats(): Promise<void> {
  return source()
    .stats()
    .then((stats) => set({ stats }))
    .catch(() => set({ stats: null }))
}

export function open(): void {
  shell().showPage('memPage')
  void refreshStats()
  void load()
}

export function close(): void {
  shell().showPage(null)
}

export function setKind(kind: MemKind): void {
  if (state.kind === kind) return
  clearTimeout(debounce)
  closeDetail()
  set({ kind, page: 1, q: '', items: [] })
  void load()
}

/* A keystroke changes no pixels until the reload lands, so the query is
   stored without notifying -- the debounced reload is the only redraw. */
export function search(q: string): void {
  state = { ...state, q }
  clearTimeout(debounce)
  debounce = setTimeout(() => {
    state = { ...state, page: 1 }
    void load()
  }, 350)
}

export function pageBy(delta: number): void {
  set({ page: state.page + delta })
  void load()
}

/* The portal's own container inside the shared #dBody. Another page's
   opener may wipe #dBody at any time (the skills/plugins openers do),
   which detaches this node but leaves React's tree inside it intact;
   every open re-adopts it, so the island never reconciles into nodes
   a legacy wipe orphaned. */
let host: HTMLDivElement | null = null
export function detailHost(): HTMLDivElement {
  if (!host) {
    host = document.createElement('div')
    /* Out of the box tree: #dBody is a grid and the sections were its
       items before this container existed; contents keeps them so. */
    host.style.display = 'contents'
  }
  return host
}

export function openDetail(it: MemItem): void {
  const body = document.getElementById('dBody')
  if (body && !body.contains(detailHost())) {
    body.innerHTML = ''
    body.appendChild(detailHost())
  }
  set({ detail: it })
}

export function closeDetail(): void {
  shell().closeDetail?.()
  if (state.detail) set({ detail: null })
}

/* Called when legacy chrome closed the drawer itself (Esc, the close
   button, a click outside): only the island state has to follow. */
export function detailDismissed(): void {
  if (state.detail) set({ detail: null })
}

export function remove(it: MemItem): void {
  if (busy) return
  busy = true
  source()
    .remove(it)
    .then(() => {
      closeDetail()
      void refreshStats()
      return load()
    })
    .catch((e: unknown) => {
      if ((e as { handled?: boolean }).handled) return
      console.error('memory delete', e)
    })
    .finally(() => {
      busy = false
    })
}

/* A language flip changes nothing in this state, but every visible string
   comes from T(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
