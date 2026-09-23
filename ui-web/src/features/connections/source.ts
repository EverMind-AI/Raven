/* -- connections (channels): the rpc source ---------------------------
   The connections island (ui-web/src/features/connections/) owns the drawing;
   this module only speaks channels.* over /rpc and merges the answer onto the
   catalogue's rows. The live layer installs it onto the seam, which replaces
   the fixture source before the first paint. */

import { t } from '../../i18n/t'
import { servesChannels } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../../state/toast'
import { CHANNELS, chanName } from './catalogue'

import type { ChannelsConfigureResult } from '../../rpc/generated'
import type { ConnChannel, ConnectionsSource } from './types'

/* Merged onto the catalogue's own objects rather than into fresh ones: they
   are what `rows()` has always answered with and what the island is already
   drawn from, so a status that landed between two reads is on the row the
   reader is looking at. */
export async function loadChannels(): Promise<void> {
  const r = await gateway().call('channels.status', {})
  const byName = Object.fromEntries(r.channels.map((c) => [c.name, c]))
  CHANNELS.forEach((c) => {
    const s = byName[c.id]
    if (!s) return
    /* No prose state line: the LED and the switch say on/off, and a missing
       credential says "not configured" through c.missing. `who` is reserved for a real
       identity (the account the channel signs in as), which no backend
       supplies yet -- so live rows keep their sub line empty. */
    c.who = ''
    /* The schema-declared field list rides the status row; the page's
       configure form is drawn from it, so the form and the config can't drift. */
    c.fields = s.fields || []
    c.missing = s.missing || []
    /* Three separate facts, kept separate. `on` is what the config asks for;
       `running` is whether the adapter came up; `connected` is whether the
       account is paired, which only the QR channels report. Absent means the
       gateway could not be asked -- not "no". */
    c.on = s.enabled
    c.running = s.running
    c.connected = s.connected
    c.qrLogin = !!s.qr_login
  })
  gatewayRunningLive = r.gateway_running
}

let gatewayRunningLive = false

/* The refusals a reader can do something about, in their own words; the words
   keyed here are the channel manager's (raven/gateway/manager.py). Any other
   refusal keeps the gateway's word verbatim, which is what a bug report needs
   and what a new one will read as until it earns a sentence. */
const OUTCOME_SAY: Record<string, string> = {
  missing_dep: 'gui.conn.out_missing_dep',
  bad_config: 'gui.conn.out_bad_config',
  deny_all: 'gui.conn.out_deny_all',
}

/* Why the adapter is not up, for a write that asked for it and did not get it
   -- null when there is nothing of the kind to report. The reason was thrown
   away here, so a channel whose SDK is missing went red on the page while the
   sentence that fixes it went to the gateway log nobody has open. */
function refusalOf(on: boolean, r: ChannelsConfigureResult): string | null {
  const outcome = r.outcome
  /* Switching off is done the moment the config says so, whatever the gateway
     had in its table; and an outcome nobody answered is the next-launch case
     below, not a refusal. */
  if (!on || !outcome || outcome === 'unreachable' || outcome === 'started' || outcome === 'already') return null
  const why = OUTCOME_SAY[outcome]
  const said = why ? t(why) : t('gui.conn.out_refused', { outcome })
  return r.detail ? `${said} ${r.detail}` : said
}

/* What the switch actually did, which the toast used to guess at: it told every
   reader to reopen the app, including the one whose channel was already
   running by the time they read it. Only a write nobody applied is left for the
   next launch, and only that one keeps the old sentence. */
function sayOutcome(c: ConnChannel, on: boolean, r: ChannelsConfigureResult): void {
  const name = chanName(c)
  const refused = refusalOf(on, r)
  if (refused) {
    toast(t('gui.conn.toggle_failed', { name, detail: refused }))
    return
  }
  const state = t(on ? 'gui.conn.enabled' : 'gui.conn.disabled')
  const applied = !!r.outcome && r.outcome !== 'unreachable'
  toast(t(applied ? 'gui.conn.toggled_now' : 'gui.conn.toggled', { name, state }))
}

export const connSource: ConnectionsSource = {
  /* `initial` is the page-open fetch: only that one toasts a failed load or
     warns about a gateway that is not receiving -- a background reload (the
     scan poll's refresh) stays silent, as the old page did. */
  rows: async (initial) => {
    try {
      await loadChannels()
    } catch (e) {
      if (initial) toast(t('gui.op.load_failed', { detail: String((e as Error).message || e) }))
    }
    if (initial && !gatewayRunningLive && CHANNELS.some((c) => c.on)) {
      toast(t('gui.conn.not_receiving'))
    }
    return CHANNELS
  },
  /* Read off the same status call, which carries the gateway lock's answer.
     The page needs it to tell "this entrance is not receiving" from "nothing
     here could be": with no host, pressing connect starts no adapter and mints
     no code, and the card should say so before the press rather than after. */
  hostRunning: () => gatewayRunningLive,
  /* The write the old code hid behind an Object.defineProperty accessor on
     `c.on`: optimistic flip, then the setting, then the toast -- and on
     failure the flip is taken back and the rejection marked handled so the
     island redraws without toasting a second time.

     Through channels.configure, not settings.set on the raw key. It validates
     the name against the channel's own schema, and it is the one writer the
     adapter's start and stop hang off server-side, so both verbs take the same
     path. Writing the flag straight into config left them lopsided: whatever
     the connect path did, disconnect only ever wrote `false`. */
  toggle: (c, on) => {
    c.on = on
    return gateway().call('channels.configure', { name: c.id, fields: {}, enabled: on })
      .then(async (r) => {
        /* The write is hot-applied, so what the row says about it is a fact the
           gateway already has: reading it back is the difference between the
           row the reader is looking at and whatever the last section entry
           happened to see. Its own failure stays quiet and stays out of the
           catch below -- the write landed, and taking the switch back over a
           status read would report the opposite of what happened. */
        await loadChannels().catch(() => {})
        sayOutcome(c, on, r)
      })
      .catch((e) => {
        c.on = !on
        toast(t('gui.op.save_failed', { detail: e.message || e }))
        throw { handled: true }
      })
  },
  /* Credentials and the switch travel together, and the server applies them in
     that order, so a channel is never on without the values it was turned on
     for. */
  apply: async (c, patch, enable) => {
    try {
      const fields = patch && Object.keys(patch).length ? patch : {}
      const r = await gateway().call('channels.configure', { name: c.id, fields, enabled: !!enable })
      if (Object.keys(fields).length) toast(t('gui.conn.saved_x', { name: chanName(c) }))
      await loadChannels()
      /* Saved and not started are two different things, and this path said only
         the first: the pane's own state line then had to carry a refusal it has
         no words for. */
      const refused = refusalOf(!!enable, r)
      if (refused) toast(t('gui.conn.toggle_failed', { name: chanName(c), detail: refused }))
    } catch (e) {
      const err = e as { data?: { detail?: string }; message?: string }
      toast(t('gui.op.save_failed', { detail: (err.data && err.data.detail) || err.message || String(e) }))
    }
  },
  /* One scan-code read; the island polls this while the dialog is open. Null
     when the gateway does not speak channels.*, which the island shows as the
     same waiting frame the old panel kept. */
  qr: (c) => (servesChannels() ? gateway().call('channels.qr', { name: c.id }) : Promise.resolve(null)),
}

/* Test seam only: what the gateway last said about a running host is the
   module's, so it outlives a case. */
export function _resetForTests(): void {
  gatewayRunningLive = false
}
