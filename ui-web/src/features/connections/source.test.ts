// @vitest-environment happy-dom
/* What one `channels.status` answer does to the catalogue's rows. The merge
 * was untested while it lived in the legacy layer, and three of its rules are
 * about telling "no" apart from "nobody could say".
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { CHANNELS, chanName } from './catalogue'
import { connSource, loadChannels } from './source'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setShell } from '../../shell/bridge'
import { setGateway } from '../../state/gateway'

import type { Shell } from '../../shell/bridge'

type StatusRow = { name: string } & Record<string, unknown>

const shell = {
  T: (key: string, vars?: Record<string, string | number>) =>
    (vars ? `${key}(${Object.entries(vars).map(([k, v]) => `${k}=${v}`).join(',')})` : key),
  confirmAsk: () => {},
  showPage: () => {},
} as unknown as Shell

function answering(channels: StatusRow[], gatewayRunning = true): void {
  const transport = new FixtureTransport({})
  transport.call = (async () => ({ channels, gateway_running: gatewayRunning })) as typeof transport.call
  setGateway(transport)
}

const row = (id: string) => CHANNELS.find((c) => c.id === id)!

beforeEach(() => {
  setShell(shell)
  /* The rows are one shared array for the life of the page, so a case starts
     from the state the last one left -- the same thing a redraw does. */
  CHANNELS.forEach((c) => {
    c.on = false
    delete c.who
    delete c.fields
    delete c.missing
    delete c.running
    delete c.connected
  })
})

describe('the catalogue', () => {
  it('holds the twelve entrances', () => {
    expect(CHANNELS).toHaveLength(12)
    expect(new Set(CHANNELS.map((c) => c.id)).size).toBe(12)
  })

  /* Two spellings: a catalogue key for the channels whose names differ by
     language, a brand name used verbatim for the rest. */
  it('names a row by whichever of the two spellings it carries', () => {
    expect(chanName(row('feishu'))).toBe('gui.chan.feishu')
    expect(chanName(row('slack'))).toBe('Slack')
  })

  it('marks the two that sign in by scanning a code', () => {
    expect(CHANNELS.filter((c) => c.qrLogin).map((c) => c.id)).toEqual(['weixin', 'whatsapp'])
  })
})

describe('merging one status answer', () => {
  it('writes the schema-declared field list onto the row', async () => {
    answering([{ name: 'slack', enabled: true, fields: [{ key: 'bot_token' }], missing: ['bot_token'] }])
    await loadChannels()
    expect(row('slack')).toMatchObject({ on: true, fields: [{ key: 'bot_token' }], missing: ['bot_token'] })
  })

  /* Three separate facts, kept separate: what the config asks for, whether the
     adapter came up, and whether the account is paired. */
  it('keeps enabled, running and connected apart', async () => {
    answering([{ name: 'weixin', enabled: true, running: false, connected: null, qr_login: true }])
    await loadChannels()
    expect(row('weixin')).toMatchObject({ on: true, running: false, connected: null, qrLogin: true })
  })

  it('leaves a row the gateway did not mention alone', async () => {
    answering([{ name: 'slack', enabled: true }])
    await loadChannels()
    expect(row('discord').on).toBe(false)
    expect(row('discord')).not.toHaveProperty('fields')
  })

  /* `who` is reserved for a real identity, which no backend supplies yet, so a
     live row keeps its sub line empty rather than showing the demo's. */
  it('clears the identity line, which nothing fills yet', async () => {
    row('feishu').who = 'EverMind'
    answering([{ name: 'feishu', enabled: true }])
    await loadChannels()
    expect(row('feishu').who).toBe('')
  })

  it('answers with the catalogue rows themselves, so a status lands on what is drawn', async () => {
    answering([{ name: 'slack', enabled: true }])
    const rows = await connSource.rows()
    expect(rows).toBe(CHANNELS)
    expect(rows.find((c) => c.id === 'slack')?.on).toBe(true)
  })

  it('reports whether anything could host an adapter at all', async () => {
    answering([], false)
    await connSource.rows()
    expect(connSource.hostRunning?.()).toBe(false)
    answering([], true)
    await connSource.rows()
    expect(connSource.hostRunning?.()).toBe(true)
  })

  /* A background reload stays silent; only the page-open fetch reports. */
  it('keeps the last rows when the read fails', async () => {
    answering([{ name: 'slack', enabled: true }])
    await connSource.rows()
    const transport = new FixtureTransport({})
    transport.call = (async () => { throw new Error('offline') }) as typeof transport.call
    setGateway(transport)
    await expect(connSource.rows()).resolves.toBe(CHANNELS)
    expect(row('slack').on).toBe(true)
  })
})
