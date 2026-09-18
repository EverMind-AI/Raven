// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetSources } from '../../../state/sources'
import { install, mount, snap } from '../harness'
import * as store from '../store'
import { ROLES, roleProviders, rolesUsing } from './Roles'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

const role = (id: string) => ROLES.find((r) => r.id === id)!
const pill = (roleName: string): HTMLElement => screen.getByLabelText(`gui.settings.roles.change {"role":"${roleName}"}`)
const picker = (): HTMLElement => document.querySelector('.mpk') as HTMLElement
const pick = async (roleName: string, model: string, providerName?: string): Promise<void> => {
  await act(async () => { fireEvent.click(pill(roleName)) })
  if (providerName) await act(async () => { fireEvent.click(within(picker()).getByText(providerName)) })
  await act(async () => { fireEvent.click(within(picker()).getByText(model)) })
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

  it('a media role offers OpenRouter only, writes the selection first and then unhides the tool', async () => {
    const { calls } = install()
    await mount('model')
    expect(roleProviders(role('image'), snap()).map((p) => p.id)).toEqual(['openrouter'])
    await pick('gui.settings.roles.image', 'openai/gpt-4o')
    expect(sets(calls)).toEqual([
      { key: 'tools.media.image', value: { model: 'openai/gpt-4o', quality: '' } },
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

  it('an EverOS role offers only providers with a key of their own and writes the section with borrow_from', async () => {
    const { calls } = install()
    await mount('model')
    expect(roleProviders(role('embedding'), snap()).map((p) => p.id)).toEqual(['anthropic', 'openrouter'])
    expect(pill('gui.settings.roles.memllm').textContent).toContain('openai/gpt-4o')
    await pick('gui.settings.roles.embedding', 'text-embedding-3-small', 'OpenRouter')
    expect(calls).toEqual([['everosSet', { section: 'embedding', fields: { model: 'text-embedding-3-small' }, borrowFrom: 'openrouter' }]])
  })

  it('a typed id is added to the provider before the role names it', async () => {
    const { calls } = install()
    await mount('model')
    await act(async () => { fireEvent.click(pill('gui.settings.roles.gate')) })
    const box = screen.getByPlaceholderText('gui.model.pick_search') as HTMLInputElement
    await act(async () => { fireEvent.change(box, { target: { value: 'claude-haiku-4-5' } }) })
    await act(async () => { fireEvent.click(screen.getByText('gui.model.pick_use {"id":"claude-haiku-4-5"}')) })
    expect(calls).toEqual([
      ['provider', { op: 'add_model', slug: 'anthropic', model: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateModel', value: 'claude-haiku-4-5' }],
      ['set', { key: 'skillForge.llmGateProvider', value: 'anthropic' }],
    ])
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
