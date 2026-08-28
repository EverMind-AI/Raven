/* The failure bars appended over the page when boot or authentication stops. */

import { t } from './bridge'

export interface FailureBar {
  say(text: string): void
}

export function show(text: string): FailureBar {
  const bar = document.createElement('div')
  bar.className = 'topfail'
  bar.textContent = text
  document.body.appendChild(bar)
  return {
    say(next: string): void {
      bar.textContent = next
    },
  }
}

export function bootError(where: string, error: unknown): void {
  const detail = error as { message?: unknown; stack?: unknown } | null
  const at = String(detail?.stack || '').split('\n')[1] || ''
  const message = detail?.message || error
  const bar = document.createElement('div')
  bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99;background:#d96a5b;color:#fff;'
    + 'font:12px/1.5 ui-monospace,monospace;padding:8px 14px;white-space:pre-wrap'
  bar.textContent = `${t('gui.boot_fail', { where, err: String(message) })}\n${at.trim()}`
  document.body.appendChild(bar)
  if (window.console) console.error('[boot]', where, error)
}
