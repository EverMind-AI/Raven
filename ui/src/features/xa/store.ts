import { ds, shell, t } from '../../shell/bridge'

import type { XaActArgs, XaOp, XaRow, XaSource } from './types'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (the More row opens it, Esc closes it, a language flip
 * redraws it), so the state lives in a plain store the shims can call,
 * and the component subscribes.
 */

export interface XaState {
  rows: XaRow[]
  /* The two flags only the page can answer: which sheet is unfolded, and
     whether a probe is in flight. Both are about what is drawn, so neither
     belongs to whichever source is answering. */
  sheet: string | null
  probing: boolean
  /* Bumped when the rows are replaced: the sheet's form is uncontrolled and
     mutated in place, so a fresh answer remounts it -- the same wholesale
     redraw the legacy xaSheetDraw performed after every mutation. */
  epoch: number
}

let state: XaState = { rows: [], sheet: null, probing: false, epoch: 0 }
const listeners = new Set<() => void>()

export const getState = (): XaState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<XaState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): XaSource => ds<XaSource>('xa')

/* What to show a reader when a call fails. The server's own sentence first: a
   rejected rpc frame carries `message` as the error's *code name*
   ("subagent_not_found") and the reason, when there is one, under `data.detail`.
   Reading `message` first therefore showed the code name and hid the sentence
   written to explain it. */
const failure = (e: unknown): string => {
  const err = e as { message?: string; detail?: string; data?: { detail?: string } } | null
  return (err && ((err.data && err.data.detail) || err.detail || err.message)) || String(e)
}

export function open(): void {
  shell().showPage('xaPage')
  void source()
    .load(true)
    .then((rows) => set({ rows, epoch: state.epoch + 1 }))
    /* Through `failure` like every other rejection here: the rpc client rejects
       with the error frame verbatim, and `String()` on that object is
       "[object Object]" -- not a hard-to-read reason but no reason at all. */
    .catch((e: unknown) => shell().toast(t('gui.agent.failed', { detail: failure(e) })))
}

export function close(): void {
  shell().showPage(null)
}

/* Every mutation goes through here, and the two long-running ones say so on
   screen before they hand over: a probe re-measures every entry and a test
   runs the agent for real, either of which can outlast a model turn, and a
   button that neither moves nor disables reads as a dead button. */
export async function run(op: XaOp | 'probe', row?: XaRow, args?: XaActArgs): Promise<void> {
  let rows = state.rows
  /* A rename moves the open sheet, but only once the rows that carry the new
     name are here: the sheet is resolved by looking the name up in rows, so
     moving it any earlier resolves to nothing, and the shared drawer -- which
     stays open, since only the lookup went missing -- would sit empty for the
     length of the write. Set on success only, or a rejected rename would point
     the sheet at a name no row will ever have and close the drawer. */
  let renamed = ''
  try {
    if (op === 'probe') {
      set({ probing: true })
      try {
        rows = await source().load(true)
      } finally {
        set({ probing: false })
      }
    } else {
      if (op === 'test' && row) {
        row.test_running = true
        set({})
      }
      try {
        rows = await source().act(op, row as XaRow, args || {})
        if (row && args?.new_name && args.new_name !== row.name) renamed = args.new_name
      } finally {
        if (op === 'test' && row) row.test_running = false
      }
    }
  } catch (e) {
    shell().toast(t('gui.agent.failed', { detail: failure(e) }))
  }
  const landed: Partial<XaState> = { rows, epoch: state.epoch + 1 }
  if (renamed && state.sheet === row?.name) landed.sheet = renamed
  set(landed)
  if (state.sheet && !rows.some((x) => x.name === state.sheet)) closeSheet()
  watchBuilds(rows)
}

/* A build is the one action whose call returns before the work does -- it is a
   few hundred MB of downloads, so `subagents.build` hands back as soon as it is
   under way. Nothing pushes when it finishes, so the row would sit on
   "Installing..." until the reader happened to reload. Re-listing while any row
   carries `building` is what turns it back into "ready" on its own.

   Deliberately dumb about lifetime: one timer at a time, re-armed from the
   result it just read, and never armed when nothing is building. It stops
   because the flag stops, not because anything remembers to cancel it. */
let buildTimer: ReturnType<typeof setTimeout> | null = null
const BUILD_POLL_MS = 4000

function watchBuilds(rows: XaRow[]): void {
  if (buildTimer !== null) return
  if (!rows.some((r) => r.building)) return
  buildTimer = setTimeout(() => {
    buildTimer = null
    /* Unprobed while a build is in flight -- a probe per poll would re-measure
       every other entry for nothing. Probed once on the poll that finds the last
       build gone, because a cli row's status is what the card renders: without it
       a folder that just finished building reads "unverified", which is not what
       the reader watched happen. */
    void source()
      .load(false)
      .then(async (next) => {
        const done = !next.some((r) => r.building)
        const rows = done ? await source().load(true) : next
        set({ rows, epoch: state.epoch + 1 })
        watchBuilds(rows)
      })
      .catch(() => {
        /* A failed poll is not worth a toast: the build is still running and the
           next action or reload will show where it got to. */
      })
  }, BUILD_POLL_MS)
}

/* ── the shared detail drawer ────────────────────────────────────── */

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

export function sheetOpen(row: XaRow): void {
  const body = document.getElementById('dBody')
  if (body && !body.contains(detailHost())) {
    body.innerHTML = ''
    body.appendChild(detailHost())
  }
  set({ sheet: row.name, epoch: state.epoch + 1 })
}

export function closeSheet(): void {
  shell().closeDetail?.()
  if (state.sheet) set({ sheet: null })
}

/* Called when legacy chrome closed the drawer itself (Esc, the close
   button, a click outside): only the island state has to follow. */
export function sheetDismissed(): void {
  if (state.sheet) set({ sheet: null })
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
