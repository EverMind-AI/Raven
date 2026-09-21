// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, source as settingsSource } from '../../../test/settingsHarness'
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
    expect(screen.getByText('gui.settings.plugins.failed_retry')).toBeTruthy()
    expect(document.querySelectorAll('.settings-xrow.settings-dim')).toHaveLength(1)
  })

  it('retry reconnects without toggling; the switch goes through plug.toggle', async () => {
    const { calls } = install()
    await mount('plugins')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.plugins.failed_retry')) })
    expect(calls).toEqual([['retryServer', 'github']])
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
})
