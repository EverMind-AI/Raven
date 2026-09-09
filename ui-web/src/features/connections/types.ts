/* One row per entry of the channel catalogue (ui-web/src/demo/030-fixtures.js).
   Both sources answer with those same objects, mutated in place: the fixture
   so demo edits stick across a redraw, the rpc source so the merged status
   lands on the rows the list is already drawn from. */
export interface ConnField {
  key: string
  label?: string
  secret?: boolean
  required?: boolean
  set?: boolean
}

export interface ConnChannel {
  id: string
  /* The catalogue's two spellings: `key` names a message-catalogue entry for
     the generic channels, `name` is a brand name used verbatim. */
  key?: string
  name?: string
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

/* The DS.conn contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this.
   `rows(true)` is the page-open fetch: the rpc source reserves its
   gateway-not-running warning for that one call. `qr` resolving null means
   "nothing to show yet"; the island keeps polling while the dialog is up. */
export interface ConnSource {
  rows(initial?: boolean): Promise<ConnChannel[]>
  /* Whether anything is running that could host a channel adapter at all --
     the gateway lock, read after the last `rows`. A page-level fact, not a
     per-row one: with no host there is no adapter to start, no code to mint and
     nothing to ask, which is the difference between "this entrance is not
     receiving" and "nothing here could be". Absent, or undefined, means the
     source cannot say, and the page then claims nothing. */
  hostRunning?(): boolean | undefined
  toggle(c: ConnChannel, on: boolean): Promise<unknown>
  apply(c: ConnChannel, patch: Record<string, string>, enable: boolean): Promise<unknown>
  qr(c: ConnChannel): Promise<ConnQr | null>
}
