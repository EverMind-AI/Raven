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
  const many = (): ReturnType<typeof snap> => {
    const data = snap()
    const row = data.providers.find((r) => r.id === 'openrouter')!
    row.configured = Array.from({ length: 11 }, (_, i) => `openrouter/vendor/model-${i}`)
    return data
  }
  const chips = (): string[] => [...document.querySelectorAll('.settings-tag2')].map((c) => c.firstChild!.textContent!)

  it('folds a long model list past eight, and a removal does not snap it shut', async () => {
    /* A gateway takes models by the dozen; the list only ever grew, pushing the
       rest of the pane out of reach. */
    install(many())
    await open('openrouter')
    expect(chips()).toHaveLength(8)
    const more = document.querySelector('.settings-tagmore') as HTMLButtonElement
    expect(more.textContent).toBe('gui.settings.providers.models_more {"n":"3"}')
    await act(async () => { fireEvent.click(more) })
    expect(chips()).toHaveLength(11)
    /* A write redraws the page from a new snapshot; the fold lives in the store
       so that redraw keeps it open. */
    await act(async () => { store.set({ epoch: store.get().epoch + 1 }) })
    expect(chips()).toHaveLength(11)
    await act(async () => { fireEvent.click(document.querySelector('.settings-tagmore')!) })
    expect(chips()).toHaveLength(8)
  })

  it('names each chip without the provider in front, keeping the full id as its title', async () => {
    install(many())
    await open('openrouter')
    expect(chips()[0]).toBe('vendor/model-0')
    expect(document.querySelector('.settings-tag2')!.getAttribute('title')).toBe('openrouter/vendor/model-0')
  })

  it('asks a keyless local server for its address once', async () => {
    const data = snap()
    data.providers.push({ id: 'vllm', name: 'vLLM', models: [], configured: [], on: false, kind: 'local',
      acceptsKey: false, needsBase: true })
    install(data)
    await open('vllm')
    expect(screen.getAllByText('gui.settings.providers.base')).toHaveLength(1)
    expect(document.querySelectorAll('.settings-tp-main input[aria-label="gui.settings.providers.base"]')).toHaveLength(1)
  })

  it('leaves the page one way out: the key link when there is one, the vendor site when not', async () => {
    /* The name's arrow used to be `keyUrl || homepage`, the same page as "Get a
       key" two lines below it. */
    install()
    await open('anthropic')
    expect(document.querySelectorAll('.settings-tp-main a.exlink')).toHaveLength(1)
    expect(document.querySelector('.settings-tp-name a')).toBeNull()
    expect(screen.getByText('gui.settings.get_key').closest('a')!.getAttribute('href')).toBe('https://console.anthropic.com/settings/keys')
  })

  it('refuses to disconnect a provider a role runs on, naming the roles, and writes nothing', async () => {
    const { calls } = install()
    await open('anthropic')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect_key')) })
    expect(calls).toEqual([])
    expect(screen.getByRole('alert').textContent)
      .toBe('gui.settings.providers.in_use {"roles":"gui.settings.roles.chat, gui.settings.roles.title, gui.settings.roles.gate"}')
  })

  /* And it says so where the button is. The panel's own foot sits under a grid
     `calc(100vh - 220px)` tall, so a refusal drawn there is a screen below the
     control that refused: the button appeared to do nothing at all. */
  it('says the refusal inside the pane, not at the foot of the panel', async () => {
    install()
    await open('anthropic')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.providers.disconnect_key')) })
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(alerts[0]!.closest('.settings-tp-main')).not.toBeNull()
    expect(document.querySelector('.settings-panel > .settings-inline-err')).toBeNull()
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
    /* The save, then the test it kicks off on the side. */
    expect(calls).toEqual([['provider', { op: 'save_key', slug: 'openai', api_key: 'sk-new' }], ['fetchModels:verify', 'openai']])
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
    expect(calls).toEqual([['provider', { op: 'save_key', slug: 'ollama', api_base: 'http://localhost:11434' }], ['fetchModels:verify', 'ollama']])
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

  const addPicked = (): HTMLButtonElement => document.querySelector('.settings-apfoot .mini.go') as HTMLButtonElement
  const pickAll = (): HTMLElement => document.querySelector('.settings-apall') as HTMLElement

  it('a click ticks a row and writes nothing; the footer adds the ticked set in one call and closes', async () => {
    const calls = await catalogue([
      { id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false },
      { id: 'gpt-5-mini', label: 'gpt-5-mini', kind: 'text', added: false },
      { id: 'o4', label: 'o4', kind: 'text', added: false },
    ])
    expect(calls).toEqual([['fetchModels', 'anthropic']])
    expect(addPicked().disabled).toBe(true)
    await act(async () => { fireEvent.click(popRows()[0]!) })
    await act(async () => { fireEvent.click(popRows()[2]!) })
    expect(popRows().map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'true'])
    expect(calls).toHaveLength(1)
    expect(addPicked().textContent).toBe('gui.settings.providers.add_picked {"n":"2"}')
    /* A second click takes the tick off again. */
    await act(async () => { fireEvent.click(popRows()[2]!) })
    await act(async () => { fireEvent.click(popRows()[1]!) })
    await act(async () => { fireEvent.click(addPicked()) })
    expect(calls[1]).toEqual(['addModels', { slug: 'anthropic', models: ['gpt-5', 'gpt-5-mini'] }])
    expect(document.querySelector('.settings-apop')).toBeNull()
  })

  it('keeps a tick through a search that hides its row', async () => {
    const calls = await catalogue([
      { id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false },
      { id: 'o4', label: 'o4', kind: 'text', added: false },
    ])
    await act(async () => { fireEvent.click(popRows()[0]!) })
    await act(async () => { fireEvent.change(popSearch(), { target: { value: 'o4' } }) })
    await act(async () => { fireEvent.click(popRows()[0]!) })
    expect(addPicked().textContent).toBe('gui.settings.providers.add_picked {"n":"2"}')
    await act(async () => { fireEvent.click(addPicked()) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['gpt-5', 'o4'] }])
  })

  it('counts the kinds over the search result and narrows the rows to the tab', async () => {
    await catalogue([
      { id: 'gpt-5', label: 'gpt-5', kind: 'text', added: false },
      { id: 'gpt-embed', label: 'gpt-embed', kind: 'embedding', added: false },
      { id: 'other-embed', label: 'other-embed', kind: 'embedding', added: false },
    ])
    const tabs = (): string[] => [...document.querySelectorAll('.settings-mkind')].map((b) => b.textContent!)
    /* Three, not five: the harness's anthropic already carries two models, and
       what is already on the provider is not a choice this list offers. */
    expect(tabs()).toEqual(['gui.model.kind_all3', 'gui.model.type.text1', 'gui.model.type.embedding2'])
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

  it('one model written two ways is one model, and the added spelling stays out', async () => {
    /* `model.fetch_models` returns provider-qualified ids; a model added by
       hand is stored as it was typed. The backend calls them the same model
       (`wire.py` merge_key), so the qualified row for a bare configured id is
       a model this provider already has, and offering to add it again is the
       bug the merge key exists to stop. */
    const calls = await catalogue([
      { id: 'anthropic/claude-opus-4-5', label: 'Opus', kind: 'text', added: true },
      { id: 'anthropic/claude-sonnet-4-5', label: 'Sonnet', kind: 'text', added: true },
      { id: 'anthropic/claude-haiku', label: 'Haiku', kind: 'text', added: false },
    ])
    /* The harness's anthropic carries the two bare spellings as configured. */
    expect(popRows().map((r) => r.querySelector('.settings-apnm')!.textContent)).toEqual(['Haiku'])
    await act(async () => { fireEvent.click(pickAll()) })
    await act(async () => { fireEvent.click(addPicked()) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['anthropic/claude-haiku'] }])
  })

  it('drops a model from the list the moment it is added, and says so when none is left', async () => {
    /* The whole point of the list is what is not on the provider yet. A vendor
       every one of whose models is already added used to open as a page of
       ticks with nothing to press, and the footer offered "add all (0)". */
    const calls = await catalogue([
      { id: 'claude-opus-4-5', label: 'Opus', kind: 'text', added: true },
      { id: 'claude-sonnet-4-5', label: 'Sonnet', kind: 'text', added: true },
    ])
    expect(popRows()).toHaveLength(0)
    expect(screen.getByText('gui.settings.providers.all_added')).toBeTruthy()
    expect(screen.queryByText('gui.settings.providers.no_models_yet')).toBeNull()
    expect(document.querySelector('.settings-apfoot .mini')).toBeNull()
    expect(document.querySelector('.settings-apall')).toBeNull()
    expect(calls.some((c) => c[0] === 'provider')).toBe(false)
  })

  it('"select all" ticks what is shown and writes nothing; the add is still the one write', async () => {
    /* It replaced an "add all" button: a second way to write, beside a list
       that is otherwise built by ticking and committed by one button. */
    const calls = await catalogue([
      { id: 'a', label: 'a', kind: 'text', added: false },
      { id: 'b', label: 'b', kind: 'text', added: false },
      { id: 'zz', label: 'zz', kind: 'text', added: false },
    ])
    expect(document.querySelectorAll('.settings-apfoot .mini')).toHaveLength(1)
    expect(pickAll().getAttribute('aria-checked')).toBe('false')
    await act(async () => { fireEvent.click(popRows()[0]!) })
    expect(pickAll().getAttribute('aria-checked')).toBe('mixed')
    await act(async () => { fireEvent.click(pickAll()) })
    expect(pickAll().getAttribute('aria-checked')).toBe('true')
    expect(calls).toHaveLength(1)
    /* Over what is shown: a search narrows what it takes. */
    await act(async () => { fireEvent.click(pickAll()) })
    await act(async () => { fireEvent.change(popSearch(), { target: { value: 'z' } }) })
    await act(async () => { fireEvent.click(pickAll()) })
    await act(async () => { fireEvent.click(addPicked()) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['zz'] }])
  })

  it('heads each vendor group and trails the bare rows with none', async () => {
    await catalogue([
      { id: 'zeta/one', label: 'Zeta One', kind: 'text', added: false },
      { id: 'alpha/two', label: 'Alpha Two', kind: 'text', added: false },
      { id: 'alpha/three', label: 'Alpha Three', kind: 'text', added: true },
    ])
    const heads = [...document.querySelectorAll('.settings-mgroup')]
    expect(heads.map((g) => g.querySelector('.settings-gn')!.textContent)).toEqual(['alpha', 'zeta'])
    /* One each: `alpha/three` is on the provider already, and the harness's two
       bare configured ids are too, so neither the group counts nor the rows
       carry them. */
    expect(heads.map((g) => g.querySelector('.settings-gc')!.textContent)).toEqual(['1', '1'])
    expect(popRows().map((r) => r.querySelector('.settings-apnm')!.textContent))
      .toEqual(['Alpha Two', 'Zeta One'])
    expect(document.querySelector('.settings-ga')).toBeNull()
  })

  it('names a row by its display name, or by its id without the group prefix', async () => {
    /* A hand-typed model has no display name, so it fell back to the raw id --
       `deepseek/1111` under a head that already says deepseek, beside rows
       named "DeepSeek V4 Flash": one vendor, spelled twice in two cases. */
    await catalogue([
      { id: 'deepseek/deepseek-v4-pro', label: 'DeepSeek V4 Pro', kind: 'text', added: false },
      { id: 'deepseek/1111', label: 'deepseek/1111', kind: 'text', added: false },
      { id: 'bare-one', label: 'bare-one', kind: 'text', added: false },
    ])
    const names = popRows().map((r) => r.querySelector('.settings-apnm')!.textContent)
    expect(names).toEqual(['DeepSeek V4 Pro', '1111', 'bare-one'])
    /* The full id stays on the row for anyone who needs it. */
    expect(popRows()[1]!.getAttribute('title')).toBe('deepseek/1111')
  })

  it('groups a gateway\'s qualified ids by vendor, not under the gateway\'s own name', async () => {
    /* A live gateway list writes every id with its own name in front --
       `openrouter/anthropic/claude-opus-5.5` -- and grouping on the first
       segment filed all of them under one head named after the provider the
       popover already belongs to. The harness provider here is anthropic. */
    await catalogue([
      { id: 'anthropic/meta/llama-4', label: 'anthropic/meta/llama-4', kind: 'text', added: false },
      { id: 'anthropic/qwen/qwen3-max', label: 'qwen/qwen3-max', kind: 'text', added: false },
    ])
    const heads = [...document.querySelectorAll('.settings-mgroup .settings-gn')].map((g) => g.textContent)
    expect(heads).toEqual(['meta', 'qwen'])
    expect(popRows().map((r) => r.querySelector('.settings-apnm')!.textContent)).toEqual(['llama-4', 'qwen3-max'])
  })

  it('a vendor head is that vendor\'s select-all: none, some, all', async () => {
    /* It replaced a bare "+" at the head's far edge that wrote the whole vendor
       at once -- a second, differently committing way to add, beside rows
       that tick. */
    const calls = await catalogue([
      { id: 'alpha/one', label: 'A1', kind: 'text', added: false },
      { id: 'alpha/two', label: 'A2', kind: 'text', added: false },
      { id: 'zeta/one', label: 'Z1', kind: 'text', added: false },
    ])
    const head = (): HTMLElement => document.querySelectorAll<HTMLElement>('.settings-mgroup')[0]!
    expect(head().getAttribute('aria-checked')).toBe('false')
    await act(async () => { fireEvent.click(popRows()[0]!) })
    expect(head().getAttribute('aria-checked')).toBe('mixed')
    await act(async () => { fireEvent.click(head()) })
    expect(head().getAttribute('aria-checked')).toBe('true')
    expect(popRows().map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'true', 'false'])
    expect(calls).toHaveLength(1)
    await act(async () => { fireEvent.click(head()) })
    expect(popRows().map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'false', 'false'])
    await act(async () => { fireEvent.click(head()) })
    await act(async () => { fireEvent.click(addPicked()) })
    expect(calls[calls.length - 1]).toEqual(['addModels', { slug: 'anthropic', models: ['alpha/one', 'alpha/two'] }])
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
