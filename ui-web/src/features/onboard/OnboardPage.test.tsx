// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as langPick from '../../state/lang/pick'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { setupState } from '../model/source'
import { OnboardApp } from './OnboardPage'
import * as store from './store'

import type { AgentsPane, FoundAgent, ImportScan, OnboardSource } from './types'
import type { JSX } from 'react'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* A step body under the test's control: what it says it is, whether it has
   loaded, whether it is done, and a way to change either and notify. */
interface FakePane extends AgentsPane {
  setDone(v: boolean): void
  setLoaded(v: boolean): void
  agents: FoundAgent[]
}

function fakePane(name: string): FakePane {
  const listeners = new Set<() => void>()
  let loaded = false
  let done = false
  const notify = (): void => { for (const fn of [...listeners]) fn() }
  const pane: FakePane = {
    Pane: (): JSX.Element => <div className={`fake-${name}`} />,
    load: vi.fn(async () => {}),
    subscribe: (fn) => { listeners.add(fn); return () => { listeners.delete(fn) } },
    loaded: () => loaded,
    done: () => done,
    found: () => pane.agents,
    agents: [],
    setDone: (v) => { done = v; notify() },
    setLoaded: (v) => { loaded = v; notify() },
  }
  return pane
}

const SCAN_NONE: ImportScan = { ready: true, reason: '', platforms: [] }
const SCAN_CLAUDE: ImportScan = {
  ready: true,
  reason: '',
  platforms: [
    { platform: 'claude_code', scannable: true, memory_files: 31, conversations: 284, estimated_size: 1 },
    { platform: 'codex', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
  ],
}

interface Harness {
  model: FakePane
  search: FakePane
  agents: FakePane
  source: OnboardSource
  runs: Array<[string[], string]>
  configured: boolean
  started: boolean
}

function install(scan: ImportScan = SCAN_NONE): Harness {
  const h: Harness = {
    model: fakePane('model'),
    search: fakePane('search'),
    agents: fakePane('agents'),
    runs: [],
    configured: true,
    started: true,
    source: null as unknown as OnboardSource,
  }
  h.source = {
    providerConfigured: async () => h.configured,
    scan: async () => scan,
    startImport: async (platforms, tier) => {
      h.runs.push([platforms, tier])
      return { started: h.started, total: 3, detail: h.started ? '' : 'nothing to import' }
    },
  }
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
  setSources({ onboard: h.source })
  store.setPanes({ model: h.model, search: h.search, agents: h.agents })
  document.body.innerHTML = '<div id="onb" hidden></div>'
  return h
}

const host = (): HTMLElement => document.getElementById('onb')!
const mount = (): ReturnType<typeof render> => render(<OnboardApp />, { container: host() })
const buttons = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('.ob-foot button')]
const button = (text: string): HTMLButtonElement => buttons().find((b) => b.textContent === text)!
const steps = (): string[] => [...document.querySelectorAll<HTMLElement>('.ob-sitem')].map((el) => `${el.textContent}:${el.dataset.state}`)

const open = async (): Promise<void> => {
  await act(async () => {
    store.open()
  })
}
const click = async (el: Element): Promise<void> => {
  await act(async () => {
    fireEvent.click(el)
  })
}
const settle = async (ms = store.CLOSE_MS + 40): Promise<void> => {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms))
  })
}

