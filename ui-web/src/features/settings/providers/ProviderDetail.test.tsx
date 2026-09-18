// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetSources } from '../../../state/sources'
import { install, mount, snap } from '../harness'
import * as store from '../store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

/* The models card's add button: the first card that offers one. */
const modelsAdd = (): HTMLElement => document.querySelectorAll('.settings-card')[1]!.querySelector('button.mini.ghost') as HTMLElement
const formSave = (): HTMLElement => document.querySelector('.settings-kvform button.mini:not(.ghost)') as HTMLElement

async function open(slug: string): Promise<void> {
  await mount('model')
  await act(async () => { store.set({ provider: slug }) })
}

describe('provider detail', () => {
  it('refuses to disconnect a provider a role runs on, naming the roles, and writes nothing', async () => {
    const { calls } = install()
    await open('anthropic')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent)
      .toBe('gui.settings.providers.in_use {"roles":"gui.settings.roles.chat, gui.settings.roles.title, gui.settings.roles.gate"}')
  })

  it('disconnects a provider no role uses', async () => {
    const data = snap()
    delete (data.raw as { context?: unknown }).context
    data.everos = { available: true, sections: {} }
    const { calls } = install(data)
    await open('openrouter')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect')) })
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

  it('the model sheet fetches the vendor list, takes ticks plus a typed id, and adds them in one call', async () => {
    const { calls } = install(undefined, {
      fetchModels: async (slug) => { calls.push(['fetchModels', slug]); return { models: [{ id: 'gpt-5', label: 'gpt-5', kind: 'chat', added: false }, { id: 'claude-opus-4-5', label: 'x', kind: 'chat', added: true }], status: 'ok' } },
    })
    await open('anthropic')
    await act(async () => { fireEvent.click(modelsAdd()) })
    expect(calls).toEqual([['fetchModels', 'anthropic']])
    await act(async () => { fireEvent.click(screen.getByLabelText('gpt-5', { exact: false }).closest('label')!.querySelector('input')!) })
    const box = document.getElementById('mlq') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'my-finetune' } }) })
    await act(async () => { fireEvent.click(screen.getByText('my-finetune').closest('label')!.querySelector('input')!) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add_n {"n":2}')) })
    expect(calls[1]).toEqual(['addModels', { slug: 'anthropic', models: ['gpt-5', 'my-finetune'] }])
  })

  it('a vendor with no list endpoint says so and still takes a typed id', async () => {
    const { calls } = install(undefined, { fetchModels: async () => ({ models: [], status: 'unsupported' }) })
    await open('anthropic')
    await act(async () => { fireEvent.click(modelsAdd()) })
    expect(screen.getByText('gui.settings.providers.no_list')).toBeTruthy()
    const box = document.getElementById('mlq') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'typed-one' } }) })
    await act(async () => { fireEvent.click(screen.getByText('typed-one').closest('label')!.querySelector('input')!) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.add_n {"n":1}')) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['typed-one'] }])
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
  it('lives in the advanced card and is written on its own through set_fields', async () => {
    const { calls } = install()
    await open('anthropic')
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
