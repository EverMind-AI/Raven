// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { AgentsBody, FoundAgent, ImportScan, OnboardSource, StepBody } from './types'

const body = (done: boolean, found: FoundAgent[] = []): AgentsBody => ({
  Body: () => null as never,
  load: async () => {},
  subscribe: () => () => {},
  loaded: () => true,
  done: () => done,
  found: () => found,
})

const source = (scan: ImportScan): OnboardSource => ({
  providerConfigured: async () => true,
  scan: async () => scan,
  startImport: async () => ({ started: true, total: 0, detail: '' }),
})

const READY: ImportScan = {
  ready: true,
  reason: '',
  platforms: [{ platform: 'hermes', scannable: true, memory_files: 8, conversations: 52, estimated_size: 0 }],
}

afterEach(() => {
  store._resetForTests()
  resetSources()
})

const opened = async (scan: ImportScan, found: FoundAgent[], done = false): Promise<void> => {
  setSources({ onboard: source(scan) })
  store.setBodies({ model: body(done) as StepBody, search: body(done) as StepBody, agents: body(done, found) })
  store.open()
  await Promise.resolve()
  await Promise.resolve()
}

describe('the wizard store', () => {
  it('does not open before the page has handed it the bodies', () => {
    setSources({ onboard: source(READY) })
    store.open()
    expect(store.isOpen()).toBe(false)
  })

  it('offers the sync step only for an agent the importer can read', async () => {
    await opened(READY, [{ id: 'codex', name: 'Codex' }])
    expect(store.visibleSteps()).toEqual(['model', 'search', 'agents'])
    await opened(READY, [{ id: 'hermes', name: 'Hermes' }])
    expect(store.visibleSteps()).toEqual(['model', 'search', 'agents', 'sync'])
    expect(store.platformOf({ id: 'hermes', name: 'Hermes' })?.conversations).toBe(52)
  })

  it('needs the importer to be ready as well', async () => {
    await opened({ ...READY, ready: false, reason: 'off' }, [{ id: 'hermes', name: 'Hermes' }])
    expect(store.syncVisible()).toBe(false)
  })

  it('reads each step\'s verdict from its body, and the sync step from the picks', async () => {
    await opened(READY, [{ id: 'hermes', name: 'Hermes' }], true)
    expect(store.stepDone('model')).toBe(true)
    expect(store.stepDone('sync')).toBe(false)
    store.toggleSync('hermes')
    expect(store.stepDone('sync')).toBe(true)
    store.toggleSync('hermes')
    expect(store.stepDone('sync')).toBe(false)
  })

  it('moves within the visible steps and never past either end', async () => {
    await opened(READY, [])
    store.back()
    expect(store.get().step).toBe('model')
    await store.next()
    await store.next()
    await store.next()
    expect(store.get().step).toBe('agents')
    expect(store.isLast('agents')).toBe(true)
  })

  it('records a skip and lands on the next step', async () => {
    await opened(READY, [])
    await store.next()
    await store.skip()
    expect(store.get().skipped).toEqual({ search: true })
    expect(store.get().step).toBe('agents')
  })
})
