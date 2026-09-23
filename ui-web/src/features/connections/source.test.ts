// @vitest-environment happy-dom
/* What one `channels.status` answer does to the catalogue's rows. Three of the
 * merge's rules are about telling "no" apart from "nobody could say".
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import * as toastStore from '../../state/toast'
import { CHANNELS, chanName } from './catalogue'
import { connSource, loadChannels } from './source'


type StatusRow = { name: string } & Record<string, unknown>

setTranslator((key: string, vars?: Record<string, unknown> | null) =>
(vars ? `${key}(${Object.entries(vars).map(([k, v]) => `${k}=${v}`).join(',')})` : key))
vi.spyOn(pageStore, 'show').mockImplementation(() => {})
vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
/* The notices, which are the switch's whole report to the reader. */
const said: string[] = []
vi.spyOn(toastStore, 'show').mockImplementation((text: string) => {
  said.push(text)
})

function answering(channels: StatusRow[], gatewayRunning = true): void {
  const transport = new FixtureTransport({})
  transport.call = (async () => ({ channels, gateway_running: gatewayRunning })) as typeof transport.call
  setGateway(transport)
}

/* Both halves of a switch press: the write, answered with the word the gateway
   sent back, and the status read that follows it. */
function switching(result: Record<string, unknown>, after: StatusRow[], gatewayRunning = true): string[] {
  const asked: string[] = []
  const transport = new FixtureTransport({})
  transport.call = (async (method: string) => {
    asked.push(method)
    return method === 'channels.configure' ? result : { channels: after, gateway_running: gatewayRunning }
  }) as typeof transport.call
  setGateway(transport)
  return asked
}

const row = (id: string) => CHANNELS.find((c) => c.id === id)!

beforeEach(() => {
  said.length = 0
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

  /* One spelling: a brand whose name is the same in both languages is still
     named through the message catalogue, not written into the row. */
  it('names every row through the message catalogue', () => {
    expect(chanName(row('feishu'))).toBe('gui.chan.feishu')
    expect(chanName(row('slack'))).toBe('gui.chan.slack')
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

/* The switch used to be a write and a guess: it flipped the flag, toasted
   "reopen the Raven app", and left every live fact on the row at whatever the
   last section entry had read. The gateway applies the write on the spot and
   answers with a word, so both halves of that are now readable. */
describe('the row switch', () => {
  it('reads the status back, so the row is not the previous load\'s', async () => {
    const asked = switching({ applied: true, outcome: 'started' }, [
      { name: 'slack', enabled: true, running: true, connected: null },
    ])
    await connSource.toggle(row('slack'), true)
    expect(asked).toEqual(['channels.configure', 'channels.status'])
    expect(row('slack')).toMatchObject({ on: true, running: true, connected: null })
  })

  it('says what the gateway did rather than what the reader should do about it', async () => {
    switching({ applied: true, outcome: 'started' }, [{ name: 'slack', enabled: true, running: true }])
    await connSource.toggle(row('slack'), true)
    expect(said).toEqual(['gui.conn.toggled_now(name=gui.chan.slack,state=gui.conn.enabled)'])
  })

  /* The one case the old sentence was true of: nobody applied the write, so the
     next launch is what honours it. */
  it('keeps the reopen advice for the write no gateway took', async () => {
    switching({ applied: true, outcome: 'unreachable' }, [{ name: 'slack', enabled: true }], false)
    await connSource.toggle(row('slack'), true)
    expect(said).toEqual(['gui.conn.toggled(name=gui.chan.slack,state=gui.conn.enabled)'])
  })

  /* A refusal has a reason and something to do about it; both were dropped, and
     the row went red with neither. */
  it('names why an adapter would not start, with the server sentence', async () => {
    switching({ applied: true, outcome: 'missing_dep', detail: 'Run: uv sync --inexact --extra channels' }, [
      { name: 'slack', enabled: true, running: false },
    ])
    await connSource.toggle(row('slack'), true)
    expect(said).toEqual([
      'gui.conn.toggle_failed(name=gui.chan.slack,detail=gui.conn.out_missing_dep Run: uv sync --inexact --extra channels)',
    ])
  })

  it('keeps an outcome it has no sentence for readable', async () => {
    switching({ applied: true, outcome: 'no_manager' }, [{ name: 'slack', enabled: true }])
    await connSource.toggle(row('slack'), true)
    expect(said).toEqual([
      'gui.conn.toggle_failed(name=gui.chan.slack,detail=gui.conn.out_refused(outcome=no_manager))',
    ])
  })

  /* The reload rides on the write's promise, so its failure would otherwise
     arrive as the write's: switch back, "could not save", neither of them
     true. */
  it('keeps the write when the status read that follows it fails', async () => {
    const transport = new FixtureTransport({})
    transport.call = (async (method: string) => {
      if (method === 'channels.configure') return { applied: true, outcome: 'started' }
      throw new Error('offline')
    }) as typeof transport.call
    setGateway(transport)
    await connSource.toggle(row('slack'), true)
    expect(row('slack').on).toBe(true)
    expect(said).toEqual(['gui.conn.toggled_now(name=gui.chan.slack,state=gui.conn.enabled)'])
  })

  /* The pane's Connect is the same write, and it is where a wheel install meets
     this: the credentials were saved and the adapter never came up. */
  it('says why the pane\'s connect did not start anything either', async () => {
    switching({ applied: true, outcome: 'missing_dep', detail: 'Run: the installer' }, [
      { name: 'slack', enabled: true, running: false },
    ])
    await connSource.apply(row('slack'), { bot_token: 'x' }, true)
    expect(said).toEqual([
      'gui.conn.saved_x(name=gui.chan.slack)',
      'gui.conn.toggle_failed(name=gui.chan.slack,detail=gui.conn.out_missing_dep Run: the installer)',
    ])
  })

  /* Switching off is done the moment the config says so, whatever the gateway
     had in its table. */
  it('reports a stop as a stop', async () => {
    switching({ applied: true, outcome: 'absent' }, [{ name: 'slack', enabled: false }])
    await connSource.toggle(row('slack'), false)
    expect(said).toEqual(['gui.conn.toggled_now(name=gui.chan.slack,state=gui.conn.disabled)'])
  })
})
