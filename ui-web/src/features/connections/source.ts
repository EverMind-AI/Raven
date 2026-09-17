/* -- connections (channels): the rpc source ---------------------------
   The connections island (ui-web/src/features/connections/) owns the drawing;
   this module only speaks channels.* over /rpc and merges the answer onto the
   catalogue's rows. The live layer installs it onto the seam, which replaces
   the fixture source before the first paint. */

import type { ConnSource } from './types'

import { servesChannels } from '../../rpc/capabilities'
import { t } from '../../i18n/t'
import { show as toast } from '../../state/toast'
import { gateway } from '../../rpc/gateway'

import { CHANNELS, chanName } from './catalogue'

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

export const connSource: ConnSource = {
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
      .then(() => toast(t('gui.conn.toggled', { name: chanName(c), state: t(on ? 'gui.conn.enabled' : 'gui.conn.disabled') })))
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
      await gateway().call('channels.configure', { name: c.id, fields, enabled: !!enable })
      if (Object.keys(fields).length) toast(t('gui.conn.saved_x', { name: chanName(c) }))
      await loadChannels()
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
