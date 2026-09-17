import { t } from '../../i18n/t'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'

import type { ConnChannel, ConnSource } from './types'
import * as page from '../../state/page'
import { makeStore } from '../../state/store'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (nav opens it, Esc closes it and its dialog, a language flip
 * redraws it), so the state lives in a plain store the shims can call, and
 * the component subscribes.
 */

export interface ConnState {
  rows: ConnChannel[]
  /* False until the first rows fetch answers: the legacy live page cleared
     the stage on first open rather than showing a not-yet-loaded list. */
  loaded: boolean
  /* Which entry's credential dialog is up -- the old `connEdit`. */
  dialogId: string | null
  /* Remounts the dialog subtree when it is reopened, so its uncontrolled
     inputs start from the row's current values. */
  epoch: number
  /* Whether anything is running that could host an adapter (see
     ConnSource.hostRunning). Undefined until a source that answers has been
     asked. */
  host?: boolean
}

const store = makeStore<ConnState>({ rows: [], loaded: false, dialogId: null, epoch: 0 })

export const { get, subscribe, _resetForTests } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<ConnState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): ConnSource => ds('conn')

export async function refresh(initial = false): Promise<void> {
  try {
    const rows = await source().rows(initial)
    set({ rows, loaded: true, host: source().hostRunning?.() })
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: String((e as Error).message || e) }))
    set({ loaded: true })
  }
}

export function close(): void {
  page.show(null)
}

export function openDialog(c: ConnChannel): void {
  set({ dialogId: c.id, epoch: get().epoch + 1 })
}

export function closeDialog(): void {
  set({ dialogId: null })
}

/* Optimistic, like the accessor it replaces: both sources flip `c.on` before
   their first await, so the redraw right after already shows the new get();
   the rpc source reverts the flag and rejects handled on failure, and the
   second redraw takes the switch back. */
export function toggle(c: ConnChannel): void {
  const p = source().toggle(c, !c.on)
  redraw()
  void p.catch(() => redraw())
}

/* Credentials and the switch travel together; the source speaks its own
   failures, so this only has to repaint whatever get() the write left. */
export async function apply(c: ConnChannel, patch: Record<string, string>, enable: boolean): Promise<void> {
  try {
    await source().apply(c, patch, enable)
  } catch {
    /* the source already toasted */
  }
  redraw()
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
