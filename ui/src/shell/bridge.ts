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
  sessionKey?(): string
  dur?(ms: number): string
  plainTitle?(s: string): string
  /* The transcript bridge: draws a delegated run's record into a stage box
     with the transcript's own renderer (live-only; the demo global it calls
     through stays null, and the island never asks without a record). */
  agentStagePaint?(box: HTMLElement, ctx: unknown, opts?: { key?: string; empty?: string; reset?: boolean }): void
  /* Transcript island verbs: the tail-follow, the attachment image bytes,
     the lightbox, the path opener, the diff builders the workspace panel
     already owns, and the turn's tl/raw logs the shell keeps per turn. */
  down?(): void
  attImage?(path: string): string | undefined
  openImage?(src: string, name: string): void
  attNotes?(): string[]
  pathOpen?(path: string): void
  hunkFromEdit?(oldText: string, newText: string): unknown
  hunkFromWrite?(content: string): unknown
  hunkFromUnified?(lines: string | string[]): unknown
  tlPush?(entry: { name: string; arg: string; ms: number; ok: boolean }): void
  rawPush?(line: string): void
  /* Composer island verbs. `send`/`halt` stay with the shell on purpose: they
     mutate the turn globals (busy, use, tl, raw) and diverge between the demo
     replay and the live rpc, so the dock asks for the action and the page
     decides what it means. The rest are the draft store, the tail anchor and
     the two catalogues the palette renders from. */
  send?(text: string): void
  halt?(): void
  noteRow?(label: string, detail: string): void
  stick?(): boolean
  setStick?(on: boolean): void
  /* The draft debounce, kept whole: the composer parks 250ms after a
     keystroke and drops the parked copy the moment the text is sent. */
  draftTouch?(): void
  draftDrop?(): void
  draftPark?(): void
  attImageSet?(path: string, url: string): void
  slashName?(id: string): string
  slashHelp?(id: string): string
  /* Rail island verbs, all optional for the same reason. They are late-bound
     closures over legacy names the live layer rebinds (openSession,
     removeSession, renameTitle) or wraps (drawList during the live boot),
     which is why the island calls back out instead of acting locally. */
  drawList?(): void
  setCur?(id: string | null): void
  openSession?(s: unknown): void
  removeSession?(s: unknown): void
  renameTitle?(): void
  dropDraft?(id: string): void
  pinPersist?(id: string, pinned: boolean): void
  openCron?(): void
  /* What markNew needs of the chrome's page registry: the NAV_OF keys and the
     button a page lights up. The More rows are not in here -- the nav flyout
     module marks its own (see shell/navfly.ts). */
  navState?(): { pages: string[]; btnOf(p: string): string | undefined }
  /* Chrome verbs. The nav flyout reaches the other two module pages through
     these, and re-decides the nav marks after a row navigates. */
  openXa?(): void
  openConn?(): void
  markNew?(): void
  /* Remembers a theme pick: the preference belongs to the legacy look store,
     which also persists it and repaints the appearance page. */
  themeSet?(next: 'dark' | 'light'): void
  /* What the foot row says: the running build, and how this platform spells
     the settings shortcut. */
  appVersion?(): string | null
  modKey?(): string
  isMac?(): boolean
  /* Republishes the offset the docked composer stands at; a panel drag moves
     the column the composer lives in. */
  dockLift?(): void
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
