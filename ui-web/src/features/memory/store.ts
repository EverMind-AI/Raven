import { t } from '../../i18n/t'
import * as confirm from '../../state/confirm'
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
  /* The local/cloud switch: a write in flight, the server's sentence about
     what waits for a reload, and a write that failed. Kept apart from `err`,
     which takes the whole section over -- a failed switch leaves the section
     readable on the backend it still has. */
  backendBusy: boolean
  backendNotice: string
  backendErr: string
  /* The cloud key-entry panel: a write being checked, the sentence it was
     refused with, and whether the key on file is known bad. The last one keeps
     the panel up after the write -- `cloud_key_set` turns true either way. */
  cloudBusy: boolean
  cloudErr: string
  cloudRejected: boolean
  /* The reader asked to replace a working key. The panel is the same one the
     no-key state uses; this is what opens it over a store that has a key. */
  cloudEditing: boolean
  /* The local server's start button: a start in flight and its refusal. */
  localBusy: boolean
  localErr: string
  /* A backend switch's reads are out. The old store's counts and rows must not
     sit under the new segment while they are; the section draws the wait. */
  switching: boolean
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
  backendBusy: false,
  backendNotice: '',
  backendErr: '',
  cloudBusy: false,
  cloudErr: '',
  cloudRejected: false,
  cloudEditing: false,
  localBusy: false,
  localErr: '',
  switching: false,
})
let debounce: ReturnType<typeof setTimeout> | undefined

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

/* The service refusing the key on file. The section can fix this one in place,
   so it must not be read as the service being down. The cloud's own body for
   this is "The API key in the request is missing or invalid", which carries
   no 401 in text -- the status is in the transport, not the sentence. */
const AUTH_FAILURE = /\b(401|403)\b|unauthori[sz]ed|missing authorization|api key\b[^.]*(missing|invalid)/i

export async function load(): Promise<void> {
  /* `note` is cleared with `err`: it explains the answer this load is about
     to fetch, and a stale one outlives the condition it described -- after
     installing the plugin the page would keep saying it is missing. */
  set({ phase: 'loading', err: '', note: '' })
  try {
    const r = await source().list({ kind: get().kind, page: get().page, page_size: MEM_PAGE_SIZE, q: get().q || null })
    set({ items: r.items || [], total: r.total || 0, note: r.note || '', phase: 'ready' })
  } catch (e) {
    const detail = failure(e)
    /* A key on file that the service refuses is the key panel's state, not the
       store's outage: the reader can replace it right here. Without this, a
       re-entry after a bad key landed showed the error frame and no way back
       to the field. */
    if (get().stats?.backend === 'everos-cloud' && get().stats?.cloud_key_set && AUTH_FAILURE.test(detail)) {
      set({
        phase: 'ready',
        items: [],
        total: 0,
        detail: null,
        cloudRejected: true,
        cloudErr: t('gui.mem.cloud_bad_key'),
      })
      return
    }
    if ((e as { down?: boolean }).down) set({ phase: 'down' })
    else set({ phase: 'error', err: detail })
  }
}

export function refreshStats(): Promise<void> {
  return source()
    .stats()
    .then((stats) => set({ stats, note: (stats && stats.note) || get().note }))
    .catch(() => set({ stats: null }))
}

/* The local/cloud switch's first half: a turn in flight is a question, not a
   spinner. The loop switches only when that turn finishes, so the reader is
   told that before anything is written and can back out. A status probe that
   cannot answer does not block the switch -- `applyRuntime` still waits. */
export function switchBackend(backend: string): void {
  if (get().backendBusy || get().stats?.backend === backend) return
  set({ backendBusy: true, backendErr: '' })
  void (async () => {
    let busy = false
    try {
      busy = (await source().switchStatus()).active
    } catch {
      /* The guard is an explanation, not the safety boundary: the rebind waits
         for the turn itself. */
    }
    if (busy) {
      set({ backendBusy: false })
      confirm.ask(
        t('gui.mem.switch_busy_t'),
        t('gui.mem.switch_busy_sub'),
        t('gui.mem.switch_confirm'),
        () => performSwitch(backend),
        'primary',
      )
      return
    }
    performSwitch(backend)
  })()
}

/* The switch itself: the config write, the live rebind and the reads that
   follow. The section re-reads the file per call, so its rows follow at once;
   the loop serving chat memory keeps the backend it booted with until
   `applyRuntime` points it at this one, which is a live reference swap rather
   than a reload. */
