/* The strangler seam between a migrated island and the legacy shell.
 *
 * The shell publishes late-bound closures on window.RavenShell (see
 * ui-web/src/demo/155-bridge.js): late-bound so the live layer's rebinds --
 * toast, most notably -- win over the demo definitions the bridge was
 * evaluated with. window.DS is the DataSource seam object itself, published
 * by ui-web/src/seam/000-datasource.js.
 *
 * Everything here throws loudly when the shell is absent: an island runs
 * inside the assembled page or inside a test that installed fakes, never
 * standalone, and a silent fallback would just move the failure downstream.
 */

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
  confirmAsk(title: string, body: string, label: string, fn: () => void): void
  showPage(id: string | null): void
  /* Grown by the islands. Optional, so fakes that predate a helper
     stay valid: each one is only reached from the island that asked. */
  useInTask?(promptKey: string, name: string): void
  closeDetail?(): void
  /* Optional verbs: each exists once an island needs it and the shell half
     (demo/155-bridge.js) publishes it. */
  showWorkspace?(tab: string): void
  workspaceSetOpen?(open: boolean): void
  wsShows?(tab: string): boolean
  wsView?(): WsPanelView
  wsPick?(tab: string): void
  /* Transcript island verbs: the attachment marker the history reader needs. */
  attNotes?(): string[]
  /* What markNew needs of the chrome's page registry: the NAV_OF keys and the
     button a page lights up. The More rows are not in here -- the nav flyout
     module marks its own (see shell/navfly.ts). */
  navState?(): { pages: string[]; btnOf(p: string): string | undefined }
  /* One verb for one action: the banner's only button opens the plugins page
     AND the websearch entry on it, and a reader who lands on the page without
     the entry open has to hunt for what the banner was talking about. */
  openWebsearch?(): void
  /* Re-decides the nav marks after a row navigates. The flyout opens its three
     pages by importing the owning islands (see shell/navfly.ts); this stays a
     verb because rail/store imports that module, so calling rail's markNew
     from it directly would close an import cycle. */
  markNew?(): void
  /* Redraws the capabilities page, but only while it is open on the plugin
     tab: a plugin write that lands with the page shut, or on another tab, has
     nothing to repaint. */
  plugRedraw?(): void
  /* Settings-island verbs. Each optional for the same reason, and each
     published by one guarded line in ui-web/src/demo/155-bridge.js. */
  openSet?(): void
  closeSet?(): void
  /* Whether the dialog is up. Closing it is legacy chrome flipping the veil,
     which unmounts nothing, so the island cannot answer this from its own
     state. */
  setIsOpen?(): boolean
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
