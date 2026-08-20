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

const failure = (e: unknown): string => {
  const err = e as { message?: string; detail?: string } | null
  return (err && (err.message || err.detail)) || String(e)
}

export function open(): void {
  shell().showPage('xaPage')
  void source()
    .load(true)
    .then((rows) => set({ rows, epoch: state.epoch + 1 }))
    .catch((e: unknown) => shell().toast(t('gui.agent.failed', { detail: String(e) })))
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
