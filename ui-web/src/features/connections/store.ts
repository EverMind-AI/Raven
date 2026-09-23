import { t } from '../../i18n/t'
import * as settingsDialog from '../../state/settings'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { ConnChannel, ConnectionsSource } from './types'

/* Section state, outside React on purpose: two of the callers that drive this
 * section are not React. The Escape order closes its credentials dialog
 * (state/escapeOrder.ts) and the module page's own leave slot shuts that
 * dialog behind the reader (app/install.ts fills state/page.ts's slot) -- so
 * the state lives in a plain store those two can call, and the component
 * subscribes.
 */

export interface ConnState {
  rows: ConnChannel[]
  /* False until the first rows fetch answers: the list is not drawn at all
     until then, so a page still loading never reads as "no channels". */
  loaded: boolean
  /* Which entry the column beside the list is showing -- the old `connEdit`,
     and before that the id of a modal card. */
  viewId: string | null
  /* Remounts the pane's subtree when another entry is picked, so its
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

export function openChannel(c: ConnChannel): void {
  set({ viewId: c.id, epoch: get().epoch + 1 })
}

export function closeChannel(): void {
  set({ viewId: null })
}

/* What arriving at this section of the settings dialog costs: the rows, and the
   credentials pane a previous visit was left on. Registered at this module's
   own evaluation rather than by the page's wiring, the same shape
   features/desk/store.ts fills state/escapeOrder.ts's slot with: the alternative
   is src/app/install.ts importing three island stores for three lines, which is
   three island graphs in the page's own wiring. */
function enter(): void {
  closeChannel()
  void refresh(true)
}
settingsDialog.onEnter('channels', enter)
/* And what leaving it costs: a pane left open would come back over whatever
   section the reader opens next, still showing the channel they had left. */
settingsDialog.onLeave('clearConnChannel', closeChannel)

/* Optimistic, like the accessor it replaces: both sources flip `c.on` before
   their first await, so the redraw right after already shows the new get();
   the rpc source reverts the flag and rejects handled on failure, and the
   second redraw takes the switch back. */
export function toggle(c: ConnChannel): void {
  const p = source().toggle(c, !c.on)
  redraw()
  /* Both ways: the write's own answer is what the row is drawn from once the
     source has read the status back, so the paint the press earns is not the
     last one. */
  void p.then(() => redraw(), () => redraw())
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
   comes from t(), so a re-render is the whole redraw. `host` rides along
   because it is otherwise written only on section entry: a gateway that came up
   since then left the pane telling the reader nothing was running it. */
function redraw(): void {
  set({ host: source().hostRunning?.() })
}
