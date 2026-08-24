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

/* What the appearance page reads and writes: per-front-end look plus the
   shared config.language. */
export interface LookState {
  theme: string
  codeFont: string
  motion: string
  lang: string
}

export interface Shell {
  /* `fallback` mirrors the legacy T(): what to show when the catalogue has no
     entry for the key (the connections form labels schema-declared fields). */
  T(key: string, vars?: Record<string, string | number>, fallback?: string): string
  toast(text: string, action?: ToastAction): void
  menuAt(x: number, y: number, items: Array<MenuItem | '-'>): void
  confirmAsk(title: string, body: string, label: string, fn: () => void): void
  showPage(id: string | null): void
  /* Grown by the islands. Optional, so fakes that predate a helper
     stay valid: each one is only reached from the island that asked. */
  useInTask?(promptKey: string, name: string): void
  reachText?(reach: string): string
  reachHint?(reach: string): string
  closeDetail?(): void
  /* Optional verbs: each exists once an island needs it and the shell half
     (demo/155-bridge.js) publishes it. */
  showWorkspace?(tab: string): void
  wsShows?(tab: string): boolean
  wsView?(): WsPanelView
  wsPick?(tab: string): void
  /* Transcript island verbs: the attachment marker the history reader needs. */
  attNotes?(): string[]
  /* The draft store, the tail anchor and the two catalogues the palette renders
     from. `send`/`halt` used to be here, and their leaving is the point of the
     change that moved them: as shell verbs the page decided what sending meant
     AND reached back into this island for the attachment tray. They are
     DS.composer.send / .stop now, so the island builds the message and the
     source decides what happens to it. */
  slashName?(id: string): string
  slashHelp?(id: string): string
  /* Rail island verbs that still belong to page chrome. */
  openCron?(): void
  /* What markNew needs of the chrome's page registry: the NAV_OF keys and the
     button a page lights up. The More rows are not in here -- the nav flyout
     module marks its own (see shell/navfly.ts). */
  navState?(): { pages: string[]; btnOf(p: string): string | undefined }
  /* Chrome verbs. The nav flyout reaches the other two module pages through
     these, and re-decides the nav marks after a row navigates. */
  /* One verb for one action: the banner's only button opens the plugins page
     AND the websearch entry on it, and a reader who lands on the page without
     the entry open has to hunt for what the banner was talking about. */
  openWebsearch?(): void
  openXa?(): void
  openConn?(): void
  markNew?(): void
  /* Remembers a theme pick: the preference belongs to the legacy look store,
     which also persists it and repaints the appearance page. */
  themeSet?(next: 'dark' | 'light'): void
  /* Republishes the offset the docked composer stands at; a panel drag moves
     the column the composer lives in. */
  plugRedraw?(): void
  /* Settings-island verbs. Each optional for the same reason, and each
     published by one guarded line in ui/src/demo/155-bridge.js. */
  openSet?(): void
  closeSet?(): void
  /* Whether the dialog is up. Closing it is legacy chrome flipping the veil,
     which unmounts nothing, so the island cannot answer this from its own
     state. */
  setIsOpen?(): boolean
  look?: { get(): LookState; set(patch: Partial<LookState>): void }
  ntf?: { get(): boolean; set(on: boolean): void; push(title: string): void }
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

/* One optional verb, insisted on. Every verb above is declared optional because
   the demo and live layers publish different subsets, but a caller that cannot
   do its job without one should say so rather than degrade: asking for
   `shell().wsView?.()` moves missing panel-state wiring into a later property
   read, and the fake most likely to omit the verb is a test's.
   Lives here rather than beside its first caller because the module docstring
   above is where this repo states the rule -- a silent fallback moves the
   failure downstream -- and a helper enforcing it belongs with the rule. */
export function verb<K extends keyof Shell>(name: K): NonNullable<Shell[K]> {
  const v = shell()[name]
  if (!v) throw new Error(`RavenShell.${String(name)} is not wired`)
  return v as NonNullable<Shell[K]>
}
