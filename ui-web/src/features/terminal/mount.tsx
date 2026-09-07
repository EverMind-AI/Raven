/** Mount bridge for the permanent hosted terminal React island. */

import { createRoot } from 'react-dom/client'

import { TerminalApp } from './TerminalPage'
import * as store from './store'

import type { Root } from 'react-dom/client'

let root: Root | null = null

export function mount(host: HTMLElement): void {
  if (root) return
  root = createRoot(host)
  root.render(<TerminalApp />)
}

export function setTask(taskId: string | null): void {
  store.setTask(taskId)
}

export function detach(): void {
  if (!root) return
  root.unmount()
  root = null
  store.stop()
}
