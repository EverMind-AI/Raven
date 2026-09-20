import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

import * as store from './store'
import { SubagentsApp } from './SubagentsPage'

import type { Root } from 'react-dom/client'

/* The workspace's agents view has no page section of its own: the pane's own
 * mount wipes #wsBody and dispatches on every repaint
 * (features/workspace/store.ts). So the island mounts per repaint -- that
 * mount unmounts this one (detach) BEFORE the wipe, while the DOM React owns
 * is still intact, and makes a fresh root after. State lives in the store, so
 * a remount costs nothing but the render.
 */

let root: Root | null = null

export function detach(): void {
  store.attached(false)
  if (!root) return
  const r = root
  root = null
  r.unmount()
}

export function draw(box: HTMLElement): void {
  detach()
  store.hook()
  root = createRoot(box)
  /* Synchronous, because the pane's mount and its callers may touch the drawn
     DOM in the same task (a graph's own click selects its node next). */
  flushSync(() => root!.render(<SubagentsApp />))
  store.attached(true)
}
