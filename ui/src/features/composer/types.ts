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

/* DS.composer: what the dock reads of the page it sits in. The demo shell
 * registers the fixture half (ui/src/demo/090-composer.js) and the live layer
 * installs over the parts only it can answer -- the meter's wording and the
 * upload transport. `busy` reads the page global the turn machine owns, so it
 * is a closure, never a snapshot. The queue belongs to the composer store.
 */
export interface ComposerSource {
  busy(): boolean
  /* Whether the busy turn can be stopped by the reader. A USER turn can; a
     runtime turn (a delegated result re-entering) cannot -- turn.cancel only
     resolves handles turn.send registered, and claiming the stop would reset
     the UI while the delegated deltas still stream. The dock reads this to
     keep the stop action from appearing at all for such a turn. */
  cancellable(): boolean
  meter(): string
  slash: SlashCmd[]
  /* What the demo canvas says instead of opening a file picker it has no
     backend for. Absent in live mode, which installs `upload` instead. */
  pickHint?: string
  upload?(req: UploadReq): Promise<UploadRes>
  /* The two actions the go button is. Required, not optional like `upload`:
     both modes install them, because a composer that cannot send is not a
     composer. They were shell verbs until the page layer stopped owning the
     turn -- and as shell verbs they had the wrong direction, since `send`
     reached back INTO this island for the attachment tray. */
  send(text: string): void
  stop(): void
}