beforeEach(() => {
  setupState.providerConfigured = null
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  resetSources()
  resetTranslator()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('the onboarding wizard', () => {
  it('renders nothing until opened, then takes the host', async () => {
    install()
    mount()
    expect(host().children.length).toBe(0)
    expect(host().hidden).toBe(true)
    await open()
    expect(host().hidden).toBe(false)
    expect(document.querySelector('.ob-app')).not.toBeNull()
  })

  it('asks every step for its data at once', async () => {
    const h = install()
    mount()
    await open()
    expect(h.model.load).toHaveBeenCalledTimes(1)
    expect(h.agents.load).toHaveBeenCalledTimes(1)
  })

  it('offers three steps when nothing can be synced, the first with only Next', async () => {
    const h = install()
    mount()
    await open()
    expect(steps()).toEqual(['1gui.onb.step_model:current', '2gui.onb.step_search:upcoming', '3gui.onb.step_agents:upcoming'])
    expect(buttons().map((b) => b.textContent)).toEqual(['gui.onb.next'])
    expect(button('gui.onb.next').disabled).toBe(true)
    await act(async () => { h.model.setDone(true) })
    expect(button('gui.onb.next').disabled).toBe(false)
  })

  it('shows the loading line until the pane has loaded, then the pane', async () => {
    const h = install()
    mount()
    await open()
    expect(document.querySelector('.ob-loading')).not.toBeNull()
    await act(async () => { h.model.setLoaded(true) })
    expect(document.querySelector('.fake-model')).not.toBeNull()
  })

  it('walks forward and back, and a skipped step is marked so', async () => {
    const h = install()
    mount()
    await open()
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    expect(steps()[0]).toBe('✓gui.onb.step_model:done')
    expect(steps()[1]).toBe('2gui.onb.step_search:current')
    expect(buttons().map((b) => b.textContent)).toEqual(['gui.onb.back', 'gui.onb.skip', 'gui.onb.next'])
    expect(button('gui.onb.next').disabled).toBe(true)
    expect(button('gui.onb.skip').disabled).toBe(false)
    await click(button('gui.onb.skip'))
    expect(steps()).toEqual(['✓gui.onb.step_model:done', '-gui.onb.step_search:skipped', '3gui.onb.step_agents:current'])
    await click(button('gui.onb.back'))
    expect(steps()[1]).toBe('2gui.onb.step_search:current')
  })

  it('disables Skip once the step is done: a configured step is not one you skipped', async () => {
    const h = install()
    mount()
    await open()
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    await act(async () => { h.search.setDone(true) })
    expect(button('gui.onb.skip').disabled).toBe(true)
    expect(button('gui.onb.next').disabled).toBe(false)
  })

  it('ends on Start chatting when there is nothing to sync, and re-reads the first-run verdict', async () => {
    const h = install()
    h.configured = true
    mount()
    await open()
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    await click(button('gui.onb.skip'))
    expect(steps()[2]).toBe('3gui.onb.step_agents:current')
    expect(button('gui.onb.enter')).toBeDefined()
    expect(button('gui.onb.enter').disabled).toBe(true)
    await act(async () => { h.agents.setDone(true) })
    await click(button('gui.onb.enter'))
    expect(host().dataset.off).toBe('1')
    expect(setupState.providerConfigured).toBe(true)
    await settle()
    expect(host().children.length).toBe(0)
    expect(host().hidden).toBe(true)
    expect(h.runs).toEqual([])
  })

  it('adds the sync step once the scan can read an agent the machine has', async () => {
    const h = install(SCAN_CLAUDE)
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }]
    mount()
    await open()
    expect(steps().length).toBe(4)
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    await click(button('gui.onb.skip'))
    expect(button('gui.onb.next')).toBeDefined()
    await click(button('gui.onb.skip'))
    expect(steps()[3]).toBe('4gui.onb.step_sync:current')
    const rows = [...document.querySelectorAll('.ob-row')]
    expect(rows.length).toBe(2)
    expect(rows[0]!.textContent).toContain('gui.onb.sync_counts:{"files":31,"convs":284}')
    expect(rows[0]!.querySelector('[role=switch]')).not.toBeNull()
    expect(rows[1]!.textContent).toContain('gui.onb.sync_unsupported')
    expect(rows[1]!.querySelector('[role=switch]')).toBeNull()
    expect(button('gui.onb.start_sync').disabled).toBe(true)
    await click(rows[0]!.querySelector('[role=switch]')!)
    expect(rows[0]!.querySelector('[role=switch]')!.getAttribute('aria-checked')).toBe('true')
    expect(button('gui.onb.start_sync').disabled).toBe(false)
    expect(button('gui.onb.skip').disabled).toBe(true)
    await click(button('gui.onb.start_sync'))
    expect(h.runs).toEqual([[['claude_code'], 'full']])
    expect(host().dataset.off).toBe('1')
  })

  it('stays open and says why when the import does not start', async () => {
    const h = install(SCAN_CLAUDE)
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }]
    h.started = false
    mount()
    await open()
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    await click(button('gui.onb.skip'))
    await click(button('gui.onb.skip'))
    await click(document.querySelector('.ob-row [role=switch]')!)
    await click(button('gui.onb.start_sync'))
    expect(host().dataset.off).toBeUndefined()
    expect(document.querySelector('.ob-err')!.textContent).toBe('nothing to import')
  })

  it('leaves the sync step out when the importer cannot run', async () => {
    const h = install({ ...SCAN_CLAUDE, ready: false, reason: 'no memory backend' })
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }]
    mount()
    await open()
    expect(steps().length).toBe(3)
  })

  it('switches the language through the page pick, persisted', async () => {
    install()
    const pick = vi.spyOn(langPick, 'pick').mockImplementation(async () => {})
    mount()
    await open()
    const [zh, en] = [...document.querySelectorAll<HTMLButtonElement>('.ob-lang button')]
    expect(zh!.textContent).toBe('gui.onb.lang_zh')
    await click(en!)
    expect(pick).toHaveBeenCalledWith('en', { persist: true })
  })

  it('keeps its rendered shape', async () => {
    const h = install(SCAN_CLAUDE)
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }]
    mount()
    await open()
    await act(async () => { h.model.setLoaded(true) })
    expect(domSnapshot(host())).toMatchSnapshot('model step')
  })
})
