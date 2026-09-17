import { t } from '../../i18n/t'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'

import type { CronDraft, CronJob, CronSource } from './types'
import * as page from '../../state/page'
import { makeStore } from '../../state/store'

/* Page state, outside React on purpose: the callers that drive this page are
 * not React. The rail's flyout and the nav open it (state/navfly.ts), the
 * Escape order closes it (state/escapeOrder.ts), the boot prefetches it
 * (app/boot.ts) and the page's own teardown shuts its sheet
 * (app/install.ts) -- so the state lives in a plain store those four can
 * call, and the component subscribes.
 */

export interface CronState {
  rows: CronJob[]
  /* Bumped by every refresh answer: what the run-history refetch keys on,
     since a refresh swaps row objects without changing any identity a
     component's dep array could see. */
  rev: number
  /* False until the first rows fetch answers: the legacy page cleared the
     stage on first open rather than showing a not-yet-loaded empty get(). */
  loaded: boolean
  viewId: string | null
  /* Drafts are mutable objects edited in place by uncontrolled inputs --
     the same discipline the legacy form kept: a keystroke changes no get()
     anyone re-renders on, so focus and IME composition are never disturbed.
     `epoch` remounts the form subtrees when a draft is replaced. */
  draft: CronDraft | null
  sheet: CronDraft | null
  epoch: number
}

const store = makeStore<CronState>({ rows: [], rev: 0, loaded: false, viewId: null, draft: null, sheet: null, epoch: 0 })

export const { get, subscribe, _resetForTests } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<CronState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): CronSource => ds('cron')

export async function refresh(): Promise<void> {
  try {
    const rows = await source().rows()
    set({ rows, loaded: true, rev: get().rev + 1 })
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: String((e as Error).message || e) }))
    set({ loaded: true, rev: get().rev + 1 })
  }
}

/* app/boot.ts calls this to prefetch the rows without opening the page. */
export function warm(): Promise<void> {
  return source()
    .rows()
    .then((rows) => set({ rows, loaded: true }))
    .catch(() => {})
}

export function open(): void {
  set({ viewId: null, draft: null })
  page.show('cronPage')
  void refresh()
}

export function close(): void {
  page.show(null)
}

export function openDetail(j: CronJob): void {
  set({ viewId: j.id, draft: { ...j }, epoch: get().epoch + 1 })
}

export function backToList(): void {
  set({ viewId: null, draft: null })
}

/* After a successful save the reader stays on the job's page and the form
   shows what was saved: the draft is rebuilt from the saved row (remounted
   via epoch), never merely dropped -- a null draft here would fall through
   to the list with viewId still set. */
export function viewSaved(saved: CronJob | null): void {
  set({ viewId: saved ? saved.id : null, draft: saved ? { ...saved } : null, epoch: get().epoch + 1 })
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
  set({ sheet: draft, epoch: get().epoch + 1 })
}

export function closeSheet(): void {
  set({ sheet: null })
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. The island's own controls
   ask for this after writing a draft in place, which no get() can see. */
export function redraw(): void {
  set({})
}
