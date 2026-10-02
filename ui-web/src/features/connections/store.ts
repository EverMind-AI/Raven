import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as hub from '../../state/hub'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { ConnChannel, ConnectionsSource } from './types'

/* Page state, outside React on purpose: the callers that close the channel's
 * sheet are not React -- the shared drawer's own close button, its scrim,
 * Escape and a page switch all go through state/detail.ts -- so the state
 * lives in a plain store those can reach, and the component subscribes.
 */

export interface ConnState {
  rows: ConnChannel[]
  /* False until the first rows fetch answers: the list is not drawn at all
     until then, so a page still loading never reads as "no channels". */
  loaded: boolean
  /* Which entry the sheet is showing. */
  viewId: string | null
  /* Remounts the sheet's subtree when another entry is picked, so its
     uncontrolled inputs start from that row's current values. */
  epoch: number
  /* Whether anything is running that could host an adapter (see
     ConnectionsSource.hostRunning). Undefined until a source that answers has been
     asked. */
  host?: boolean
}

const store = makeStore<ConnState>({ rows: [], loaded: false, viewId: null, epoch: 0 })

export const { get, subscribe, _resetForTests } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<ConnState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): ConnectionsSource => ds('connections')

export async function refresh(initial = false): Promise<void> {
  try {
    const rows = await source().rows(initial)
    set({ rows, loaded: true, host: source().hostRunning?.() })
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: String((e as Error).message || e) }))
    set({ loaded: true })
  }
}

/* The page. Its rows are fetched again on every open, because the greeting's
   entry may have read them long before and a channel may have come up since. */
export function open(): void {
  page.show('connectionsPage')
  void refresh(true)
}
hub.onOpen('channels', open)

/** Where the sheet renders: the host the shared drawer keeps for this island. */
export function detailHost(): HTMLDivElement {
  return detail.host('connections')
}

export function openChannel(c: ConnChannel): void {
  detail.open('connections')
  set({ viewId: c.id, epoch: get().epoch + 1 })
}

export function closeChannel(): void {
  detail.close()
}

/* Whoever closed the drawer, the card goes -- a fade later, or what fades is
   an empty panel. `gen` says whether the reader opened another card inside
   that window, which the id alone cannot: reopening the same channel writes
   the same id back. */
function dismissed(): void {
  if (!get().viewId) return
  const gen = detail.get().gen
  detail.dropAfterFade(
    () => set({ viewId: null }),
    () => detail.get().gen !== gen,
  )
}
detail.onClose('connections', dismissed)

/* Credentials and the switch travel together; the source speaks its own
   failures, so this only has to repaint whatever get() the write left. */
export async function apply(c: ConnChannel, patch: Record<string, string>, enable: boolean): Promise<boolean> {
  let applied = false
  try {
    applied = await source().apply(c, patch, enable)
  } catch {
    /* the source already toasted */
  }
  redraw()
  return applied
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. `host` rides along
   because it is otherwise written only on section entry: a gateway that came up
   since then left the pane telling the reader nothing was running it. */
function redraw(): void {
  set({ host: source().hostRunning?.() })
}
