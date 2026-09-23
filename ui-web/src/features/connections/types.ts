/* One row per entry of the channel catalogue (./catalogue.ts). The source
   answers with those same objects, mutated in place, so a status that landed
   between two reads is on the row the reader is looking at. */
export interface ConnField {
  key: string
  label?: string
  secret?: boolean
  required?: boolean
  set?: boolean
}

export interface ConnChannel {
  id: string
  /* The message-catalogue entry that names the row, in both languages. */
  key: string
  on: boolean
  who?: string
  fields?: ConnField[]
  missing?: string[]
  /* Three separate facts, kept separate. `on` is what the config asks for;
     `running` is whether the adapter came up; `connected` is whether the
     account is paired, which only the QR channels report. Absent means the
     gateway could not be asked -- not "no". */
  running?: boolean | null
  connected?: boolean | null
  qrLogin?: boolean
}

/* One channels.qr answer. `connected: true` ends the island's polling. */
export interface ConnQr {
  qr?: string
  qr_text?: string
  connected: boolean
}

/* The DS.connections contract both the offline fixture library and the rpc
   source (live layer) implement. The island only ever talks to this.
   `rows(true)` is the page-open fetch: the rpc source reserves its
   gateway-not-running warning for that one call. `qr` resolving null means
   "nothing to show yet"; the island keeps polling while the dialog is up. */
export interface ConnectionsSource {
  rows(initial?: boolean): Promise<ConnChannel[]>
  /* Whether anything is running that could host a channel adapter at all --
     the gateway lock, read after the last `rows`. A page-level fact, not a
     per-row one: with no host there is no adapter to start, no code to mint and
     nothing to ask, which is the difference between "this entrance is not
     receiving" and "nothing here could be". Absent, or undefined, means the
     source cannot say, and the page then claims nothing. */
  hostRunning?(): boolean | undefined
  toggle(c: ConnChannel, on: boolean): Promise<unknown>
  /* Resolves true once the write was applied, false when it was refused or never
     reached the gateway (the source has already said why). */
  apply(c: ConnChannel, patch: Record<string, string>, enable: boolean): Promise<boolean>
  qr(c: ConnChannel): Promise<ConnQr | null>
}
