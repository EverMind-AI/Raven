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
      { key: 'tools.disabledTools', value: ['write_file'] },
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
      { key: 'tools.disabledTools', value: ['image_generate', 'write_file', 'text_to_speech'] },
    ])
  })

  it('a media role with OpenRouter not connected offers to connect it instead of a picker', async () => {
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, on: false } : p))
    install(data)
    await mount('model')
    expect(screen.getAllByText('gui.settings.roles.connect_openrouter').length).toBeGreaterThan(0)
  })

  it('an EverOS role offers every connected vendor, and writes the pair', async () => {
    /* Which models a vendor serves is the vendor's to say: a catalogue that
       lags it is no reason to refuse the slot, and an id can be typed. The
       fixture's table leaves Anthropic out of embedding; it is offered anyway. */
    const { calls } = install()
    await mount('model')
    expect(roleProviders(role('embedding'), snap()).map((p) => p.id)).toEqual(['anthropic', 'openrouter'])
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
    const box = screen.getByPlaceholderText('gui.picker.search_or_type_ph') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'claude-haiku-4-5' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.model.pick_use {"id":"claude-haiku-4-5"}')) })
    expect(calls).toEqual([
      ['provider', { op: 'add_model', slug: 'anthropic', model: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateModel', value: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateProvider', value: 'anthropic' }],
    ])
  })

  it('a pick shows in the slot before the write answers, and a refused one is put back', async () => {
    let refuse: (e: unknown) => void = () => {}
    install(snap(), { set: () => new Promise((_res, rej) => { refuse = rej }) })
    await mount('model')
    await pick('gui.settings.roles.title', 'claude-sonnet-4-5')
    expect(pill('gui.settings.roles.title').textContent).toContain('claude-sonnet-4-5')
    await act(async () => { refuse({ handled: true }) })
    expect(pill('gui.settings.roles.title').textContent).not.toContain('claude-sonnet-4-5')
  })

  it('a slot with no provider offers the providers page and goes there', async () => {
    /* A fresh install starts here: every slot empty, and the way out has to be
       reachable. A sentence naming the page is not. */
    const data = snap()
    data.providers = data.providers.map((p) => ({ ...p, on: false }))
    install(data)
    await mount('model')
    const out = screen.getAllByText('gui.settings.roles.no_provider')[0]!.closest('button')!
    await act(async () => { fireEvent.click(out) })
    expect(store.get().tab).toBe('provider')
  })

  it('every slot says to connect a provider until one is connected, then names what it is missing', async () => {
    /* "None of the connected vendors serves this role" read as a claim about
       vendors the reader had never connected. */
    const none = snap()
    none.providers = none.providers.map((p) => ({ ...p, on: false }))
    install(none)
    await mount('model')
    expect(screen.queryAllByText('gui.settings.roles.no_vendor_for_role')).toHaveLength(0)
    expect(screen.queryAllByText('gui.settings.roles.connect_openrouter')).toHaveLength(0)
    for (const media of ['image', 'speech', 'video']) {
      const row = screen.getByText(`gui.settings.roles.${media}`).closest('.settings-row')!
      expect(row.querySelector('.settings-ctl')!.textContent).toContain('gui.settings.roles.no_provider')
    }
    cleanup()
    store._resetForTests()
    const some = snap()
    some.everos = { ...some.everos!, supports: {}, sections: {} }
    install(some)
    await mount('model')
    /* Only rerank still names a missing capability: the other memory slots
       take any connected vendor. */
    expect(screen.getAllByText('gui.settings.roles.no_vendor_for_role')).toHaveLength(1)
    expect(screen.getByText('gui.settings.roles.rerank').closest('.settings-row')!.textContent)
      .toContain('gui.settings.roles.no_vendor_for_role')
  })

  it('with no EverOS installed the memory slots say so instead of blaming the vendors', async () => {
    const data = snap()
    data.everos = { available: false, note: 'install everos-memory', sections: {} }
    install(data)
    await mount('model')
    const row = screen.getByText('gui.settings.roles.embedding').closest('.settings-row')!
    expect(row.textContent).toContain('gui.settings.roles.everos_missing')
    expect(row.querySelector('[title="install everos-memory"]')).not.toBeNull()
    expect(screen.queryAllByText('gui.settings.roles.no_vendor_for_role')).toHaveLength(0)
  })

  it('a slot with no provider sits in the same box as a slot with a model', async () => {
    /* Three labels of three lengths drawn as buttons sized to their words made
       a ragged column beside the served rows' pills. */
    const data = snap()
    data.providers = data.providers.map((p) => (p.id === 'openrouter' ? { ...p, on: false } : p))
    install(data)
    await mount('model')
    const served = pill('gui.settings.roles.title').closest('.settings-ctl > *')!
    const unserved = screen.getAllByText('gui.settings.roles.connect_openrouter')[0]!.closest('.settings-ctl > *')!
    expect(unserved.className.split(' ')).toContain('settings-mpill')
    expect(served.className.split(' ')).toContain('settings-mpill')
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
    const box = screen.getByPlaceholderText('gui.picker.search_or_type_ph') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'our-finetune' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.model.pick_use {"id":"our-finetune"}')) })
    expect(calls[0]![0]).toBe('provider')
    expect(calls[0]![1]).toMatchObject({
      op: 'add_model', model: 'our-finetune', capabilities: ['embedding'], output_modalities: ['vector'],
    })
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

  it('the closed disclosure reads out all three values, and opening it hands them to the drawer', async () => {
    install()
    await mount('model')
    const disc = screen.getByText('gui.settings.roles.params').closest('button') as HTMLButtonElement
    const chunks = (): string[] => [...disc.querySelectorAll('.settings-sv')].map((n) => n.textContent || '')
    expect(disc.getAttribute('aria-expanded')).toBe('false')
    /* One truncating line: the values sit in a single span that can ellipsize. */
    expect(disc.querySelectorAll('.settings-sum > .settings-sv')).toHaveLength(3)
    expect(chunks()).toEqual([
      'gui.settings.roles.sum_effortgui.settings.roles.effort_low',
      'gui.settings.roles.sum_itersgui.settings.roles.sum_iters_n {"n":40}',
      'gui.settings.roles.sum_ctxgui.settings.roles.sum_ctx_auto',
    ])
    expect(document.querySelector('.settings-cfg')).toBeNull()
    const row = disc.closest('.settings-row') as HTMLElement
    expect(row.className).not.toContain('settings-open')
    await act(async () => { fireEvent.click(disc) })
    expect(disc.getAttribute('aria-expanded')).toBe('true')
    expect(chunks()).toEqual([])
    expect(document.querySelector('.settings-cfg')).toBeTruthy()
    /* The row hands over its separator, which it only grows at all because the
       drawer stopped it being the last child of its wrapper. */
    expect(row.className).toContain('settings-open')
  })

  it('a pinned window is the value the closed disclosure shows, and the mode sits beside it when open', async () => {
    const pinned = snap()
    ;(pinned.raw.agents as { defaults: Record<string, unknown> }).defaults.contextWindowTokens = 300000
    install(pinned)
    await mount('model')
    const disc = screen.getByText('gui.settings.roles.params').closest('button') as HTMLButtonElement
    expect([...disc.querySelectorAll('.settings-sv')].map((n) => n.textContent)).toContain('gui.settings.roles.sum_ctx300,000 tok')
    await act(async () => { fireEvent.click(disc) })
    /* One row owns the window: the box that holds it and the two modes. */
    const row = screen.getByLabelText('gui.settings.roles.ctx_fixed').closest('.settings-row') as HTMLElement
    expect(row.querySelector('.settings-k')?.textContent).toBe('gui.settings.roles.ctx')
    expect([...row.querySelectorAll('.settings-seg button')].map((b) => b.textContent))
      .toEqual(['gui.settings.roles.ctx_auto', 'gui.settings.roles.ctx_pin'])
  })
})
