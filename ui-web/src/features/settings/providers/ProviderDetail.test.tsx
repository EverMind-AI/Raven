// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'

import type { Call } from '../../../test/settingsHarness'
import type { ModelCandidate } from '../types'

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

/* The models section's own add button, by the attribute it carries. */
const modelsAdd = (): HTMLElement => document.querySelector('[data-addmodel]') as HTMLElement
const formSave = (): HTMLElement => document.querySelector('.settings-kvform button.mini:not(.ghost)') as HTMLElement

/* Advanced is a fold now, closed on arrival: an address override, the request
   headers and the display names are not what a reader opened the page for. */
async function openAdv(): Promise<void> {
  await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.advanced')) })
}

/* The detail pane is the Model providers page's right column since the split;
   the Model settings page holds the roles card alone. */
async function open(slug: string): Promise<void> {
  await mount('provider')
  await act(async () => { store.set({ provider: slug }) })
}

describe('provider detail', () => {
  it('refuses to disconnect a provider a role runs on, naming the roles, and writes nothing', async () => {
    const { calls } = install()
    await open('anthropic')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect_key')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent)
      .toBe('gui.settings.providers.in_use {"roles":"gui.settings.roles.chat, gui.settings.roles.title, gui.settings.roles.gate"}')
  })

  /* And it stands at the foot, not among the fields: the box a reader is
     typing a key into should not have "disconnect and clear the key" under it. */
  it('keeps disconnect at the foot of the pane, away from the key box', async () => {
    install()
    await open('openrouter')
    expect(document.querySelector('.settings-tp-foot')!.textContent)
      .toBe('gui.settings.providers.disconnect_key')
    const secs = [...document.querySelectorAll('.settings-sec')]
    expect(secs.some((sec) => sec.textContent?.includes('gui.settings.providers.disconnect_key'))).toBe(false)
  })

  it('disconnects a provider no role uses', async () => {
    const data = snap()
    delete (data.raw as { context?: unknown }).context
    data.everos = { available: true, sections: {} }
    const { calls } = install(data)
    await open('openrouter')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect_key')) })
    expect(calls).toEqual([['provider', { op: 'disconnect', slug: 'openrouter' }]])
  })

  it('refuses to remove the model a role uses and removes one nobody does', async () => {
    const { calls } = install()
    await open('anthropic')
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.providers.remove_model {"model":"claude-opus-4-5"}')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent).toContain('gui.settings.roles.chat')
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.providers.remove_model {"model":"claude-sonnet-4-5"}')) })
    expect(calls).toEqual([['provider', { op: 'remove_model', slug: 'anthropic', model: 'claude-sonnet-4-5' }]])
  })

  it('words the disconnect by shape, so it says what it is about to drop', async () => {
    const data = snap()
    data.providers = data.providers.map((p) => (
      p.id === 'minimax_global' || p.id === 'ollama' ? { ...p, on: true } : p
    ))
    install(data)
    await open('minimax_global')
    expect(screen.getByText('gui.settings.providers.disconnect_auth')).toBeTruthy()
    await open('ollama')
    expect(screen.getByText('gui.settings.providers.disconnect_local')).toBeTruthy()
    await open('anthropic')
    expect(screen.getByText('gui.settings.providers.disconnect_key')).toBeTruthy()
  })

  it('re-entering a key connects with the key alone, and an empty key on a new provider is refused first', async () => {
    const { calls } = install()
    await open('openai')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent).toBe('gui.settings.providers.key_first')
    const box = screen.getByLabelText('gui.settings.providers.api_key') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'sk-new' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls).toEqual([['provider', { op: 'save_key', slug: 'openai', api_key: 'sk-new' }]])
  })

  it('a local provider connects by address alone and refuses an empty one', async () => {
    const { calls } = install()
    await open('ollama')
    const box = screen.getByLabelText('gui.settings.providers.base') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: '' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls).toEqual([])
    await act(async () => { fireEvent.change(box, { target: { value: 'http://localhost:11434' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.connect')) })
    expect(calls).toEqual([['provider', { op: 'save_key', slug: 'ollama', api_base: 'http://localhost:11434' }]])
  })

  it('a header is added as a one-name patch and removed as a one-name null', async () => {
    const { calls } = install()
    await open('openrouter')
    await openAdv()
    expect(screen.getByText('X-Title')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.providers.remove_header {"name":"X-Title"}')) })
    expect(calls).toEqual([['setFields', { slug: 'openrouter', fields: { extra_headers: { 'X-Title': null } } }]])
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add_another')) })
    await act(async () => {
      fireEvent.change(screen.getByPlaceholderText('gui.settings.providers.header_name_ph'), { target: { value: 'APP-Code' } })
      fireEvent.change(screen.getByPlaceholderText('gui.settings.providers.header_value_ph'), { target: { value: 'abc' } })
    })
    await act(async () => { fireEvent.click(formSave()) })
    expect(calls).toEqual([['setFields', { slug: 'openrouter', fields: { extra_headers: { 'APP-Code': 'abc' } } }]])
  })

  it('a display name is written through add_model with label and description, and cleared with empty ones', async () => {
    const { calls } = install()
    await open('anthropic')
    await openAdv()
    expect(screen.getByText('Opus · the big one')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.providers.remove_label {"model":"claude-opus-4-5"}')) })
    expect(calls).toEqual([['provider', { op: 'add_model', slug: 'anthropic', model: 'claude-opus-4-5', label: '', description: '' }]])
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add_another')) })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('gui.settings.providers.model_id'), { target: { value: 'claude-sonnet-4-5' } })
      fireEvent.change(screen.getByPlaceholderText('gui.settings.providers.label_ph'), { target: { value: 'Sonnet 4.5' } })
      fireEvent.change(screen.getByPlaceholderText('gui.settings.providers.description_ph'), { target: { value: 'fast' } })
    })
    await act(async () => { fireEvent.click(formSave()) })
    expect(calls).toEqual([['provider', { op: 'add_model', slug: 'anthropic', model: 'claude-sonnet-4-5', label: 'Sonnet 4.5', description: 'fast' }]])
  })

  /* The popover that replaced the tick-then-confirm sheet: a click on a row is
     the write, and the two batch controls are what is left of collecting. */
  const popSearch = (): HTMLInputElement => document.querySelector('.settings-apop input') as HTMLInputElement
  /* The catalogue rows; the typed-id row shares their shape and is reached
     through `.settings-apadd` instead. */
  const popRows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.settings-apm:not(.settings-apadd)')]
  const catalogue = async (models: ModelCandidate[], status = 'ok'): Promise<Call[]> => {
    const { calls } = install(undefined, {
      fetchModels: async (slug) => { calls.push(['fetchModels', slug]); return { models, status } },
    })
    await open('anthropic')
    await act(async () => { fireEvent.click(modelsAdd()) })
    return calls
  }

  it('fetches the vendor list on open and writes one model per click', async () => {
    const calls = await catalogue([
      { id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false },
      { id: 'claude-opus-4-5', label: 'x', kind: 'text', added: true },
    ])
    expect(calls).toEqual([['fetchModels', 'anthropic']])
    await act(async () => { fireEvent.click(popRows()[0]!) })
    expect(calls[1]).toEqual(['provider', { op: 'add_model', slug: 'anthropic', model: 'gpt-5' }])
  })

  it('counts the kinds over the search result and narrows the rows to the tab', async () => {
    await catalogue([
      { id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false },
      { id: 'gpt-embed', label: 'gpt-embed', kind: 'embedding', added: false },
      { id: 'other-embed', label: 'other-embed', kind: 'embedding', added: false },
    ])
    const tabs = (): string[] => [...document.querySelectorAll('.settings-mkind')].map((b) => b.textContent!)
    /* Five, not three: the two models already on this provider are rows too,
       or a model added by hand could never be taken off from here. */
    expect(tabs()).toEqual(['gui.model.kind_all5', 'gui.model.type.text3', 'gui.model.type.embedding2'])
    await act(async () => { fireEvent.change(popSearch(), { target: { value: 'gpt' } }) })
    expect(tabs()).toEqual(['gui.model.kind_all2', 'gui.model.type.text1', 'gui.model.type.embedding1'])
    await act(async () => { fireEvent.click([...document.querySelectorAll('.settings-mkind')][2]!) })
    expect(popRows().map((r) => r.querySelector('.settings-apnm')!.textContent)).toEqual(['gpt-embed'])
  })

  it('a typed id carries a kind chip that cycles, and states its tags on the add', async () => {
    const calls = await catalogue([{ id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false }])
    await act(async () => { fireEvent.change(popSearch(), { target: { value: 'my-team/bge-x' } }) })
    const chip = (): HTMLElement => document.querySelector('.settings-apkind') as HTMLElement
    expect(chip().textContent).toBe('gui.model.type.embedding')
    await act(async () => { fireEvent.click(chip()) })
    expect(chip().textContent).toBe('gui.model.type.reranker')
    await act(async () => { fireEvent.click(document.querySelector('.settings-apadd')!) })
    expect(calls[calls.length - 1]).toEqual(['provider', {
      op: 'add_model', slug: 'anthropic', model: 'my-team/bge-x',
      capabilities: ['rerank'], output_modalities: ['text'],
    }])
  })

  it('a vendor with no list endpoint says so and still takes a typed id', async () => {
    const calls = await catalogue([], 'unsupported')
    expect(screen.getByText('gui.settings.providers.no_list')).toBeTruthy()
    await act(async () => { fireEvent.change(popSearch(), { target: { value: 'typed-one' } }) })
    await act(async () => { fireEvent.click(document.querySelector('.settings-apadd')!) })
    expect(calls[calls.length - 1]).toEqual(['provider', { op: 'add_model', slug: 'anthropic', model: 'typed-one' }])
  })

  it('treats one model written two ways as one row, already added', async () => {
    /* `model.fetch_models` returns provider-qualified ids; a model added by
       hand is stored as it was typed. The backend calls them the same model
       (`wire.py` merge_key); drawing them as two rows offered to add one that
       was already there and left "add all" at a number it could never reach. */
    const calls = await catalogue([
      { id: 'anthropic/claude-opus-4-5', label: 'Opus', kind: 'text', added: true },
      { id: 'anthropic/claude-sonnet-4-5', label: 'Sonnet', kind: 'text', added: true },
      { id: 'anthropic/claude-haiku', label: 'Haiku', kind: 'text', added: false },
    ])
    /* The harness's anthropic carries the two bare spellings as configured. */
    expect(popRows().map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'true', 'false'])
    expect(popRows()).toHaveLength(3)
    expect(screen.getByText('gui.model.add_all {"n":"1"}')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.model.add_all {"n":"1"}')) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['anthropic/claude-haiku'] }])
  })

  it('adds everything shown in one call', async () => {
    const calls = await catalogue([
      { id: 'a', label: 'a', kind: 'text', added: false },
      { id: 'b', label: 'b', kind: 'text', added: false },
    ])
    await act(async () => { fireEvent.click(screen.getByText('gui.model.add_all {"n":"2"}')) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['a', 'b'] }])
  })

  it('heads each vendor group and trails the bare rows with none', async () => {
    const calls = await catalogue([
      { id: 'zeta/one', label: 'Zeta One', kind: 'text', added: false },
      { id: 'alpha/two', label: 'Alpha Two', kind: 'text', added: false },
      { id: 'alpha/three', label: 'Alpha Three', kind: 'text', added: true },
    ])
    const heads = [...document.querySelectorAll('.settings-mgroup')]
    expect(heads.map((g) => g.querySelector('.settings-gn')!.textContent)).toEqual(['alpha', 'zeta'])
    expect(heads.map((g) => g.querySelector('.settings-gc')!.textContent)).toEqual(['2', '1'])
    /* The harness's anthropic carries two bare configured ids. They trail the
       labelled groups and are drawn without a head of their own: a bare row
       between two vendor heads reads as the previous vendor's tail, and an
       "add this whole vendor" control over rows with no vendor means nothing. */
    expect(popRows().map((r) => r.querySelector('.settings-apnm')!.textContent))
      .toEqual(['Alpha Two', 'Alpha Three', 'Zeta One', 'claude-opus-4-5', 'claude-sonnet-4-5'])
    /* The group's control offers the group, not the page: `alpha/three` is
       already on the provider and stays out of the frame. */
    await act(async () => { fireEvent.click(heads[0]!.querySelector('.settings-ga')!) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['alpha/two'] }])
  })

  it('an OAuth provider authorizes in the browser and shows the code until it lands or expires', async () => {
    vi.useFakeTimers()
    const { calls } = install(undefined, { oauthLogin: async (slug) => { calls.push(['oauthLogin', slug]); return { verification_uri: 'https://v.example/device', user_code: 'ABCD', expires_in: 1 } } })
    await open('minimax_global')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.auth_browser')) })
    expect(calls).toEqual([['oauthLogin', 'minimax_global']])
    expect(screen.getByText('gui.settings.providers.oauth_code {"code":"ABCD"}', { exact: false })).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(store.OAUTH_POLL_MS * 11) })
    expect(screen.getByText('gui.settings.providers.oauth_expired')).toBeTruthy()
    vi.useRealTimers()
  })
})

