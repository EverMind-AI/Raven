import * as settingsDialog from '../../state/settings'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'

import type { MemItem, MemKind, MemStats, MemorySource } from './types'

/* Section state, outside React on purpose: the callers that drive this section
 * are not React. Memory is a section of the settings dialog rather than a page
 * of its own, so the dialog's own nav is what opens it and the dialog's own
 * Escape takes it back -- the state lives in a plain store those callers can
 * reach, and the component subscribes.
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
  /* Why there is nothing to show, when that is not a failure: no memory
     plugin installed, or one installed that memory.backend does not name.
     Four zeros and a retry button said neither, and read as "your memories
     are gone" instead of "they are not kept here". */
  note: string
  err: string
  detail: MemItem | null
}

const store = makeStore<MemoryState>({
  kind: 'episode',
  page: 1,
  q: '',
  items: [],
  total: 0,
  stats: null,
  phase: 'idle',
  note: '',
  err: '',
  detail: null,
})
let debounce: ReturnType<typeof setTimeout> | undefined
let busy = false

export const { get, subscribe, _resetForTests } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<MemoryState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): MemorySource => ds('memory')

function failure(e: unknown): string {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err.data && err.data.detail) || err.message || String(e)
}

export async function load(): Promise<void> {
  /* `note` is cleared with `err`: it explains the answer this load is about
     to fetch, and a stale one outlives the condition it described -- after
     installing the plugin the page would keep saying it is missing. */
  set({ phase: 'loading', err: '', note: '' })
  try {
    const r = await source().list({ kind: get().kind, page: get().page, page_size: MEM_PAGE_SIZE, q: get().q || null })
    set({ items: r.items || [], total: r.total || 0, note: r.note || '', phase: 'ready' })
  } catch (e) {
    if ((e as { down?: boolean }).down) set({ phase: 'down' })
    else set({ phase: 'error', err: failure(e) })
  }
}

export function refreshStats(): Promise<void> {
  return source()
    .stats()
    .then((stats) => set({ stats, note: (stats && stats.note) || get().note }))
    .catch(() => set({ stats: null }))
}

/* What arriving at this section of the settings dialog costs: the counters and
   the first page -- so a reader who picks the row in the dialog's own nav gets
   the same two reads the opener above does. Registered at this module's own
   evaluation rather than by the page's wiring, the same shape
   features/desk/store.ts fills state/escapeOrder.ts's slot with: the
   alternative is src/app/install.ts importing three island stores for three
   lines, which is three island graphs in the page's own wiring. */
function enter(): void {
  void refreshStats()
  void load()
}
settingsDialog.onEnter('memory', enter)

export function setKind(kind: MemKind): void {
  if (get().kind === kind) return
  clearTimeout(debounce)
  closeDetail()
  set({ kind, page: 1, q: '', items: [] })
  void load()
}

/* A keystroke changes no pixels until the reload lands, so the query is
   stored without notifying -- the debounced reload is the only redraw. */
export function search(q: string): void {
  set({ q })
  clearTimeout(debounce)
  debounce = setTimeout(() => {
    set({ page: 1 })
    void load()
  }, 350)
}

export function pageBy(delta: number): void {
  set({ page: get().page + delta })
  void load()
}

/* Which memory the right column is showing. It used to be the shared drawer's
   card, with an onClose registration and a fade to wait out; beside its own
   list it is one field, and a pick replaces it. */
export function openDetail(it: MemItem): void {
  set({ detail: it })
}

function closeDetail(): void {
  set({ detail: null })
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