function performSwitch(backend: string): void {
  const stats = get().stats
  /* The segment moves at once, and the old store's numbers and rows go with
     it: they belong to the backend the reader just left. The wait below is
     what the section draws instead. */
  set({
    stats: stats ? { ...stats, backend } : stats,
    switching: true,
    backendBusy: true,
    backendNotice: '', backendErr: '',
    cloudRejected: false, cloudErr: '', cloudEditing: false, localErr: '',
    page: 1, q: '', detail: null, items: [], total: 0, phase: 'loading',
  })
  void (async () => {
    try {
      /* The write applies the switch in place: the settings writer points the
         running loop at the file, so there is no second apply to do here and
         no generation reload -- the document's socket stays connected. What the
         write cannot promise is the moment: a switch asked for while a turn is
         running waits for that turn, and the reply carries that sentence. */
      const reply = await source().setBackend(backend)
      /* The settings dialog's models section drew its EverOS Cloud card from the
         snapshot it read when it opened, and `selected` just changed. Without
         this the reader who goes to enter the key finds no card. The island
         registers what re-reads it; state/ does not import an island. */
      settingsDialog.requestRefresh()
      set({ backendNotice: reply.warning || '' })
      await refreshStats()
      await load()
      set({ switching: false, backendBusy: false })
    } catch (e) {
      /* The optimistic segment above is a claim the file did not change, so
         put it back from the file rather than leaving the section reading one
         store while the switch says the other. */
      set({ switching: false, backendBusy: false, backendErr: failure(e) })
      await refreshStats()
      await load()
    }
  })()
}

/* A working key can be replaced too: the same panel, opened from the link
   under the source switch rather than by a missing key. */
export function startCloudEdit(): void {
  set({ cloudEditing: true, cloudErr: '', cloudRejected: false })
}

/* The local server's start button. The backend starts the same server when a
   generation boots; this is the section's way to do it after that process went
   away, without asking the reader to restart Raven. The server comes up
   through the local backend the loop is pointed at, so one call both starts it
   and moves chat onto it -- the fix for a switch whose apply failed, too. */
export function startLocal(): void {
  if (get().localBusy) return
  set({ localBusy: true, localErr: '' })
  void (async () => {
    try {
      const r = await source().startLocal()
      const queued = r.state === 'queued'
      set({
        localBusy: false,
        localErr: r.ok || queued ? '' : (r.detail || t('gui.mem.local_failed')),
        /* Applied means the loop now reads the file's backend, so a notice
           saying they disagree is over; queued is that notice's own case. */
        backendNotice: queued ? (r.detail || t('gui.mem.reload_wait')) : r.state === 'applied' ? '' : get().backendNotice,
      })
      if (r.ok) {
        await refreshStats()
        await load()
      }
    } catch (e) {
      set({ localBusy: false, localErr: failure(e) })
    }
  })()
}

/* The panel for a local server the plugin does not start: the one action is to
   look again once the user has started it. */
export function recheckLocal(): void {
  void (async () => {
    await refreshStats()
    await load()
  })()
}

/* The hosted backend's key, from the panel the section draws in place of the
   store when there is none. The write is not the validation: a key can be
   saved and still be rejected, so the backend's own health check answers
   before the section leaves its panel. Only an ``ok`` unlocks the store. */
export function saveCloudKey(key: string): void {
  const value = key.trim()
  if (!value || get().cloudBusy) return
  /* `cloudRejected` is deliberately NOT cleared here. When the panel is up
     because the key on file was refused, clearing it would make the panel's
     condition false for the duration of the check -- the section would flash to
     the store and back when the probe refused it again. The flag moves only
     when the verdict does. */
  set({ cloudBusy: true, cloudErr: '' })
  void (async () => {
    try {
      await source().setCloudKey(value)
      const probe = await source().probeCloud()
      if (probe.status !== 'ok') {
        set({
          cloudBusy: false,
          cloudEditing: false,
          cloudRejected: true,
          cloudErr: probe.hint || t('gui.mem.cloud_bad_key'),
        })
        return
      }
      /* The key is what the running loop was missing; point it at the rebuilt
         cloud backend before the section reads, then load the rows. The live swap
         keeps the document's socket up, so the reader stays connected. */
      const applied = await source().applyRuntime()
      await load()
      await refreshStats()
      /* The models section caches its EverOS Cloud card; the key saved here is
         the value that card shows, and it may already be open behind this. */
      settingsDialog.requestRefresh()
      set({
        cloudBusy: false, cloudRejected: false, cloudEditing: false,
        /* Queued is not done: a turn is running and the loop reaches the key's
           backend when it ends, which is the sentence the reply carries. */
        backendNotice: applied.ok && applied.state !== 'queued' ? '' : (applied.detail || t('gui.mem.reload_wait')),
      })
    } catch (e) {
      set({ cloudBusy: false, cloudErr: failure(e) })
    }
  })()
}

/* What arriving at this section of the settings dialog costs: the counters and
   the first page -- so a reader who picks the row in the dialog's own nav gets
   the same two reads the opener above does. Registered at this module's own
   evaluation rather than by the page's wiring, the same shape
   features/desk/store.ts fills state/escapeOrder.ts's slot with: the
   alternative is src/app/install.ts importing three island stores for three
   lines, which is three island graphs in the page's own wiring. */
function enter(): void {
  /* backendNotice is deliberately kept: it says the file and the running loop
     disagree, and re-entering the section changes neither. Only a successful
     apply clears it -- the switch, the key save and Start all report one. */
  set({ backendErr: '', cloudEditing: false, localErr: '' })
  /* The counters first: ``load`` reads the backend and the cloud key state off
     them to tell a refused key from an unreachable service. */
  void (async () => {
    await refreshStats()
    await load()
  })()
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
