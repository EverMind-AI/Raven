/* Which entrances are in service, and what each one asks for.
 *
 * The field lists are the real schemas' -- keys and labels both -- because
 * what an entrance costs to get into is derived from them: no required field at
 * all means the only way in is signing in by phone, and the page groups and
 * orders the catalogue by that. A fixture without them read every brand as a
 * scan-login entrance. The rows themselves are the catalogue's
 * (features/connections/catalogue.ts), which production reads too; only which
 * two are in service is fixture.
 */

import { CHANNELS } from '../../features/connections/catalogue'

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ChannelField, ResultOf } from '../generated'

const creds = (...pairs: Array<[string, string, boolean?]>): ChannelField[] =>
  pairs.map(([key, label, secret]) => ({ key, label, required: true, secret: !!secret, set: false }))

const CH_FIELDS: Record<string, ChannelField[]> = {
  feishu:   creds(['app_id','应用 App ID'], ['app_secret','应用 App Secret', true]),
  wecom:    creds(['corp_id','企业 CorpID'], ['secret','应用 Secret', true]),
  slack:    creds(['bot_token','机器人 Bot Token', true], ['app_token','应用 App Token', true]),
  dingtalk: creds(['client_id','应用 ClientID'], ['client_secret','应用 ClientSecret', true]),
  qq:       creds(['app_id','App ID'], ['app_secret','App Secret', true]),
  telegram: creds(['token','机器人 Token', true]),
  discord:  creds(['bot_token','机器人 Bot Token', true]),
  matrix:   creds(['homeserver','主服务器地址'], ['access_token','访问令牌', true]),
  mochat:   creds(['claw_token','Claw Token', true]),
  email:    creds(['imap_host','收件服务器（IMAP 地址）'], ['imap_username','邮箱账号'],
                  ['imap_password','邮箱密码或授权码', true], ['smtp_host','发件服务器（SMTP 地址）'],
                  ['smtp_username','发件账号'], ['smtp_password','发件密码或授权码', true]),
  /* Two entrances sign in instead: no form, a code on the phone. */
  weixin:   [],
  whatsapp: []
};

/* Which two entrances the demo has in service. The rest of a channel row --
   its id, its name and whether it signs in by scanning -- is the catalogue's. */
const IN_SERVICE = ['feishu', 'email']

export interface ChannelsFixture {
  fixtures: Fixtures
}

export function createChannels(_env: FixtureEnv): ChannelsFixture {
  /* Which entrance is switched on, held here: a toggle and a configure both
     change what the next `channels.status` answers, which is what makes the
     offline drawer the drawer. */
  const on = new Set(IN_SERVICE)
  const filled = new Set(IN_SERVICE)

  const row = (id: string, qrLogin: boolean): ResultOf<'channels.status'>['channels'][number] => {
    const fields = CH_FIELDS[id] || []
    const configured = filled.has(id)
    return {
      name: id,
      enabled: on.has(id),
      configured,
      /* An entrance in service has its credentials in place; one that is not is
         missing all of them, which is what its row counts down. */
      missing: configured ? [] : fields.map((f) => f.key),
      fields: fields.map((f) => ({ ...f, set: configured })),
      running: on.has(id),
      connected: qrLogin ? false : undefined,
      qr_login: qrLogin,
    }
  }

  return {
    fixtures: {
      'channels.status': () => ({
        channels: CHANNELS.map((c) => row(c.id, !!c.qrLogin)),
        /* The demo's world has a host in it -- one of its channels is
           receiving -- so it answers yes. Left unanswered, the page would tell
           the reader nothing is running it over a row that is. */
        gateway_running: true,
      }),
      'channels.configure': (p) => {
        const params = p as { name: string; fields?: Record<string, unknown>; enabled?: boolean }
        if (params.fields && Object.keys(params.fields).length) filled.add(params.name)
        if (params.enabled) on.add(params.name)
        else on.delete(params.name)
        return { applied: true }
      },
      /* No code: the canvas has no adapter to mint one, and the island draws
         the same waiting frame it draws for a gateway that cannot be asked. */
      'channels.qr': () => ({ connected: false, running: false }),
    },
  }
}