describe('provider detail, the address of a direct vendor', () => {
  it('lives behind the advanced fold and is written on its own through set_fields', async () => {
    const { calls } = install()
    await open('anthropic')
    /* And nowhere else: an override of the registry's own address is not a
       field the page shows until it is asked for. */
    expect(screen.queryAllByLabelText('gui.settings.providers.base')).toHaveLength(0)
    await openAdv()
    const boxes = screen.getAllByLabelText('gui.settings.providers.base') as HTMLInputElement[]
    expect(boxes).toHaveLength(1)
    await act(async () => { fireEvent.change(boxes[0]!, { target: { value: 'https://proxy.example/v1' } }); fireEvent.keyDown(boxes[0]!, { key: 'Enter' }) })
    expect(calls).toEqual([['setFields', { slug: 'anthropic', fields: { api_base: 'https://proxy.example/v1' } }]])
  })

  it('lists only the names a person stated, never the catalogue\'s own', async () => {
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, labels: { 'openai/gpt-4o': { label: 'GPT-4o' } } } : p))
    install(data)
    await open('openrouter')
    await openAdv()
    expect(screen.queryByText('GPT-4o')).toBeNull()
  })
})

describe('provider detail, Azure', () => {
  it('shows the stored deployment and API version from the config section', async () => {
    const data = snap()
    data.providers = [...data.providers, { id: 'azure_openai', name: 'Azure OpenAI', models: [], configured: [], on: true, kind: 'endpoint', acceptsKey: true, needsBase: true }]
    ;(data.raw.providers as Record<string, unknown>).azure_openai = { apiBase: 'https://acme.openai.azure.com', deployment: 'gpt-4o-eu', apiVersion: '2024-10-21' }
    install(data)
    await open('azure_openai')
    expect((screen.getByLabelText('gui.settings.providers.deployment') as HTMLInputElement).value).toBe('gpt-4o-eu')
    expect((screen.getByLabelText('gui.settings.providers.api_version') as HTMLInputElement).value).toBe('2024-10-21')
    expect((screen.getByLabelText('gui.settings.providers.base') as HTMLInputElement).value).toBe('https://acme.openai.azure.com')
  })
})
