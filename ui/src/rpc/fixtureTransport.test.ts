import { describe, expect, it } from 'vitest'

import { FixtureTransport } from './fixtureTransport'
import { RpcError } from './transport'

import type { ConnectionState } from './transport'

describe('FixtureTransport', () => {
  it('answers a value responder as-is', async () => {
    const t = new FixtureTransport({ 'cron.list': { jobs: [] } })
    await expect(t.call('cron.list', {})).resolves.toEqual({ jobs: [] })
  })

  it('hands a function responder the params the caller sent', async () => {
    const seen: unknown[] = []
    const t = new FixtureTransport({
      'session.list': (params) => {
        seen.push(params)
        return { sessions: [] }
      },
    })
    await t.call('session.list', { limit: 3 })
    expect(seen).toEqual([{ limit: 3 }])
  })

  it('awaits an async function responder', async () => {
    const t = new FixtureTransport({
      'session.list': async () => ({ sessions: [] }),
    })
    await expect(t.call('session.list', {})).resolves.toEqual({ sessions: [] })
  })

  it('rejects an unrecorded method with the -32601 an engine would send', async () => {
    const t = new FixtureTransport({})
    const err = await t.call('cron.list', {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(RpcError)
    expect((err as RpcError).code).toBe(-32601)
  })

  it('records every call in order, for traffic assertions', async () => {
    const t = new FixtureTransport({
      'cron.list': { jobs: [] },
      'session.list': { sessions: [] },
    })
    await t.call('session.list', { limit: 1 })
    await t.call('cron.list', {})
    expect(t.calls.map((c) => c.method)).toEqual(['session.list', 'cron.list'])
  })

  it('fans a pushed notification out to handlers, and detach removes one', () => {
    const t = new FixtureTransport({})
    const got: unknown[] = []
    const detach = t.on('turn.delta', (p) => got.push(p))
    t.on('turn.delta', (p) => got.push(p))
    t.emit('turn.delta', { text: 'x' })
    detach()
    t.emit('turn.delta', { text: 'y' })
    expect(got).toEqual([{ text: 'x' }, { text: 'x' }, { text: 'y' }])
  })

  it('reports the current state immediately, then each transition', async () => {
    const t = new FixtureTransport({})
    const states: ConnectionState[] = []
    t.onState((s) => states.push(s))
    await t.connect()
    t.close()
    expect(states).toEqual(['closed', 'open', 'closed'])
  })
})
