import { t } from '../../i18n/t'
import * as settingsDialog from '../../state/settings'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { CronDraft, CronJob, CronSource } from './types'

/* Section state, outside React on purpose: the callers that drive this
 * section are not React. Schedules is a section of the settings dialog rather
 * than a page of its own, so `open` below raises that dialog on this section
 * and the dialog's own Escape takes it back; the boot prefetches the rows
 * (app/boot.ts) and the page's own teardown shuts the sheet
 * (app/install.ts) -- so the state lives in a plain store those callers can
 * reach, and the component subscribes.
 */

export interface CronState {
  rows: CronJob[]
  /* Bumped by every refresh answer: what the run-history refetch keys on,
     since a refresh swaps row objects without changing any identity a
     component's dep array could see. */
  rev: number
  /* False until the first rows fetch answers: the list is not drawn at all
     until then, so a page still loading never reads as "no jobs". */
  loaded: boolean
  viewId: string | null
  /* Drafts are mutable objects edited in place by uncontrolled inputs: a
     keystroke changes no get() anyone re-renders on, so focus and IME
     composition are never disturbed.
     `epoch` remounts the form subtrees when a draft is replaced. */
  draft: CronDraft | null
  sheet: CronDraft | null
  /* Whether the sheet is parked: the reader left this section with a draft
     still in it. The draft is kept rather than dropped, because the way out of
     this form that a reader actually takes is the one link inside it -- "where
     results go" opens Channels -- and dropping the draft there costs a
     half-written job to a press that reads like a detour. Off screen while
     parked, since the sheet is a veil at the body and would otherwise float
     over whichever section the reader went to. */
  parked: boolean
  epoch: number
}

const store = makeStore<CronState>({
  rows: [], rev: 0, loaded: false, viewId: null, draft: null, sheet: null, parked: false, epoch: 0,
})

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
  settingsDialog.openSection('cron')
}

/* What arriving at this section of the settings dialog costs: the list, from
   the top -- so a reader who picks the row in the dialog's own nav gets the
   same fetch the opener above does. Registered at this module's own evaluation
   rather than by the page's wiring, the same shape features/desk/store.ts fills
   state/escapeOrder.ts's slot with: the alternative is src/app/install.ts
   importing three island stores for three lines, which is three island graphs
   in the page's own wiring. */
function enter(): void {
  /* Un-parked: a draft the reader left here is what they come back to. */
  set({ viewId: null, draft: null, parked: false })
  void refresh()
}
settingsDialog.onEnter('cron', enter)

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
  set({ sheet: draft, parked: false, epoch: get().epoch + 1 })
}

export function closeSheet(): void {
  set({ sheet: null, parked: false })
}

/* What leaving the section costs: the sheet goes off screen, and the draft in
   it does not. It used to be dropped here, which made the form's own link to
   Channels -- the one a reader follows to set a delivery route up while they
   are writing the job that needs it -- delete the job with no prompt. */
function park(): void {
  if (get().sheet) set({ parked: true })
}
settingsDialog.onLeave('parkCronSheet', park)

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. The island's own controls
   ask for this after writing a draft in place, which no get() can see. */
export function redraw(): void {
  set({})
}
