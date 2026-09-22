// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { ROLES, everosLocked, roleProviders, roleValue, rolesUsing } from './Roles'

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

const role = (id: string) => ROLES.find((r) => r.id === id)!
const pill = (roleName: string): HTMLElement => screen.getByLabelText(`gui.settings.roles.change {"role":"${roleName}"}`)
/* One picker for the whole page since 2026-09-20: the composer's, at the body. */
const picker = (): HTMLElement => document.querySelector('.mpick') as HTMLElement
/* The picker is one list grouped by provider, so a model is reached through
   its group: the named provider's where one is given, the first group listing
   it otherwise. */
const group = (name: string): HTMLElement =>
  [...picker().querySelectorAll<HTMLElement>('.model-group')]
    .find((g) => g.querySelector('.model-group-hd .nm')?.textContent?.includes(name))!
const pick = async (roleName: string, model: string, providerName?: string): Promise<void> => {
  await act(async () => { fireEvent.click(pill(roleName)) })
  const scope = providerName ? group(providerName) : picker()
  await act(async () => { fireEvent.click(within(scope).getAllByText(model)[0]!) })
}
const sets = (calls: Array<[string, unknown]>) => calls.filter(([m]) => m === 'set').map(([, a]) => a)

describe('model roles', () => {
  it('a keyed role writes its model then its provider; clearing writes null to both', async () => {
    const { calls } = install()
    await mount('model')
    expect(pill('gui.settings.roles.curator').textContent).toContain('anthropic/claude-sonnet-4-5')
    await pick('gui.settings.roles.title', 'claude-sonnet-4-5')
    expect(sets(calls)).toEqual([
      { key: 'sessionTitle.model', value: 'claude-sonnet-4-5' },
      { key: 'sessionTitle.provider', value: 'anthropic' },
    ])
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.roles.clear {"role":"gui.settings.roles.curator"}')) })
    expect(sets(calls)).toEqual([
      { key: 'context.curatorModel', value: null },
      { key: 'context.curatorProvider', value: null },
    ])
  })

  it('the chat role writes through pickModel and reloads', async () => {
    const { calls } = install()
    await mount('model')
    await pick('gui.settings.roles.chat', 'claude-sonnet-4-5')
    expect(calls).toEqual([['pickModel', { model: 'claude-sonnet-4-5', provider: 'anthropic' }]])
  })

  it('a chat pick the gateway cannot chat on yet sets the restart flag', async () => {
    /* A first run: the write landed and the process has no loop to serve it.
       The flag is what the onboarding wizard's frame reads to say so. */
    install(snap(), { pickModel: async () => true })
    await mount('model')
    expect(store.get().needsRestart).toBe(false)
    await pick('gui.settings.roles.chat', 'claude-sonnet-4-5')
    expect(store.get().needsRestart).toBe(true)
  })

  it('a media role offers OpenRouter only, writes the selection first and then unhides the tool', async () => {
    const { calls } = install()
    await mount('model')
    expect(roleProviders(role('image'), snap()).map((p) => p.id)).toEqual(['openrouter'])
    /* The image slot lists image models: a text model on the same provider is
       not offered for it any more. */
    await pick('gui.settings.roles.image', 'gemini-2.5-flash-image')
    expect(sets(calls)).toEqual([
      { key: 'tools.media.image', value: { model: 'google/gemini-2.5-flash-image', quality: '' } },
      { key: 'tools.disabledTools', value: ['deep_research'] },
    ])
  })

  it('clearing a media role empties the model, keeps the quality, and puts the tool back on the disabled list', async () => {
    const data = snap()
    ;(data.raw.tools as { media?: unknown; disabledTools: string[] }).media = { speech: { model: 'x/tts', quality: '' } }
    const { calls } = install(data)
    await mount('model')
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.roles.clear {"role":"gui.settings.roles.speech"}')) })
    expect(sets(calls)).toEqual([
      { key: 'tools.media.speech', value: { model: '', quality: '' } },
      { key: 'tools.disabledTools', value: ['image_generate', 'deep_research', 'text_to_speech'] },
    ])
  })

  it('a media role with OpenRouter not connected offers to connect it instead of a picker', async () => {
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, on: false } : p))
    install(data)
    await mount('model')
    expect(screen.getAllByText('gui.settings.roles.connect_openrouter').length).toBeGreaterThan(0)
  })

  it('an EverOS role offers only vendors that serve it, and writes the pair', async () => {
    /* Anthropic is connected and holds a key, and used to be offered here for
       every role. It serves no embeddings, which is what the slot has to ask
       about -- a key was never the question. */
    const { calls } = install()
    await mount('model')
    expect(roleProviders(role('embedding'), snap()).map((p) => p.id)).toEqual(['openrouter'])
    expect(pill('gui.settings.roles.memllm').textContent).toContain('openai/gpt-4o')
    await pick('gui.settings.roles.embedding', 'text-embedding-3-small', 'OpenRouter')
    expect(calls).toEqual([
      ['everosSet', { section: 'embedding', model: 'text-embedding-3-small', provider: 'openrouter' }],
    ])
  })

  it('a self-hosted endpoint is a vendor here like any other', async () => {
    /* The reason the slot stopped asking about auth shape: somebody's own box
       is `local`, never `key`, so the old filter hid every one of them -- and a
       role pinned to a vendor is the only way one can be recorded at all. */
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'ollama' ? { ...p, on: true } : p))
    expect(roleProviders(role('memllm'), data).map((p) => p.id)).toEqual(['anthropic', 'openrouter', 'ollama'])
  })

  it('the rerank slot offers a self-hosted box only once something knows its shape', async () => {
    /* Reranking against somebody's own server needs a request shape no table
       can name, and this page has nowhere to ask for one -- the write refuses a
       first save of exactly those providers. Offering it anyway is a picker
       entry whose only outcome is an error toast. The wizard asks, so a
       configured one stays pickable and its model stays editable. */
    const fresh = snap()
    fresh.providers = fresh.providers.map((p) => (p.id === 'ollama' ? { ...p, on: true } : p))
    expect(roleProviders(role('rerank'), fresh).map((p) => p.id)).toEqual(['openrouter'])

    const configured = snap()
    configured.providers = configured.providers.map((p) => (p.id === 'ollama' ? { ...p, on: true } : p))
    configured.everos = {
      ...configured.everos,
      sections: { rerank: { model: 'bge-reranker', provider: 'ollama', api_key_set: true } },
    }
    expect(roleProviders(role('rerank'), configured).map((p) => p.id)).toEqual(['openrouter', 'ollama'])
  })

  it('a required role has no clear control', async () => {
    /* The server refuses to clear llm and embedding -- one turns long-term
       memory off outright, the other is what every stored vector was written
       under. The button was drawn anyway, and its only result was an error. */
    const data = snap()
    data.everos = {
      ...data.everos,
      sections: {
        llm: { model: 'openai/gpt-4o', provider: 'openrouter', api_key_set: true },
        embedding: { model: 'text-embedding-3-small', provider: 'openrouter', api_key_set: true },
        rerank: { model: 'r', provider: 'openrouter', api_key_set: true },
      },
    }
    install(data)
    await mount('model')

    const clearLabel = (id: string) => `gui.settings.roles.clear {"role":"gui.settings.roles.${id}"}`
    expect(screen.queryByLabelText(clearLabel('memllm'))).toBe(null)
    expect(screen.queryByLabelText(clearLabel('embedding'))).toBe(null)
    expect(screen.queryByLabelText(clearLabel('rerank'))).not.toBe(null)
  })

  it('the slot shows the vendor as stored, with no address to match', async () => {
    /* The page used to name the vendor by comparing the section's address with
       every provider's. A self-hosted endpoint matches none of them, so the
       slot went blank for exactly the case that needed it most. */
    const data = snap()
    data.everos = { ...data.everos, sections: { llm: { model: 'qwen3-8b', provider: 'ollama', api_key_set: true } } }
    expect(roleValue(role('memllm'), data)).toEqual({ model: 'qwen3-8b', provider: 'ollama' })
  })

  it('a locked slot draws its value with nothing to click', async () => {
    /* The function answering "locked" is not the promise -- the promise is that
       the click is gone. A disabled-looking pill that still opens the picker
       takes a save raven cannot apply and reports it as done. */
    const data = snap()
    data.everos = { ...data.everos, owned: false }
    install(data)
    await mount('model')
    const shown = screen.getAllByTitle('gui.settings.roles.locked_foreign')
    expect(shown.length).toBe(4)
    for (const el of shown) {
      expect(el.tagName).not.toBe('BUTTON')
      expect(el.querySelector('button')).toBe(null)
    }
    expect(shown.map((el) => el.textContent).join(' ')).toContain('openai/gpt-4o')
  })

  it('a root the user manages locks the slots, and exported variables lock one', async () => {
    const foreign = snap()
    foreign.everos = { ...foreign.everos, owned: false }
    expect(everosLocked(role('memllm'), foreign)).toBe('foreign')

    const exported = snap()
    exported.everos = {
      ...exported.everos,
      sections: { rerank: { model: 'm', provider: 'openrouter', api_key_set: true, env_managed: true } },
    }
    expect(everosLocked(role('rerank'), exported)).toBe('env')
    expect(everosLocked(role('memllm'), exported)).toBe(null)
    expect(everosLocked(role('gate'), foreign)).toBe(null)
  })

  it('a typed id is added to the provider before the role names it', async () => {
    const { calls } = install()
    await mount('model')
    await act(async () => { fireEvent.click(pill('gui.settings.roles.gate')) })
    const box = screen.getByPlaceholderText('gui.picker.search_ph') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'claude-haiku-4-5' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.model.pick_use {"id":"claude-haiku-4-5"}')) })
    expect(calls).toEqual([
      ['provider', { op: 'add_model', slug: 'anthropic', model: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateModel', value: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateProvider', value: 'anthropic' }],
    ])
  })

  it('a slot with no provider offers the providers page and goes there', async () => {
    /* A fresh install starts here: every slot empty, and the way out has to be
       reachable. A sentence naming the page is not. */
    const data = snap()
    data.providers = data.providers.map((p) => ({ ...p, on: false }))
    install(data)
    await mount('model')
    const out = screen.getAllByText('gui.settings.roles.no_provider')[0]!
    expect(out.tagName).toBe('BUTTON')
    await act(async () => { fireEvent.click(out) })
    expect(store.get().tab).toBe('provider')
  })

  it('a media slot with OpenRouter off opens that row on the providers page', async () => {
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, on: false } : p))
    install(data)
    await mount('model')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.roles.connect_openrouter')[0]!) })
    expect([store.get().tab, store.get().provider]).toEqual(['provider', 'openrouter'])
  })

  it('a typed id keeps the kind the chip stated, not the one its name implies', async () => {
    /* "our-finetune" matches neither name pattern, so without the chip's answer
       it is stored as text and disappears from the embedding slot it was just
       typed into. */
    const { calls } = install()
    await mount('model')
    await act(async () => { fireEvent.click(pill('gui.settings.roles.embedding')) })
    const box = screen.getByPlaceholderText('gui.picker.search_ph') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'our-finetune' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.model.pick_use {"id":"our-finetune"}')) })
    expect(calls[0]).toEqual(['provider', {
      op: 'add_model', slug: 'openrouter', model: 'our-finetune',
      capabilities: ['embedding'], output_modalities: ['vector'],
    }])
  })

  it('rolesUsing counts a role following the chat model through the chat provider', () => {
    const data = snap()
    expect(rolesUsing(data, 'anthropic').map((r) => r.id)).toEqual(['chat', 'title', 'gate'])
    expect(rolesUsing(data, 'openrouter').map((r) => r.id)).toEqual(['curator', 'memllm'])
    expect(rolesUsing(data, 'anthropic', 'claude-sonnet-4-5')).toEqual([])
    expect(rolesUsing(data, 'openrouter', 'openai/gpt-4o').map((r) => r.id)).toEqual(['memllm'])
  })

  it('the chat parameters write effort and iterations, and refuse a cap outside 1-200 before any write', async () => {
    const { calls } = install()
    await mount('model')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.params')) })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.effort_high')) })
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.more')) })
    expect(sets(calls)).toEqual([
      { key: 'agents.defaults.reasoningEffort', value: 'high' },
      { key: 'agents.defaults.maxToolIterations', value: 41 },
    ])
    cleanup()
    store._resetForTests()
    const capped = snap()
    ;(capped.raw.agents as { defaults: Record<string, unknown> }).defaults.maxToolIterations = 200
    const second = install(capped)
    await mount('model')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.params')) })
    await act(async () => { fireEvent.click(screen.getByLabelText('gui.settings.more')) })
    expect(second.calls).toEqual([])
    expect(screen.getByRole('alert').textContent).toBe('gui.settings.roles.max {"n":200}')
  })

  it('a fixed context window below 1024 is refused; above the model window it saves and warns', async () => {
    const { calls } = install()
    await mount('model')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.params')) })
    expect(screen.getByText('200,000 tok')).toBeTruthy()
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.ctx_pin')) })
    expect(sets(calls)).toEqual([{ key: 'agents.defaults.contextWindowTokens', value: 200000 }])
    cleanup()
    store._resetForTests()
    const pinned = snap()
    ;(pinned.raw.agents as { defaults: Record<string, unknown> }).defaults.contextWindowTokens = 300000
    const second = install(pinned)
    await mount('model')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.roles.params')) })
    expect(screen.getByText('gui.settings.roles.ctx_over {"n":"200,000"}')).toBeTruthy()
    const box = screen.getByLabelText('gui.settings.roles.ctx_fixed') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: '512' } }); fireEvent.keyDown(box, { key: 'Enter' }) })
    expect(second.calls).toEqual([])
    expect(screen.getByRole('alert').textContent).toBe('gui.settings.roles.ctx_min {"n":1024}')
  })
})
