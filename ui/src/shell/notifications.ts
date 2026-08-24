/* The served front end's persisted desktop-notification preference and writer. */

const KEY = 'raven.gui.ntf'

const read = (): boolean => {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) || 'null')
    return !!(raw && typeof raw === 'object' && (raw as { on?: unknown }).on)
  } catch {
    return false
  }
}

let on = false

export function load(): void {
  on = read()
}

load()

export const enabled = (): boolean => on

export function setEnabled(value: boolean): void {
  on = value
  try {
    localStorage.setItem(KEY, JSON.stringify({ on }))
  } catch {
    /* Storage may be unavailable in private mode or over quota. */
  }
}

export function show(title: string, body?: string, opts?: { force?: boolean }): void {
  if (!on || !('Notification' in window) || Notification.permission !== 'granted') return
  if (!opts?.force && document.hasFocus()) return
  try {
    new Notification(title, body ? { body } : undefined)
  } catch {
    /* Some browser hosts expose Notification but cannot construct one. */
  }
}
