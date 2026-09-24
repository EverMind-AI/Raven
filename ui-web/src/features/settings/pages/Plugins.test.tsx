// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { plugChip } from './Plugins'

import type { McpSnapshot } from '../../../rpc/generated'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

const m = (over: Partial<McpSnapshot>): McpSnapshot => ({
  name: 'x', transport: 'http', state: 'connected', connected: true, tool_count: 1, enabled: true, auth: 'none', credentialed: null, ...over,
})

describe('plugins page', () => {
  it('maps the manager state and the credential to one chip', () => {
    expect(plugChip(m({ enabled: false }), false)).toBe('none')
    expect(plugChip(m({}), false)).toBe('connected')
    expect(plugChip(m({}), true)).toBe('connecting')
    expect(plugChip(m({ state: 'connecting', connected: false }), false)).toBe('connecting')
    expect(plugChip(m({ auth: 'oauth', credentialed: false }), false)).toBe('setup')
    expect(plugChip(m({ auth: 'apikey', credentialed: false, state: 'error' }), false)).toBe('setup')
    expect(plugChip(m({ state: 'error', connected: false }), false)).toBe('failed')
    expect(plugChip(m({ state: 'auth_required', connected: false, auth: 'oauth', credentialed: true }), false)).toBe('failed')
  })

  it('draws a chip only while the switch is on and counts the connected ones', async () => {
    install()
    await mount('plugins')
    expect(screen.getByText('gui.settings.plugins.counter {"on":1,"total":4}')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.connected')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.setup')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.failed')).toBeTruthy()
    expect(document.querySelectorAll('.settings-xrow.settings-dim')).toHaveLength(1)
  })

  /* No server: the empty card says so, and a "0 of 0 connected" count above it
     was a number about nothing. */
  it('drops the connected count when there is no server, leaving the empty card', async () => {
    install(snap({ mcp: [] }))
    await mount('plugins')
    expect(screen.getByText('gui.settings.plugins.none')).toBeTruthy()
    expect(screen.queryByText(/gui\.settings\.plugins\.counter/)).toBeNull()
  })

  it('the failed chip is a status, and the reconnect lives in the drawer beside the reason', async () => {
    /* "Failed · retry" was a button on the row: the one place that says what
       state a server is in was also where a stray click reconnected it. */
    const { calls } = install()
    await mount('plugins')
    const chip = screen.getByText('gui.settings.plugins.failed')
    expect(chip.closest('button')).toBeNull()
    await act(async () => { fireEvent.click(chip) })
    expect(calls).toEqual([])
    await act(async () => { fireEvent.click(screen.getByText('github')) })
    await act(async () => { await Promise.resolve() })
    const fail = document.querySelector('.settings-pfail') as HTMLElement
    /* First in the drawer, before the credential: it is what the row was
       opened for. */
    expect(document.querySelector('.settings-cfg')!.firstElementChild).toBe(fail)
    expect(fail.textContent).toContain('boom')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.plugins.retry')) })
    expect(calls).toEqual([['retryServer', 'github']])
  })

  it('a connected server\'s drawer offers no reconnect', async () => {
    install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('context7')) })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('.settings-pfail')).toBeNull()
  })

  it('the switch goes through plug.toggle', async () => {
    const { calls } = install()
    await mount('plugins')
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'context7' })) })
    expect(calls).toEqual([['toggleServer', { name: 'context7', on: false }]])
  })

  it('turning on a server that still needs its credential opens its panel', async () => {
    const { calls } = install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'asana' })) })
    expect(calls).toEqual([['toggleServer', { name: 'asana', on: false }]])
    calls.length = 0
    await act(async () => { store.set({ snap: { ...store.get().snap, mcp: store.get().snap.mcp.map((r) => (r.name === 'asana' ? { ...r, enabled: false } : r)) } }) })
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'asana' })) })
    expect(store.get().plugOpen).toBe('asana')
    expect(screen.getByText('gui.settings.plugins.auth_browser')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.plugins.auth_browser')) })
    expect(calls).toEqual([['toggleServer', { name: 'asana', on: true }], ['authServer', 'asana']])
  })

  it('an authorized OAuth server offers re-authorize and revoke', async () => {
    const { calls } = install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('notion')) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.plugins.revoke')) })
    expect(calls).toEqual([['revokeServer', 'notion']])
  })

  it('an API-key server writes the catalog field by its key and clears it with an empty value', async () => {
    const { calls } = install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('github')) })
    await act(async () => { await Promise.resolve() })
    const box = await screen.findByLabelText('Token') as HTMLInputElement
    expect(document.querySelector('a.settings-lnkico')!.getAttribute('href')).toBe('https://github.com/settings/tokens')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.update')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent).toBe('gui.settings.plugins.key_first')
    await act(async () => { fireEvent.change(box, { target: { value: 'ghp_x' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.update')) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.clear')) })
    expect(calls).toEqual([
      ['configureServer', { name: 'github', form: { token: 'ghp_x' } }],
      ['configureServer', { name: 'github', form: { token: '' } }],
    ])
  })

  it('shows what the server is and what went wrong, not just how to sign in', async () => {
    install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('github')) })
    await act(async () => { await Promise.resolve() })
    /* The manager's own words. They reached the reader as the word "failed" on
       a chip and nowhere else, which is not something anyone can act on. */
    expect(screen.getByText('boom')).toBeTruthy()
    expect(screen.getByText('https://api.githubcopilot.com/mcp')).toBeTruthy()
    expect(screen.getByText('gui.settings.plugins.tools_n {"n":3}')).toBeTruthy()
    expect(screen.getByText('list_issues get_pr')).toBeTruthy()
  })

  it('does not call a server credential-free just because the catalogue has no entry for it', async () => {
    /* Two different facts, one of them a lie: the row says this server takes a
       key, and the catalogue -- which is where the FIELD names come from --
       simply has nothing filed under its name. github reached this state on
       the live page, with an expired token and a panel telling the reader it
       needed nothing. */
    install(undefined, { serverDetail: async () => ({ known: false, fields: [], tools: [] }) })
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('github')) })
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByText('gui.settings.plugins.no_credential')).toBeNull()
    expect(screen.getByText('gui.settings.plugins.no_fields')).toBeTruthy()
  })

  it('says a credential-free server takes none, on the server\'s own word', async () => {
    install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('context7')) })
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('gui.settings.plugins.no_credential')).toBeTruthy()
    expect(screen.queryByLabelText('Token')).toBeNull()
  })
})
