import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

import { SubagentsApp } from './SubagentsPage'
import * as store from './store'

import type { Root } from 'react-dom/client'

/* The workspace's agents view has no page section of its own: drawWs wipes
 * #wsBody and dispatches on every repaint. So the island mounts per draw --
 * the workspace store unmounts it (detach) BEFORE the wipe, while the DOM
 * React owns is still intact, and mounts a fresh root after. State lives in
 * the store, so a remount costs nothing but the render.
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
  /* Synchronous like the renderer it replaces: drawWs's callers may touch
     the drawn DOM in the same task (the dag sheet selects its node next). */
  flushSync(() => root!.render(<SubagentsApp />))
  store.attached(true)
}
