import { afterEach, describe, expect, it } from 'vitest'

import { FixtureTransport } from './fixtureTransport'
import { gateway, setGateway } from './gateway'

afterEach(() => {
  setGateway(null)
})

describe('the installed gateway', () => {
  it('refuses to invent one when nothing is installed', () => {
    expect(() => gateway()).toThrow('no gateway installed')
  })

  it('hands back the transport that was installed', () => {
    const transport = new FixtureTransport({})
    setGateway(transport)

    expect(gateway()).toBe(transport)
  })

  it('can be taken back out, so a test leaves nothing behind', () => {
    setGateway(new FixtureTransport({}))
    setGateway(null)

    expect(() => gateway()).toThrow('no gateway installed')
  })
})
