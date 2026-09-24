/* The failure bars held over the page when boot or authentication stops.
 *
 * State rather than a writer: both bars are nodes at the body (drawn by
 * src/chrome/FailureBar.tsx), and neither is ever taken down -- a page that has
 * stopped has nothing to put in their place.
 *
 * Two kinds in one list, in the order they were raised: the top bar, whose
 * handle keeps rewriting the same line as the watcher learns more, and the boot
 * bar, which is what a step of the boot throws into. They share a list because
 * they share the body, and a page that raises one of each has to show them in
 * the order the page raised them.
 */

import { t } from '../i18n/t'
import { makeStore } from './store'

export interface FailureBar {
  say(text: string): void
}

export interface Bar {
  readonly id: number
  /** `top` is the standing failure bar; `boot` is a step that threw. */
  readonly kind: 'top' | 'boot'
  readonly text: string
}

/* The boot bar predates the stylesheet's bars and is styled inline on purpose:
   it is what the reader gets when the page is too broken to be trusted with a
   class, and its z-index is the literal the ladder's top step also holds. */
export const BOOT_CSS = 'position:fixed;left:0;right:0;top:0;z-index:99;background:#d96a5b;color:#fff;'
  + 'font:12px/1.5 ui-monospace,monospace;padding:8px 14px;white-space:pre-wrap'

const store = makeStore<readonly Bar[]>([])

/** The bars on screen, and the verb that raises one. */
export const { get, set, subscribe } = store

let seq = 0

export function show(text: string): FailureBar {
  const id = ++seq
  set([...get(), { id, kind: 'top', text }])
  return {
    say(next: string): void {
      set(get().map((bar) => (bar.id === id ? { ...bar, text: next } : bar)))
    },
  }
}

export function bootError(where: string, error: unknown): void {
  const detail = error as { message?: unknown; stack?: unknown } | null
  const at = String(detail?.stack || '').split('\n')[1] || ''
  const message = detail?.message || error
  set([...get(), {
    id: ++seq,
    kind: 'boot',
    text: `${t('gui.boot_fail', { where, err: String(message) })}\n${at.trim()}`,
  }])
  if (window.console) console.error('[boot]', where, error)
}

/** Test seam only: the bars are never taken down in the page. */
export function _resetForTests(): void {
  set([])
}
