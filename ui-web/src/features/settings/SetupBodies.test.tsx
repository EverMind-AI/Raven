// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setGateway } from '../../rpc/gateway'
import { resetSources, setSources } from '../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../test/settingsHarness'
import { _resetForTests as resetModelSource, setDefaultPair } from '../model/source'
import { MemoryStepBody, ModelStepBody, WebStepBody } from './SetupBodies'
import { _resetForTests as resetSettingsSource, loadSettingsWithProviders, modelStepDone, webStepDone } from './source'
import * as store from './store'

import type { RpcTransport } from '../../rpc/transport'
import type { SettingsSnapshot } from './types'
import type { JSX } from 'react'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
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
  it('a role row with no provider opens the add form above on a vendor that serves it', async () => {
    /* The wizard has no providers page to switch to; sending the reader there
       changed a tab nobody can see. */
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, on: false } : p))
    install(data)
    await openBody(ModelStepBody)
    expect(document.querySelector('.settings-padd')).toBeNull()
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.roles.connect_openrouter')[0]!) })
    expect(store.get().provAdd).toBe('openrouter')
    expect(store.get().tab).not.toBe('provider')
    expect(document.querySelector('.settings-padd .settings-vpick')!.lastElementChild!.textContent).toBe('OpenRouter')
  })

  it('the vendor dropdown opens a scrolling list of fixed height and a pick swaps the form', async () => {
    /* A native select opens a popup as tall as the screen for fifty-odd
       vendors, and its height is not the page's to set. */
    install(noneConnected())
    await openBody(ModelStepBody)
    const field = document.querySelector('.settings-padd .settings-vpick') as HTMLButtonElement
    await act(async () => { fireEvent.click(field) })
    const list = document.querySelector('.settings-vlist') as HTMLElement
    expect(list.parentElement).toBe(document.body)
    expect(list.style.maxHeight).toBe('300px')
    const openrouter = screen.getByRole('option', { name: 'OpenRouter' })
    await act(async () => { fireEvent.click(openrouter) })
    expect(document.querySelector('.settings-vlist')).toBeNull()
    expect(store.get().provAdd).toBe('openrouter')
    expect(document.querySelector('.settings-padd .settings-vpick')!.lastElementChild!.textContent).toBe('OpenRouter')
  })

  it('Escape closes the vendor list without reaching anything behind it', async () => {
    install(noneConnected())
    await openBody(ModelStepBody)
    const behind = vi.fn()
    document.addEventListener('keydown', behind)
    await act(async () => { fireEvent.click(document.querySelector('.settings-padd .settings-vpick')!) })
    await act(async () => { fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' }) })
    document.removeEventListener('keydown', behind)
    expect(document.querySelector('.settings-vlist')).toBeNull()
    expect(behind).not.toHaveBeenCalled()
  })

  it('draws both cards on the empty snapshot, and never a wait shape', async () => {
    install()
    render(createElement(ModelStepBody))
    expect(document.querySelector('.settings-panel.settings-setup')!.getAttribute('data-section')).toBe('model')
    /* Before the payload: the real cards, counting the providers it knows of,
       which on the run this step exists for is none. */
    expect(document.querySelector('.settings-wait')).toBeNull()
    expect(screen.getByText('gui.settings.providers.title {"n":0}')).toBeTruthy()
    expect(screen.getByText('gui.settings.roles.title_card')).toBeTruthy()
    await act(async () => { await store.refresh() })
    expect(document.querySelector('.settings-wait')).toBeNull()
    expect(screen.getByText('gui.settings.providers.title {"n":2}')).toBeTruthy()
    expect(screen.getByText('gui.settings.roles.title_card')).toBeTruthy()
  })

  it('a connected row\'s trailing button disconnects it, calling the provider op directly', async () => {
    const { calls } = install()
    await openBody(ModelStepBody)
    const row = [...document.querySelectorAll('.settings-prow2')].find((r) => r.textContent?.includes('Anthropic'))!
    expect(row.querySelector('button')!.textContent).toBe('gui.settings.providers.disconnect')
    await act(async () => { fireEvent.click(row.querySelector('button')!) })
    expect(calls).toEqual([['provider', { op: 'disconnect', slug: 'anthropic' }]])
  })

  it('a connected row names the vendor once', async () => {
    install()
    await openBody(ModelStepBody)
    const row = [...document.querySelectorAll('.settings-prow2')].find((r) => r.textContent?.includes('Anthropic'))!
    expect(row.textContent).not.toContain('anthropic')
  })

  it('draws the rule under the add form, not between the card title and the form', async () => {
    install()
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    const form = document.querySelector('.settings-padd')!
    expect(form.previousElementSibling!.className).toContain('settings-ch')
    expect(form.nextElementSibling!.className).toContain('settings-prow2')
  })

  it('opens the add block on its own on an empty machine, with no Cancel button', async () => {
    install(noneConnected())
    await openBody(ModelStepBody)
    expect(document.querySelector('.settings-padd')).toBeTruthy()
    expect(screen.queryByText('gui.cancel')).toBeNull()
    expect(screen.queryByText('gui.settings.providers.add')).toBeNull()
  })

  it('groups the vendors the catalogue page\'s way, an aggregator by what it resells and not by its credential', async () => {
    const data = noneConnected()
    data.providers.push(
      { id: 'custom', name: 'Custom', models: [], configured: [], on: false, kind: 'endpoint', acceptsKey: true, gateway: true },
      { id: 'azure_openai', name: 'Azure', models: [], configured: [], on: false, kind: 'endpoint', acceptsKey: true, needsBase: true },
    )
    install(data)
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(document.querySelector('.settings-padd .settings-vpick')!) })
    const groups = [...document.querySelectorAll('.settings-vlist [role="group"]')]
    expect(groups.map((g) => g.getAttribute('aria-label'))).toEqual([
      'gui.settings.providers.filter_direct',
      'gui.settings.providers.filter_gateway',
      'gui.settings.providers.filter_oauth',
      'gui.model.kind.local',
    ])
    const inGroup = (label: string): string[] =>
      [...groups.find((g) => g.getAttribute('aria-label') === label)!.querySelectorAll('[role="option"]')].map((o) => o.lastElementChild!.textContent!)
    expect(inGroup('gui.settings.providers.filter_gateway')).toEqual(['OpenRouter', 'Custom'])
    expect(inGroup('gui.settings.providers.filter_direct')).toEqual(['Anthropic', 'OpenAI', 'Azure'])
    expect(inGroup('gui.settings.providers.filter_oauth')).toEqual(['MiniMax Global'])
    expect(inGroup('gui.model.kind.local')).toEqual(['Ollama'])
  })

  it('an aggregator takes an address too, filled and hinted from the one it ships with', async () => {
    const data = noneConnected()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, apiBase: '' } : p))
    install(data)
    await openBody(ModelStepBody)
    expect(screen.queryByLabelText('gui.settings.providers.base')).toBeNull()
    await act(async () => { fireEvent.click(document.querySelector('.settings-padd .settings-vpick')!) })
    await act(async () => { fireEvent.click(screen.getByRole('option', { name: 'OpenRouter' })) })
    const address = screen.getByLabelText('gui.settings.providers.base') as HTMLInputElement
    expect(address.value).toBe('https://openrouter.ai/api/v1')
    expect(address.placeholder).toBe('https://openrouter.ai/api/v1')
  })

  it('connect saves at once, closes the form, and the row tests the key on the side', async () => {
    let answer: (v: unknown) => void = () => {}
    const data = snap()
    const { calls } = install(data, {
      provider: async (op, params) => {
        calls.push(['provider', { op, ...params }])
        data.providers = data.providers.map((p) => (p.id === params.slug ? { ...p, on: true } : p))
        return { ...data }
      },
      fetchModels: (slug, verify) => {
        calls.push([verify ? 'fetchModels:verify' : 'fetchModels', slug])
        return new Promise((r) => { answer = r as (v: unknown) => void })
      },
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: '111' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    const saved = calls.find(([m]) => m === 'provider')![1] as { op: string; slug: string }
    expect(saved.op).toBe('save_key')
    expect(document.querySelector('.settings-padd')).toBeNull()
    expect(calls).toContainEqual(['fetchModels:verify', saved.slug])
    const row = (): Element => [...document.querySelectorAll('.settings-prow2')].find((r) => r.querySelector('.settings-pnote'))!
    expect(row().querySelector('.settings-pnote')!.textContent).toBe('gui.settings.providers.probe_checking')
    expect(row().querySelector('.settings-chip')!.className).toContain('settings-on')
    await act(async () => { answer({ models: [], status: 'invalid_key', error: 'HTTP 401' }) })
    expect(row().querySelector('.settings-pnote')!.textContent).toContain('gui.settings.providers.probe_invalid_saved')
    expect(row().querySelector('.settings-chip')!.className).toContain('settings-warn')
    expect(screen.getByText('gui.settings.providers.probe_recheck')).toBeTruthy()
  })

  it('a verified key says how many models the vendor named', async () => {
    const data = snap()
    install(data, {
      provider: async (_op, params) => {
        data.providers = data.providers.map((p) => (p.id === params.slug ? { ...p, on: true } : p))
        return { ...data }
      },
      fetchModels: async () => ({
        status: 'ok',
        models: [
          { id: 'a', label: 'a', kind: 'text', added: false, source: 'live' },
          { id: 'b', label: 'b', kind: 'text', added: false, source: 'live' },
          { id: 'c', label: 'c', kind: 'text', added: false, source: 'registry' },
        ],
      }),
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: 'sk-1' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    const note = document.querySelector('.settings-pnote')!
    expect(note.getAttribute('data-tone')).toBe('ok')
    expect(note.textContent).toBe('gui.settings.providers.probe_valid {"n":2}')
  })

  it('a live read that succeeds reloads the offer, so the pickers list what the vendor named', async () => {
    const data = snap()
    let status = 'ok'
    const { calls } = install(data, {
      provider: async (_op, params) => {
        data.providers = data.providers.map((p) => (p.id === params.slug ? { ...p, on: true } : p))
        return { ...data }
      },
      fetchModels: async () => ({ status, models: [] }),
      reloadProviders: async () => {
        calls.push(['reloadProviders', null])
        return { ...data, model: 'from-the-reload' }
      },
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: 'sk-1' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls.filter(([m]) => m === 'reloadProviders')).toHaveLength(1)
    expect(store.get().snap.model).toBe('from-the-reload')

    status = 'invalid_key'
    await act(async () => { await store.recheck('openrouter') })
    await act(async () => { await store.sheetOpen('openrouter') })
    expect(calls.filter(([m]) => m === 'reloadProviders')).toHaveLength(1)

    status = 'ok'
    await act(async () => { await store.sheetOpen('openrouter') })
    expect(calls.filter(([m]) => m === 'reloadProviders')).toHaveLength(2)
  })

  it('a key with characters no key has is refused before anything is saved', async () => {
    const { calls } = install(snap())
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: '\u{1F916} Generated with Claude Code' } })
    })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls.find(([m]) => m === 'provider')).toBeUndefined()
    expect(document.querySelector('.settings-padd')).not.toBeNull()
    expect(screen.getByText('gui.settings.providers.key_not_ascii').closest('.settings-padd')).not.toBeNull()
    expect(document.querySelector('.settings-inline-err')).toBeNull()
  })

  it('the data-sync body draws the embedding row alone, without calling it optional', async () => {
    install(snap())
    await openBody(MemoryStepBody)
    const names = [...document.querySelectorAll('.settings-rt')].map((el) => el.firstChild!.textContent)
    expect(names).toEqual(['gui.settings.roles.embedding'])
    expect(document.querySelectorAll('.settings-mpill').length).toBe(1)
    expect(document.querySelector('.settings-opttag')).toBeNull()
  })

  it('a key a public catalogue cannot confirm is said so plainly, with nothing to retry', async () => {
    const data = snap()
    install(data, {
      provider: async (_op, params) => {
        data.providers = data.providers.map((p) => (p.id === params.slug ? { ...p, on: true } : p))
        return { ...data }
      },
      fetchModels: async () => ({ status: 'key_unchecked', models: [] }),
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: '111' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    const note = document.querySelector('.settings-pnote')!
    expect(note.textContent).toBe('gui.settings.providers.probe_unchecked')
    expect(note.getAttribute('data-tone')).toBe('muted')
    expect(screen.queryByText('gui.settings.providers.probe_recheck')).toBeNull()
  })

  it('a failed test checks again on request', async () => {
    const data = snap()
    let status = 'network_error'
    install(data, {
      provider: async (_op, params) => {
        data.providers = data.providers.map((p) => (p.id === params.slug ? { ...p, on: true } : p))
        return { ...data }
      },
      fetchModels: async () => ({ status, models: [] }),
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: 'sk-1' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(document.querySelector('.settings-pnote')!.textContent).toContain('gui.settings.providers.probe_unreachable')
    status = 'ok'
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.probe_recheck')) })
    expect(document.querySelector('.settings-pnote')!.getAttribute('data-tone')).toBe('ok')
  })

  it('a save that fails leaves the form open and tests nothing', async () => {
    const data = snap()
    const { calls } = install(data, {
      provider: async () => { throw { handled: true } },
    })
    await openBody(ModelStepBody)
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('gui.settings.providers.api_key'), { target: { value: 'sk-1' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(document.querySelector('.settings-padd')).not.toBeNull()
    expect(calls.filter(([m]) => String(m).startsWith('fetchModels'))).toEqual([])
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
  it('opens the provider on the catalogue page, where the wizard disconnects in place', async () => {
    /* Was: the settings model page showed the same list with a "manage" button
       that paged into the detail. The catalogue page draws the detail beside
       the list instead, so there is nothing to page to and no manage button --
       the wizard, which has no detail pane, keeps its disconnect. */
    install()
    await mount('provider')
    expect(screen.queryByText('gui.settings.providers.manage')).toBeNull()
    const rows = [...document.querySelectorAll('.settings-tp-row')]
    expect(rows.length).toBeGreaterThan(0)
    /* Two columns: the list, and the provider it opened on. */
    expect(document.querySelector('.settings-tp')!.children.length).toBe(2)
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
