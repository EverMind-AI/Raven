// @vitest-environment happy-dom
/* What the composer's model chip says about the model in force.
 *
 * The chip has to name the model and the account serving it, and neither is on
 * the wire as such: `model.options` states a provider, but the flag goes stale
 * the moment a pick moves the conversation (features/model/store.ts's
 * `carried`), so the chip resolves the account from the lists it can see. That
 * resolution is an identity question, and the identity rule is
 * features/model/types.ts's `sameModel` -- the same rule the column builder
 * de-dups by, because a provider's list mixes ids added by hand with ids the
 * vendor reports.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import * as store from '../features/model/store'
import { resetSources, setSources } from '../state/sources'
import { ModelChip } from './ModelChip'

import type { ModelSource, Provider } from '../features/model/types'

function install(providers: Provider[]): void {
  const source: ModelSource = {
    providers: () => providers,
    persist: async () => {},
    addModel: async () => {},
    openSettings: () => {},
    openProviderModels: () => {},
  }
  setSources({ model: source })
}

const mount = (): void => {
  render(<ModelChip />, { container: document.body.appendChild(document.createElement('div')) })
}

const mark = (): HTMLElement | null => document.querySelector<HTMLElement>('.provider-icon')
const name = (): string | null => document.getElementById('modelName')!.textContent

afterEach(() => {
  store._resetForTests()
  cleanup()
  resetSources()
  document.body.innerHTML = ''
})

describe('the model chip', () => {
  it('marks the account that serves the model when both spell it the same way', () => {
    install([{ id: 'openrouter', name: 'OpenRouter', on: true, models: ['my-model'], configured: ['my-model'] }])
    store.setCurrent('my-model')
    mount()
    expect(mark()).not.toBeNull()
    expect(name()).toBe('my-model')
  })

  it('marks it when the account spells the model with its vendor half', () => {
    /* The conversation carries the bare id and the account's list carries the
       qualified one. Resolving the account by string finds nobody, and the chip
       loses its mark entirely -- the reader is shown a model with no account. */
    install([{
      id: 'openrouter', name: 'OpenRouter', on: true,
      models: ['openrouter/my-model'], configured: ['openrouter/my-model'],
      labels: { 'openrouter/my-model': { label: 'My Model', kind: 'text' } },
    }])
    store.setCurrent('my-model')
    mount()
    expect(mark()).not.toBeNull()
  })

  it('marks the account the conversation is on, not another that lists the same id', () => {
    /* A gateway and a direct vendor can both list one id. Reading the account
       off the lists alone answered with whichever came first, so the chip could
       name an account the conversation is not running on -- which is the one
       question the mark exists to answer. */
    install([
      { id: 'anthropic', name: 'Anthropic', on: true, models: ['claude-opus-5'], configured: ['claude-opus-5'] },
      { id: 'openrouter', name: 'OpenRouter', on: true, models: ['claude-opus-5'], configured: ['claude-opus-5'] },
    ])
    store.setCurrent('claude-opus-5', 'openrouter')
    mount()
    expect(mark()!.getAttribute('data-provider')).toBe('openrouter')
  })

  it('marks it when the list spells the model with a name the account used to answer to', () => {
    /* A model id written before a provider was renamed. The wire canonicalizes
       the account's slug but hands back the configured list as it stands, so
       the two spellings meet here; `merge_key` treats them as one model and the
       prefixes it strips travel on the row. */
    install([{
      id: 'zai', name: 'Z.ai', on: true, routes: ['zai', 'zhipu'],
      models: ['zhipu/glm-4.6'], configured: ['zhipu/glm-4.6'],
      labels: { 'zhipu/glm-4.6': { label: 'GLM-4.6', kind: 'text' } },
    }])
    store.setCurrent('zai/glm-4.6')
    mount()
    expect(mark()).not.toBeNull()
    expect(name()).toBe('GLM-4.6')
  })

  it('names it by the account\'s label when the spellings differ', () => {
    /* The label map is keyed by the account's spelling, so looking it up with
       the conversation's spelling falls through to the raw id and the chip
       stops agreeing with the row the model was picked from. */
    install([{
      id: 'openrouter', name: 'OpenRouter', on: true,
      models: ['openrouter/my-model'], configured: ['openrouter/my-model'],
      labels: { 'openrouter/my-model': { label: 'My Model', kind: 'text' } },
    }])
    store.setCurrent('my-model')
    mount()
    expect(name()).toBe('My Model')
  })
})
