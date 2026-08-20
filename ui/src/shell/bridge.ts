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

/* What the workspace panel's chrome (still legacy: the tab bar, the badge,
   the open/close buttons) currently shows. */
export interface WsPanelView {
  tab: string
  open: boolean
  picked: boolean
}

export interface Shell {
  /* `fallback` mirrors the legacy T(): what to show when the catalogue has no
     entry for the key (the connections form labels schema-declared fields). */
  T(key: string, vars?: Record<string, string | number>, fallback?: string): string
  toast(text: string, action?: ToastAction): void
  menuAt(x: number, y: number, items: Array<MenuItem | '-'>): void
  confirmAsk(title: string, body: string, label: string, fn: () => void): void
  showPage(id: string | null): void
  /* Optional so fakes written before it keep type-checking; the page
     bridge always publishes it. */
  /* Grown by the skills island. Optional, so fakes that predate a helper
     stay valid: each one is only reached from the island that asked. */
  useInTask?(promptKey: string, name: string): void
  reachText?(reach: string): string
  closeDetail?(): void
  /* Optional verbs: each exists once an island needs it and the shell half
     (demo/155-bridge.js) publishes it. */
  copyToClip?(text: string, done: string): void
  showWorkspace?(tab: string): void
  wsShows?(tab: string): boolean
  lang?(): string
  hostPlatform?(): string
  wsView?(): WsPanelView
  wsState?(): unknown
  wsPick?(tab: string): void
  drawWsAgents?(box: HTMLElement): void
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

export function t(key: string, vars?: Record<string, string | number>, fallback?: string): string {
  return shell().T(key, vars, fallback)
}

export function ds<S>(domain: string): S {
  const seam = window.DS
  const source = seam && (seam[domain] as S | undefined)
  if (!source) throw new Error(`DS.${domain} is not installed`)
  return source
}
