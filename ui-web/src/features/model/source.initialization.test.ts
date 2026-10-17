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

async function live({ selectionOnly = true } = {}) {
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
  const { absorb } = await import('../../rpc/capabilities')
  const { RpcError } = await import('../../rpc/transport')
  let acceptsSelectionOnly = selectionOnly
  const announce = (enabled: boolean) => {
    acceptsSelectionOnly = enabled
    absorb(enabled ? ['model.options.selection_only'] : [])
  }
  announce(selectionOnly)
  const reads: Read[] = []
  await fakeGateway((method: string, params: ParamsOf<'model.options'>) => {
    if (method !== 'model.options') throw new Error(`unexpected method ${method}`)
    if (!acceptsSelectionOnly && Object.keys(params).some((key) => key !== 'session_id')) {
      throw new RpcError(-32011, 'config_validation_error')
    }
    return new Promise<Options>((resolve, reject) => { reads.push({ params, resolve, reject }) })
  })
  const answer = (model = 'deepseek/deepseek-chat', provider = 'deepseek'): Options => ({ model, provider, providers: [] })
  const tick = () => new Promise<void>((resolve) => { setTimeout(resolve, 0) })
  return { source, store, reads, staged, toasts, answer, tick, announce, bump: () => { generation += 1 } }
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
    expect(h.source.blockSendForModel()).toBe(false)
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
    expect(h.source.blockSendForModel()).toBe(false)
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
    expect(h.source.blockSendForModel()).toBe(true)
    expect(h.source.openModelsForMissingProvider()).toBe(false)
    expect(h.source.retryFailedLoad()).toBe(true)
    expect(h.store.loadStatus()).toBe('loading')
    h.reads[1]!.resolve(h.answer())
    h.reads[2]!.resolve(h.answer())
    await h.tick()
    expect(h.store.loadStatus()).toBe('ready')
  })

  it.each([true, false])('clears the previous model when the backend has no selection (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    h.store.setCurrent('previous-model', 'previous-provider')
    const load = h.source.loadSelection()
    h.reads[0]!.resolve(h.answer('', ''))
    await load
    expect(h.store.current()).toBe('')
    expect(h.store.currentProvider()).toBe('')
    expect(h.store.loadStatus()).toBe('empty')
  })

  it.each([
    [true, null], [true, 'bound-session'], [false, null], [false, 'bound-session'],
  ] as const)('keeps a known selection ready during a background refresh (selection-only: %s, session: %s)', async (selectionOnly, session) => {
    const h = await live({ selectionOnly })
    const initial = h.source.loadSelection(session)
    h.reads[0]!.resolve(h.answer())
    await initial
    const refresh = h.source.loadProviders(session)
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.store.current()).toBe('deepseek/deepseek-chat')
    expect(h.source.blockSendForModel()).toBe(false)
    expect(h.source.modelSource.loading?.()).toBe(true)
    h.reads[1]!.resolve(h.answer('updated-model', 'updated-provider'))
    if (selectionOnly) h.reads[2]!.resolve(h.answer('catalogue-model'))
    await refresh
    expect(h.store.current()).toBe('updated-model')
    expect(h.store.currentProvider()).toBe('updated-provider')
    expect(h.source.modelSource.loading?.()).toBe(false)
  })

  it.each([true, false])('waits for the first read even when an old pair is present (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    h.store.setCurrent('old-model', 'old-provider')
    const load = h.source.loadSelection('new-session')
    expect(h.store.loadStatus()).toBe('loading')
    expect(h.source.blockSendForModel()).toBe(true)
    expect(h.source.openModelsForMissingProvider()).toBe(false)
    h.reads[0]!.resolve(h.answer())
    await load
  })

  it.each([true, false])('waits for a different session even under the same generation (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    const initial = h.source.loadSelection('first')
    h.reads[0]!.resolve(h.answer())
    await initial
    const next = h.source.loadSelection('second')
    expect(h.store.loadStatus()).toBe('loading')
    expect(h.source.blockSendForModel()).toBe(true)
    h.reads[1]!.resolve(h.answer('second-model'))
    await next
    expect(h.store.current()).toBe('second-model')
  })

  it.each([true, false])('waits after navigation even when the session key is unchanged (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    const initial = h.source.loadSelection(null)
    h.reads[0]!.resolve(h.answer())
    await initial
    h.bump()
    const next = h.source.loadSelection(null)
    expect(h.store.loadStatus()).toBe('loading')
    expect(h.source.blockSendForModel()).toBe(true)
    h.reads[1]!.resolve(h.answer('new-draft-model'))
    await next
    expect(h.store.current()).toBe('new-draft-model')
  })

  it.each([true, false])('still reports a failed background selection and permits retry (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    const initial = h.source.loadSelection()
    h.reads[0]!.resolve(h.answer())
    await initial
    const refresh = h.source.loadSelection()
    expect(h.store.loadStatus()).toBe('ready')
    h.reads[1]!.reject(new Error('selection unavailable'))
    await refresh
    expect(h.store.loadStatus()).toBe('error')
    expect(h.source.blockSendForModel()).toBe(true)
    expect(h.source.openModelsForMissingProvider()).toBe(false)
    expect(h.source.retryFailedLoad()).toBe(true)
    expect(h.store.loadStatus()).toBe('loading')
    h.reads[2]!.resolve(h.answer())
    if (selectionOnly) h.reads[3]!.resolve(h.answer())
    await h.tick()
    expect(h.store.loadStatus()).toBe('ready')
  })

  it('settles both loading states when no gateway is installed', async () => {
    const h = await live()
    const { setGateway } = await import('../../rpc/gateway')
    setGateway(null)
    await expect(h.source.loadProviders()).resolves.toBeUndefined()
    expect(h.store.loadStatus()).toBe('error')
    expect(h.source.modelSource.loading?.()).toBe(false)
  })

  it.each([true, false])('drops a selection from a conversation that has been left (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
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

  it.each([true, false])('does not let a late selection or failure undo a local pick (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
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

  it.each([true, false])('keeps a staged draft pick during provider refreshes (selection-only: %s)', async (selectionOnly) => {
    const h = await live({ selectionOnly })
    h.staged.model = { model: 'draft-model', provider: 'draft-provider' }
    const load = h.source.loadProviders(null)
    expect(h.reads).toHaveLength(1)
    expect(h.store.current()).toBe('draft-model')
    h.reads[0]!.resolve(h.answer())
    await load
    expect(h.store.current()).toBe('draft-model')
  })

  it.each([null, 'bound-session'])('shares one legacy catalogue read and restores sending for session %s', async (session) => {
    const h = await live({ selectionOnly: false })
    const load = h.source.loadProviders(session)
    expect(h.reads.map((read) => read.params)).toEqual([session ? { session_id: session } : {}])
    expect(h.store.loadStatus()).toBe('loading')
    expect(h.source.blockSendForModel()).toBe(true)
    h.reads[0]!.resolve(h.answer('legacy-model', 'legacy-provider'))
    await load
    expect(h.store.current()).toBe('legacy-model')
    expect(h.store.currentProvider()).toBe('legacy-provider')
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.source.modelSource.loading?.()).toBe(false)
    expect(h.source.blockSendForModel()).toBe(false)
  })

  it('reads a selection directly from a legacy gateway', async () => {
    const h = await live({ selectionOnly: false })
    const load = h.source.loadSelection('bound-session')
    expect(h.reads.map((read) => read.params)).toEqual([{ session_id: 'bound-session' }])
    h.reads[0]!.resolve(h.answer())
    await load
    expect(h.store.current()).toBe('deepseek/deepseek-chat')
    expect(h.source.blockSendForModel()).toBe(false)
  })

  it('recovers a failed legacy read through retry without sending unsupported fields', async () => {
    const h = await live({ selectionOnly: false })
    const load = h.source.loadProviders()
    h.reads[0]!.reject(new Error('socket disconnected'))
    await load
    expect(h.store.loadStatus()).toBe('error')
    expect(h.source.blockSendForModel()).toBe(true)
    expect(h.source.retryFailedLoad()).toBe(true)
    expect(h.store.loadStatus()).toBe('loading')
    expect(h.reads.map((read) => read.params)).toEqual([{}, {}])
    h.reads[1]!.resolve(h.answer())
    await h.tick()
    expect(h.store.loadStatus()).toBe('ready')
    expect(h.source.retryFailedLoad()).toBe(false)
    expect(h.source.blockSendForModel()).toBe(false)
  })

  it('uses the newly announced mode after each handshake', async () => {
    const h = await live()
    const first = h.source.loadSelection()
    h.reads[0]!.resolve(h.answer('first-model'))
    await first
    h.announce(false)
    const legacy = h.source.loadProviders()
    expect(h.reads[1]!.params).toEqual({})
    expect(h.reads).toHaveLength(2)
    h.reads[1]!.resolve(h.answer('legacy-model'))
    await legacy
    h.announce(true)
    const current = h.source.loadProviders()
    expect(h.reads.slice(2).map((read) => read.params)).toEqual([{ include_providers: false }, {}])
    h.reads[2]!.resolve(h.answer('current-model'))
    h.reads[3]!.resolve(h.answer('obsolete-model'))
    await current
    expect(h.store.current()).toBe('current-model')
  })
})
