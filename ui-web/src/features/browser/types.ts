export interface UrlRow {
  url: string
  kind: string
  at: string
}

export interface BrowserTabRow {
  index: number
  url: string
  title?: string
  active?: boolean
  loading?: boolean
}

/* The JSON header a screencast frame arrives with. */
export interface FrameHead {
  url?: string
  title?: string
  vw?: number
  vh?: number
  loading?: boolean
}

/* What browser.* calls answer. Every field is optional because the surface
   grew over several server generations, and absence means "no news". */
export interface BrowserReply {
  available?: boolean
  reason?: string
  error?: string
  started?: boolean
  headful?: boolean
  url?: string
  title?: string
  jpeg?: string
  loading?: boolean
  can_back?: boolean
  can_forward?: boolean
  watching?: boolean
  vw?: number
  vh?: number
  tabs?: BrowserTabRow[]
}

/* The DS.browser contract -- two shapes on one seam. The demo shell registers
   the links source (no Chromium behind it, so the island draws the fetched
   links list); the live layer installs the chromium source (browser.* over
   rpc, plus the pushed screencast frames through onFrame). */
export interface LinksSource {
  embedded: false
  urls(): UrlRow[]
  openUrl(u: string): void
}

export interface ChromiumSource {
  embedded: true
  urls(): UrlRow[]
  frame(p: { quality: number }): Promise<BrowserReply>
  open(p: { url?: string; action?: string }): Promise<BrowserReply>
  watch(p: { on: boolean; quality?: number; width?: number; height?: number }): Promise<BrowserReply>
  mode(p: { headful: boolean }): Promise<BrowserReply>
  close(): Promise<unknown>
  tabs(p: { action: string; index?: number }): Promise<BrowserReply>
  input(p: Record<string, unknown>): Promise<unknown>
  /* The island subscribes here; the live layer decodes each pushed frame
     (binary or legacy base64 notify) and forwards it through this hook. */
  onFrame: ((head: FrameHead, blob: Blob | null) => void) | null
}

export type BrowserSource = LinksSource | ChromiumSource
