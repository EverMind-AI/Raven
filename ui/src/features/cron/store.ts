import { ds, shell } from '../../shell/bridge'

import type { CronDraft, CronJob, CronSource } from './types'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (nav opens it, Esc closes it, a finished turn refreshes it,
 * a language flip redraws it), so the state lives in a plain store the
 * shims can call, and the component subscribes.
 */

export interface CronState {
  rows: CronJob[]
  /* Bumped by every refresh answer: what the run-history refetch keys on,
     since a refresh swaps row objects without changing any identity a
     component's dep array could see. */
  rev: number
  /* False until the first rows fetch answers: the legacy page cleared the
     stage on first open rather than showing a not-yet-loaded empty state. */
  loaded: boolean
  viewId: string | null
  /* Drafts are mutable objects edited in place by uncontrolled inputs --
     the same discipline the legacy form kept: a keystroke changes no state
     anyone re-renders on, so focus and IME composition are never disturbed.
     `epoch` remounts the form subtrees when a draft is replaced. */
  draft: CronDraft | null
  sheet: CronDraft | null
  epoch: number
  /* Bumped only by a language flip: run stamps arrive language-baked from
     the source, so the flip must refetch them, while the plain redraws the
     island's own controls ask for must not. */
  lang: number
}

let state: CronState = { rows: [], rev: 0, loaded: false, viewId: null, draft: null, sheet: null, epoch: 0, lang: 0 }
const listeners = new Set<() => void>()

export const getState = (): CronState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<CronState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): CronSource => ds<CronSource>('cron')

export async function refresh(): Promise<void> {
  try {
    const rows = await source().rows()
    set({ rows, loaded: true, rev: state.rev + 1 })
  } catch (e) {
    shell().toast(`加载失败：${(e as Error).message || e}`)
    set({ loaded: true, rev: state.rev + 1 })
  }
}

/* Boot calls this through the shim to prefetch without opening the page. */
export function warm(): Promise<void> {
  return source()
    .rows()
    .then((rows) => set({ rows, loaded: true }))
    .catch(() => {})
}

export function open(): void {
  set({ viewId: null, draft: null })
  shell().showPage('cronPage')
  void refresh()
}

export function close(): void {
  shell().showPage(null)
}

export function openDetail(j: CronJob): void {
  set({ viewId: j.id, draft: { ...j }, epoch: state.epoch + 1 })
}

export function backToList(): void {
  set({ viewId: null, draft: null })
}

/* After a successful save the reader stays on the job's page and the form
   shows what was saved: the draft is rebuilt from the saved row (remounted
   via epoch), never merely dropped -- a null draft here would fall through
   to the list with viewId still set. */
export function viewSaved(saved: CronJob | null): void {
  set({ viewId: saved ? saved.id : null, draft: saved ? { ...saved } : null, epoch: state.epoch + 1 })
  void refresh()
}

export function openSheet(j?: CronDraft): void {
  const draft: CronDraft = j ?? {
    id: 'j' + Date.now(),
    name: '',
    what: '',
    freq: 'day',
    at: '08:00',
    on: true,
    deliver: 'app',
    when: '',
    next: '',
    runs: [],
    fresh: true,
  }
  set({ sheet: draft, epoch: state.epoch + 1 })
}

export function closeSheet(): void {
  set({ sheet: null })
}

/* A language flip changes nothing in this state, but every visible string
   comes from T(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}

/* The shim's redraw: what the legacy language flip calls. */
export function langRedraw(): void {
  set({ lang: state.lang + 1 })
}
