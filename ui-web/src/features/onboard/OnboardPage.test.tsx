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

import type { AgentsBody, FoundAgent, ImportScan, OnboardSource } from './types'
import type { JSX } from 'react'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* A step body under the test's control: what it says it is, whether it has
   loaded, whether it is done, and a way to change either and notify. */
interface FakeBody extends AgentsBody {
  setDone(v: boolean): void
  setLoaded(v: boolean): void
  setNeedsRestart(v: boolean): void
  agents: FoundAgent[]
}

function fakeBody(name: string): FakeBody {
  const listeners = new Set<() => void>()
  let loaded = false
  let done = false
  let needsRestart = false
  const notify = (): void => { for (const fn of [...listeners]) fn() }
  const body: FakeBody = {
    Body: (): JSX.Element => <div className={`fake-${name}`} />,
    load: vi.fn(async () => {}),
    subscribe: (fn) => { listeners.add(fn); return () => { listeners.delete(fn) } },
    loaded: () => loaded,
    done: () => done,
    found: () => body.agents,
    needsRestart: () => needsRestart,
    agents: [],
    setDone: (v) => { done = v; notify() },
    setLoaded: (v) => { loaded = v; notify() },
    setNeedsRestart: (v) => { needsRestart = v; notify() },
  }
  return body
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
  model: FakeBody
  search: FakeBody
  agents: FakeBody
  source: OnboardSource
  runs: Array<[string[], string]>
  configured: boolean
  started: boolean
}

function install(scan: ImportScan = SCAN_NONE): Harness {
  const h: Harness = {
    model: fakeBody('model'),
    search: fakeBody('search'),
    agents: fakeBody('agents'),
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
  store.setBodies({ model: h.model, search: h.search, agents: h.agents })
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

  it('shows the loading line until the step body has loaded, then the body', async () => {
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
    const rows = [...document.querySelectorAll('.ob-row:not(.ob-tier)')]
    expect(rows.length).toBe(2)
    expect(rows[0]!.textContent).toContain('gui.onb.sync_counts:{"files":31,"convs":284}')
    expect(rows[0]!.querySelector('[role=switch]')).not.toBeNull()
    expect(rows[1]!.textContent).toContain('gui.onb.sync_unsupported')
    expect(rows[1]!.querySelector('[role=switch]')).toBeNull()
    expect(button('gui.onb.start_sync').disabled).toBe(true)
    /* The tier rows follow the agents: memory files by default, and the
       descriptions count what the switches that are on would import. */
    const tiers = [...document.querySelectorAll<HTMLInputElement>('.ob-tier input')]
    expect(tiers.map((r) => `${r.value}:${r.checked}`)).toEqual(['memory_files:true', 'full:false'])
    expect(document.querySelector('.ob-tiers')!.textContent).toContain('gui.onb.tier_memory_desc:{"files":0}')
    await click(rows[0]!.querySelector('[role=switch]')!)
    expect(rows[0]!.querySelector('[role=switch]')!.getAttribute('aria-checked')).toBe('true')
    expect(document.querySelector('.ob-tiers')!.textContent).toContain('gui.onb.tier_full_desc:{"files":31,"convs":284}')
    expect(button('gui.onb.start_sync').disabled).toBe(false)
    expect(button('gui.onb.skip').disabled).toBe(true)
    await click(button('gui.onb.start_sync'))
    expect(h.runs).toEqual([[['claude_code'], 'memory_files']])
    expect(host().dataset.off).toBe('1')
  })

  it('starts a full import when the reader picks that tier', async () => {
    const h = install(SCAN_CLAUDE)
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }]
    mount()
    await open()
    await act(async () => { h.model.setDone(true) })
    await click(button('gui.onb.next'))
    await click(button('gui.onb.skip'))
    await click(button('gui.onb.skip'))
    await click(document.querySelector('.ob-row:not(.ob-tier) [role=switch]')!)
    await click(document.querySelector<HTMLInputElement>('.ob-tier input[value=full]')!)
    expect(document.querySelector<HTMLInputElement>('.ob-tier input[value=full]')!.checked).toBe(true)
    await click(button('gui.onb.start_sync'))
    expect(h.runs).toEqual([[['claude_code'], 'full']])
  })

  it('re-reads the importer when the primary is pressed on the agents step, and moves to the step it adds', async () => {
    /* On the answer from opening the agents step is the last one, so the
       footer offers Start chatting; a reader who connected an agent presses
       that, not Skip -- and by then the model step may have made the import
       possible. */
    const h = install({ ready: false, reason: 'no memory backend', platforms: [] })
    h.agents.agents = [{ id: 'claude_code', name: 'Claude Code' }]
    mount()
    await open()
    expect(steps().length).toBe(3)
    await act(async () => { h.model.setDone(true); h.search.setDone(true); h.agents.setDone(true) })
    await click(button('gui.onb.next'))
    await click(button('gui.onb.next'))
    expect(button('gui.onb.enter')).toBeDefined()

    h.source.scan = async () => SCAN_CLAUDE
    await click(button('gui.onb.enter'))

    expect(steps()[3]).toBe('4gui.onb.step_sync:current')
    expect(button('gui.onb.start_sync')).toBeDefined()
    expect(store.isOpen()).toBe(true)
    expect(host().dataset.off).toBeUndefined()
  })

  it('says the gateway needs a restart once the model step reports it, on every step after', async () => {
    /* A first run: the gateway started with no model, the write landed, and
       this process cannot chat on it until it comes back. Said in the frame
       rather than learnt from the first send failing. */
    const h = install()
    mount()
    await open()
    expect(document.querySelector('.ob-err')).toBeNull()
    await act(async () => { h.model.setDone(true); h.model.setNeedsRestart(true) })
    expect(document.querySelector('.ob-err')?.textContent).toBe('gui.onb.restart')
    await click(button('gui.onb.next'))
    expect(document.querySelector('.ob-err')?.textContent).toBe('gui.onb.restart')
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
