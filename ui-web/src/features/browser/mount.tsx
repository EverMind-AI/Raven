import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

import { BrowserApp } from './BrowserPage'
import * as store from './store'

import type { Root } from 'react-dom/client'

/* The workspace's browser view has no page section of its own: drawWs wipes
 * #wsBody and dispatches on every repaint. So the island mounts per draw --
 * the demo wrapper unmounts it (detach) BEFORE the wipe, while the DOM React
 * owns is still intact, and the draw shim mounts a fresh root after. State
 * lives in the store, so a remount costs nothing but the render.
 */

let root: Root | null = null

export function detach(): void {
  if (!root) return
  const r = root
  root = null
  r.unmount()
}

export function draw(box: HTMLElement): void {
  detach()
  store.setHost(box)
  store.hook()
  root = createRoot(box)
  /* Synchronous like the renderer it replaces: drawWs's callers may touch
     the drawn DOM in the same task. */
  flushSync(() => root!.render(<BrowserApp />))
}

export function hidden(): void {
  store.hidden()
}
