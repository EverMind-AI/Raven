import { ds, shell } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { ConnChannel, ConnSource } from './types'

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
}

let state: ConnState = { rows: [], loaded: false, dialogId: null, epoch: 0 }
const listeners = new Set<() => void>()

export const getState = (): ConnState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<ConnState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): ConnSource => ds<ConnSource>('conn')

export async function refresh(initial = false): Promise<void> {
  try {
    const rows = await source().rows(initial)
    set({ rows, loaded: true })
  } catch (e) {
    toast(`加载失败：${(e as Error).message || e}`)
    set({ loaded: true })
  }
}

export function open(): void {
  set({ dialogId: null })
  shell().showPage('connPage')
  void refresh(true)
}

export function close(): void {
  shell().showPage(null)
}

export function openDialog(c: ConnChannel): void {
  set({ dialogId: c.id, epoch: state.epoch + 1 })
}

export function closeDialog(): void {
  set({ dialogId: null })
}

/* Optimistic, like the accessor it replaces: both sources flip `c.on` before
   their first await, so the redraw right after already shows the new state;
   the rpc source reverts the flag and rejects handled on failure, and the
   second redraw takes the switch back. */
export function toggle(c: ConnChannel): void {
  const p = source().toggle(c, !c.on)
  redraw()
  void p.catch(() => redraw())
}

/* Credentials and the switch travel together; the source speaks its own
   failures, so this only has to repaint whatever state the write left. */
export async function apply(c: ConnChannel, patch: Record<string, string>, enable: boolean): Promise<void> {
  try {
    await source().apply(c, patch, enable)
  } catch {
    /* the source already toasted */
  }
  redraw()
}

/* A language flip changes nothing in this state, but every visible string
   comes from T(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}
