/* The strangler seam between a migrated island and the legacy shell.
 *
 * The shell publishes late-bound closures on window.RavenShell (see
 * ui/src/demo/155-bridge.js): late-bound so the live layer's rebinds --
 * toast, most notably -- win over the demo definitions the bridge was
 * evaluated with. window.DS is the DataSource seam object itself, published
 * by ui/src/seam/000-datasource.js.
 *
 * Everything here throws loudly when the shell is absent: an island runs
 * inside the assembled page or inside a test that installed fakes, never
 * standalone, and a silent fallback would just move the failure downstream.
 */

export interface ToastAction {
  label: string
  fn: () => void
}

export interface MenuItem {
  label: string
  fn: () => void
  bad?: boolean
}

export interface Shell {
  T(key: string, vars?: Record<string, string | number>): string
  toast(text: string, action?: ToastAction): void
  menuAt(x: number, y: number, items: Array<MenuItem | '-'>): void
  confirmAsk(title: string, body: string, label: string, fn: () => void): void
  showPage(id: string | null): void
}

declare global {
  interface Window {
    RavenShell?: Shell
    DS?: Record<string, unknown>
    RavenIslands?: Record<string, unknown>
  }
}

export function shell(): Shell {
  const s = window.RavenShell
  if (!s) throw new Error('RavenShell is not published; the island is running outside the page')
  return s
}

export function t(key: string, vars?: Record<string, string | number>): string {
  return shell().T(key, vars)
}

export function ds<S>(domain: string): S {
  const seam = window.DS
  const source = seam && (seam[domain] as S | undefined)
  if (!source) throw new Error(`DS.${domain} is not installed`)
  return source
}
