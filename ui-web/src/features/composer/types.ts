/* The composer island's vocabulary: the dock at the bottom of the chat --
 * the field, the send/stop button, the queued rows, the attachment tray, the
 * slash palette -- plus the one row it paints outside the dock, the live turn
 * row that rides the tail of the transcript.
 */

/* A staged file. `path` and `size` are the server's answer once the upload
   lands; `url` is the data URL kept for display only, and only for an image. */
export interface Attachment {
  name: string
  size: number
  uploading: boolean
  path: string | null
  url: string | null
}

/* One palette command. `id` is the catalogue key both spellings render from,
   never a spelling itself. */
export interface SlashCmd {
  id: string
  fn: () => void
  when?: () => boolean
}

export interface UploadReq {
  name: string
  content_b64: string
}

export interface UploadRes {
  path: string
  size: number
}

/* One bundled deck template as the server lists it. `cover` is the first page
   as a data URL, and null (or absent) on a host that cannot render one. */
export interface TemplateRow {
  name: string
  label: string
  size: number
  cover?: string | null
}

/* The template picker's two calls. Picking deposits the template under
   uploads and answers as an upload does, so the chip it stages is an ordinary
   attachment from there on. */
export interface TemplatesApi {
  /* `pending` says a cover is still being drawn on the server; the sheet asks
     again while it is open until every cover has landed. */
  list(): Promise<{ templates: TemplateRow[]; available: boolean; pending?: boolean }>
  pick(name: string): Promise<UploadRes>
  /* Every page of one template as data URLs, for the reader to flip through
     before picking; empty where the server cannot render. */
  pages(name: string): Promise<{ pages: string[] }>
}

/* The composer source: what the dock reads of the page it sits in. The boot's
 * own wiring installs it (app/install.ts) and the settings chrome adds the
 * one member only it can answer -- `beforeSend`. Turn phase and queue state
 * belong to the composer store.
 */
export interface ComposerSource {
  meter(): string
  slash: SlashCmd[]
  slashName(id: string): string
  slashHelp(id: string): string
  /* What the demo canvas says instead of opening a file picker it has no
     backend for. Absent in live mode, which installs `upload` instead. */
  pickHint?: string
  upload?(req: UploadReq): Promise<UploadRes>
  /* The deck template picker. Optional like `upload`: the demo canvas has no
     server to list them from, and the button stays hidden without it. */
  templates?: TemplatesApi
  beforeSend?(): boolean
  /* The two actions the go button is. Required, not optional like `upload`:
     both modes install them, because a composer that cannot send is not a
     composer. They were shell verbs until the page layer stopped owning the
     turn -- and as shell verbs they had the wrong direction, since `send`
     reached back INTO this island for the attachment tray. */
  send(text: string): void
  stop(): void
  /* A conversation to work in, made if the page is still on a draft, and the
     open one otherwise. The composer creates one on its first send and this is
     that same promotion by name -- for a caller that needs the conversation and
     has no message to start it with (the sub-agent roster's new-instance
     button). Optional like `upload`: the demo canvas has no server to mint one,
     and nothing there offers the actions that would ask. */
  startConversation?(): Promise<string>
}
