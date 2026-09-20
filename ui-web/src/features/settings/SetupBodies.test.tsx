// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setGateway } from '../../rpc/gateway'
import { resetSources, setSources } from '../../state/sources'
import { install, mount, snap, source as settingsSource } from '../../test/settingsHarness'
import { _resetForTests as resetModelSource, setDefaultPair } from '../model/source'
import { ModelStepBody, WebStepBody } from './SetupBodies'
import { _resetForTests as resetSettingsSource, loadSettingsWithProviders, modelStepDone, webStepDone } from './source'
import * as store from './store'

import type { RpcTransport } from '../../rpc/transport'
import type { SettingsSnapshot } from './types'
import type { JSX } from 'react'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

beforeEach(() => {
  setSources({ settings: settingsSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

/* Both panes render straight into the test container -- neither portals
   anywhere -- so a plain render plus the wizard's own refresh() is the whole
   setup, the same call the real wizard makes on its own. */
async function openBody(Pane: () => JSX.Element): Promise<void> {
  render(createElement(Pane))
  await act(async () => { await store.refresh() })
}

/* Nothing connected: off[0] is the row Providers' own effect opens. */
function noneConnected(): SettingsSnapshot {
  const data = snap()
  data.providers = data.providers.map((p) => ({ ...p, on: false }))
  return data
}

describe('ModelStepBody', () => {
  it('shows the loading line until the store has loaded, then the model cards', async () => {
    install()
    render(createElement(ModelStepBody))
    expect(document.querySelector('.settings-panel.settings-setup')!.getAttribute('data-section')).toBe('model')
    expect(document.querySelector('.settings-soonbox')!.textContent).toBe('gui.settings.loading')
    await act(async () => { await store.refresh() })
    expect(document.querySelector('.settings-soonbox')).toBeNull()
    expect(screen.getByText('gui.settings.providers.title {"n":2}')).toBeTruthy()
    expect(screen.getByText('gui.settings.roles.title_card')).toBeTruthy()
  })

  it('a connected row\'s trailing button disconnects it, calling the provider op directly', async () => {
    const { calls } = install()
    await openBody(ModelStepBody)
    const row = [...document.querySelectorAll('.settings-prow2')].find((r) => r.textContent?.includes('anthropic'))!
    expect(row.querySelector('button')!.textContent).toBe('gui.settings.providers.disconnect')
    await act(async () => { fireEvent.click(row.querySelector('button')!) })
    expect(calls).toEqual([['provider', { op: 'disconnect', slug: 'anthropic' }]])
  })

  it('opens the add block on its own on an empty machine, with no Cancel button', async () => {
    install(noneConnected())
    await openBody(ModelStepBody)
    expect(document.querySelector('.settings-padd')).toBeTruthy()
    expect(screen.queryByText('gui.cancel')).toBeNull()
    expect(screen.queryByText('gui.settings.providers.add')).toBeNull()
  })

  it('the Cancel button is back once a provider is connected', async () => {
    install()
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    expect(screen.getByText('gui.cancel')).toBeTruthy()
  })
})

describe('WebStepBody', () => {
  it('draws the search and fetch cards with their vendor selects and ready/unset chips', async () => {
    install()
    await openBody(WebStepBody)
    expect(document.querySelector('.settings-panel.settings-setup')!.getAttribute('data-section')).toBe('tools')
    expect(document.querySelectorAll('.settings-card')).toHaveLength(2)
    expect(screen.getByText('gui.settings.setup.web_search')).toBeTruthy()
    expect(screen.getByText('gui.settings.setup.web_fetch')).toBeTruthy()
    // The fixture's default search vendor (serper) carries no key yet; its
    // default fetch vendor (jina) needs none, so that card reads ready.
    expect(screen.getByText('gui.settings.setup.unset')).toBeTruthy()
    expect(screen.getByText('gui.settings.setup.ready')).toBeTruthy()
  })

  it('a search vendor with a key on file reads as ready', async () => {
    const data = snap()
    ;(data.raw.tools as Record<string, unknown>).web = { search: { provider: 'serper' }, providers: { serper: { apiKey: '****set****' } } }
    install(data)
    await openBody(WebStepBody)
    // Both cards read ready now: the search vendor has its key, and the fetch
    // vendor is the keyless jina.
    expect(screen.getAllByText('gui.settings.setup.ready')).toHaveLength(2)
    expect(screen.queryByText('gui.settings.setup.unset')).toBeNull()
  })

  it('the keyless fetch vendor (jina) carries an optional tag and the keyless hint', async () => {
    install()
    await openBody(WebStepBody)
    expect(screen.getByLabelText('gui.settings.tools.vendor_key {"name":"Jina Reader"} · gui.settings.roles.optional')).toBeTruthy()
    expect(screen.getByText('gui.settings.setup.keyless_hint')).toBeTruthy()
  })

  it('a keyed fetch vendor carries no optional tag and no hint', async () => {
    const data = snap()
    ;(data.raw.tools as Record<string, unknown>).web = { fetch: { provider: 'tavily' } }
    install(data)
    await openBody(WebStepBody)
    expect(screen.getByLabelText('gui.settings.tools.vendor_key {"name":"Tavily"}')).toBeTruthy()
    expect(screen.queryByText('gui.settings.setup.keyless_hint')).toBeNull()
    // Neither vendor has a key, so neither card reads ready.
    expect(screen.getAllByText('gui.settings.setup.unset')).toHaveLength(2)
    expect(screen.queryByText('gui.settings.setup.ready')).toBeNull()
  })
})

describe('the dialog path', () => {
  it('still shows the manage button, not disconnect, on a connected row', async () => {
    install()
    await mount('model')
    expect(screen.getAllByText('gui.settings.providers.manage').length).toBeGreaterThan(0)
    expect(screen.queryByText('gui.settings.providers.disconnect')).toBeNull()
  })
})

describe('the wizard step-done predicates', () => {
  afterEach(() => {
    setGateway(null)
    resetModelSource()
    resetSettingsSource()
  })

  /* Drives the real settings and model sources through a fake gateway --
     modelStepDone and webStepDone read their module state directly, so a
     canned SettingsSnapshot (which is all the harness above hands out) never
     reaches them. */
  async function loadRaw(
    tools: Record<string, unknown>,
    agentsDefaults: Record<string, unknown> = {},
    providers: Array<Record<string, unknown>> = [],
  ): Promise<void> {
    setGateway({
      call: async (method: string) => {
        if (method === 'settings.get') return { settings: { tools, agents: { defaults: agentsDefaults } }, config_path: '~/.raven/config.json' }
        if (method === 'model.options') return { providers, model: '' }
        throw new Error(`unmocked ${method}`)
      },
      on: () => () => {},
      binary: () => () => {},
    } as unknown as RpcTransport)
    // The wizard's refresh runs the with-providers load; the step verdict reads both.
    await loadSettingsWithProviders()
  }

  it('modelStepDone needs both a connected provider and a default model', async () => {
    await loadRaw({})
    expect(modelStepDone()).toBe(false)
    await loadRaw({}, { model: 'claude-opus-4-5', provider: 'anthropic' })
    expect(modelStepDone()).toBe(false)
    await loadRaw({}, {}, [{ slug: 'anthropic', name: 'Anthropic', authenticated: true }])
    expect(modelStepDone()).toBe(false)
    await loadRaw({}, { model: 'claude-opus-4-5', provider: 'anthropic' }, [{ slug: 'anthropic', name: 'Anthropic', authenticated: true }])
    expect(modelStepDone()).toBe(true)
    // setDefaultPair is the same seam a persisted pick writes through.
    setDefaultPair('', '')
    expect(modelStepDone()).toBe(false)
  })

  it('webStepDone is set by either web tool vendor holding a key, the legacy leaf included', async () => {
    await loadRaw({})
    expect(webStepDone()).toBe(false)
    await loadRaw({ web: { providers: { serper: { apiKey: 'k' } } } })
    expect(webStepDone()).toBe(true)
    await loadRaw({ web: { search: { apiKey: 'legacy' } } })
    expect(webStepDone()).toBe(true)
    await loadRaw({ web: { fetch: { provider: 'tavily' }, providers: { tavily: { apiKey: 'k' } } } })
    expect(webStepDone()).toBe(true)
    await loadRaw({ web: { fetch: { provider: 'tavily' } } })
    expect(webStepDone()).toBe(false)
  })
})
