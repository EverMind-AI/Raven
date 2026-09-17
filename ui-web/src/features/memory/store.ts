import { ds } from '../../state/sources'
import * as detail from '../../state/detail'

import type { MemItem, MemKind, MemStats, MemorySource } from './types'
import * as page from '../../state/page'
import { makeStore } from '../../state/store'

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

export const source = (): MemorySource => ds('memory')

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

export function open(): void {
  page.show('memoryPage')
  void refreshStats()
  void load()
}

export function close(): void {
  page.show(null)
}

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

/** Where the card renders: the host the shared drawer keeps for this island. */
export function detailHost(): HTMLDivElement {
  return detail.host('memory')
}

export function openDetail(it: MemItem): void {
  detail.open('memory')
  set({ detail: it })
}

export function closeDetail(): void {
  detail.close()
}

/* What this island does when the drawer closes, whoever closed it (Esc, the
   close button, a click outside, a page switch): drop the card -- but not until
   the drawer has finished fading, or the card is gone from inside a panel that
   is still on screen. `gen` is which open the drop belongs to: the item cannot
   answer that, because reopening the same row hands back the same object. */
export function detailDismissed(): void {
  if (!get().detail) return
  const gen = detail.get().gen
  detail.dropAfterFade(
    () => set({ detail: null }),
    () => detail.get().gen !== gen,
  )
}
detail.onClose('memory', detailDismissed)

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

/* A language flip changes nothing in this state, and every visible string comes
   from t() -- so the island's own subscription to the language store is the
   whole redraw, and this verb is what a caller outside React asks for one by. */
export function redraw(): void {
  set({})
}
