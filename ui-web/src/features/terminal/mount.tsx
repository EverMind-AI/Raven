/** Mount bridge for the permanent hosted terminal React island. */

import { createRoot } from 'react-dom/client'

import { TerminalApp } from './TerminalPage'
import * as store from './store'

import type { Root } from 'react-dom/client'

let root: Root | null = null
let frame: number | null = null
let pendingHost: HTMLElement | null = null

function mountWhenReady(): void {
  frame = null
  if (root || !pendingHost) return
  if (!window.RavenShell) {
    frame = requestAnimationFrame(mountWhenReady)
    return
  }
  root = createRoot(pendingHost)
  pendingHost = null
  root.render(<TerminalApp />)
}

export function mount(host: HTMLElement): void {
  if (root || pendingHost) return
  pendingHost = host
  frame = requestAnimationFrame(mountWhenReady)
}

export function setTask(taskId: string | null): void {
  store.setTask(taskId)
}

export function detach(): void {
  if (frame !== null) cancelAnimationFrame(frame)
  frame = null
  pendingHost = null
  if (!root) return
  root.unmount()
  root = null
  store.stop()
}
