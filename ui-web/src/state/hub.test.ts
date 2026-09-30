// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as hub from './hub'
import * as page from './page'

/* The two pages the hub switches between, and the drawer a page switch closes,
   so page.show runs against the markup it writes on. */
beforeEach(() => {
  document.body.innerHTML =
    '<div class="app"></div>' +
    '<section class="page" id="extAgentsPage" data-open="false"><div class="work"></div></section>' +
    '<section class="page" id="connectionsPage" data-open="false"><div class="work"></div></section>' +
    '<aside class="detail" id="detail" data-open="false"></aside>'
  hub._resetForTests()
})

afterEach(() => {
  page.show(null)
  vi.restoreAllMocks()
})

describe('the connections hub', () => {
  it('opens agents first, before the reader has been anywhere', () => {
    const agents = vi.fn()
    hub.onOpen('agents', agents)
    hub.onOpen('channels', vi.fn())
    hub.open()
    expect(agents).toHaveBeenCalledTimes(1)
  })

  /* The rail row goes back to where the reader was, whichever door they used:
     the greeting's entry opens the channel page through its own store, not
     through the hub. */
  it('goes back to the module the reader was last on, by the page switch', () => {
    const agents = vi.fn()
    const channels = vi.fn()
    hub.onOpen('agents', agents)
    hub.onOpen('channels', channels)
    page.show('connectionsPage')
    page.show(null)
    hub.open()
    expect(channels).toHaveBeenCalledTimes(1)
    expect(agents).not.toHaveBeenCalled()
    page.show('extAgentsPage')
    expect(hub.lastOpened()).toBe('agents')
  })

  /* Leaving the hub is not a module: a switch to no page, or to a page that is
     not one of the two, keeps the last one. */
  it('keeps the last module across a switch to nothing', () => {
    page.show('connectionsPage')
    page.show(null)
    expect(hub.lastOpened()).toBe('channels')
  })

  it('opens the module it is asked for', () => {
    const channels = vi.fn()
    hub.onOpen('agents', vi.fn())
    hub.onOpen('channels', channels)
    hub.open('channels')
    expect(channels).toHaveBeenCalledTimes(1)
  })
})
