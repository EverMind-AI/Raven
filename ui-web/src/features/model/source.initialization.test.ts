// @vitest-environment happy-dom
/* Selection initialization must stay independent of slow provider catalogues. */

import { describe, expect, it } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

import type { ParamsOf, ResultOf } from '../../rpc/generated'

type Options = ResultOf<'model.options'>
interface Read {
  params: ParamsOf<'model.options'>
  resolve(value: Options): void
  reject(error: Error): void
}

async function live() {
  let generation = 0
  const staged: { model: { model: string; provider: string } | null } = { model: null }
  const toasts: string[] = []
  const source = await loadPart(() => import('./source'), {
    fakes: {
      'src/lib/session': { current: () => null },
      'src/state/session/generation': { generation: () => generation },
      'src/state/session/staging': { staging: () => staged },
      'src/features/settings/store': { openModels: () => {} },
      'src/state/toast': { show: (text: string) => { toasts.push(text) } },
    },
  })
  const store = await import('./store')
  store._resetForTests()
  source._resetForTests()
  const reads: Read[] = []
  await fakeGateway((method: string, params: ParamsOf<'model.options'>) => {
    if (method !== 'model.options') throw new Error(`unexpected method ${method}`)
    return new Promise<Options>((resolve, reject) => { reads.push({ params, resolve, reject }) })
  })
  const answer = (model = 'deepseek/deepseek-chat', provider = 'deepseek'): Options => ({ model, provider, providers: [] })
  const tick = () => new Promise<void>((resolve) => { setTimeout(resolve, 0) })
  return { source, store, reads, staged, toasts, answer, tick, bump: () => { generation += 1 } }
}

describe('model initialization', () => {
  it('starts without an invented selection', async () => {
    const h = await live()
    expect(h.store.current()).toBe('')
    expect(h.store.loadStatus()).toBe('loading')
  })

  it('shows the configured model and permits sending while the catalogue is pending', async () => {
    const h = await live()
    const load = h.source.loadProviders(null)
    expect(h.reads.map((read) => read.params)).toEqual([{ include_providers: false }, {}])
    h.reads[0]!.resolve(h.answer())
    await h.tick()
    expect(h.store.current()).toBe('deepseek/deepseek-chat')
    expect(h.store.currentProvider()).toBe('deepseek')
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.source.modelSource.loading?.()).toBe(true)
    expect(h.source.openModelsForMissingProvider()).toBe(false)
    h.reads[1]!.resolve(h.answer('obsolete-model', 'other'))
    await load
    expect(h.store.current()).toBe('deepseek/deepseek-chat')
    expect(h.source.modelSource.loading?.()).toBe(false)
  })

  it('keeps a usable selection when the catalogue fails and offers a retry', async () => {
    const h = await live()
    const load = h.source.loadProviders()
    h.reads[0]!.resolve(h.answer())
    h.reads[1]!.reject(new Error('catalogue unavailable'))
    await load
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.source.openModelsForMissingProvider()).toBe(false)
    expect(h.toasts).toHaveLength(1)
    expect(h.source.retryFailedLoad()).toBe(true)
    h.reads[2]!.resolve(h.answer())
    h.reads[3]!.resolve(h.answer())
    await h.tick()
    expect(h.source.retryFailedLoad()).toBe(false)
  })

  it('exposes selection failure and recovers through retry', async () => {
    const h = await live()
    const load = h.source.loadSelection()
    h.reads[0]!.reject(new Error('socket disconnected'))
    await load
    expect(h.store.loadStatus()).toBe('error')
    expect(h.store.current()).toBe('')
    expect(h.source.openModelsForMissingProvider()).toBe(true)
    expect(h.source.retryFailedLoad()).toBe(true)
    expect(h.store.loadStatus()).toBe('loading')
    h.reads[1]!.resolve(h.answer())
    h.reads[2]!.resolve(h.answer())
    await h.tick()
    expect(h.store.loadStatus()).toBe('ready')
  })

  it('clears the previous model when the backend has no selection', async () => {
    const h = await live()
    h.store.setCurrent('previous-model', 'previous-provider')
    const load = h.source.loadSelection()
    h.reads[0]!.resolve(h.answer('', ''))
    await load
    expect(h.store.current()).toBe('')
    expect(h.store.currentProvider()).toBe('')
    expect(h.store.loadStatus()).toBe('empty')
  })

  it('drops a selection from a conversation that has been left', async () => {
    const h = await live()
    const old = h.source.loadSelection('old')
    h.bump()
    const next = h.source.loadSelection('next')
    expect(h.reads.map((read) => read.params.session_id)).toEqual(['old', 'next'])
    h.reads[1]!.resolve(h.answer('new-model'))
    await next
    h.reads[0]!.resolve(h.answer('old-model'))
    await old
    expect(h.store.current()).toBe('new-model')
  })

  it('does not let a late selection or failure undo a local pick', async () => {
    const h = await live()
    const load = h.source.loadSelection()
    h.store.setCurrent('chosen-model', 'chosen-provider')
    h.reads[0]!.resolve(h.answer('old-model'))
    await load
    expect(h.store.current()).toBe('chosen-model')
    const retry = h.source.loadSelection()
    h.store.setCurrent('chosen-model', 'chosen-provider')
    h.reads[1]!.reject(new Error('late failure'))
    await retry
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.store.currentProvider()).toBe('chosen-provider')
  })

  it('ignores an already superseded refresh without disturbing a current load', async () => {
    const h = await live()
    h.bump()
    const load = h.source.loadProviders('current', 1)
    await h.source.loadProviders('old', 0)
    expect(h.reads).toHaveLength(2)
    expect(h.source.modelSource.loading?.()).toBe(true)
    h.reads[0]!.resolve(h.answer('current-model'))
    h.reads[1]!.resolve(h.answer())
    await load
    expect(h.store.current()).toBe('current-model')
    expect(h.source.modelSource.loading?.()).toBe(false)
  })

  it('keeps a staged draft pick during provider refreshes', async () => {
    const h = await live()
    h.staged.model = { model: 'draft-model', provider: 'draft-provider' }
    const load = h.source.loadProviders(null)
    expect(h.reads).toHaveLength(1)
    expect(h.store.current()).toBe('draft-model')
    h.reads[0]!.resolve(h.answer())
    await load
    expect(h.store.current()).toBe('draft-model')
  })
})
