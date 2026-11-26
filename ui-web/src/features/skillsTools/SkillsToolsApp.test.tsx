// @vitest-environment happy-dom
/* Component and modal interaction tests for the Skills & Tools module page. */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirm from '../../state/confirm'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import { resetSources, setSources } from '../../state/sources'
import { pkgList, snap } from '../../test/settingsHarness'
import { ModelApp } from '../model/ModelPicker'
import * as modelStore from '../model/store'
import { SkillsToolsApp } from './SkillsToolsApp'
import * as store from './store'

import type { ModelSource, Provider } from '../model/types'
import type { SettingsSnapshot, SkillsToolsSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastCalls: string[] = []
vi.mock('../../state/toast', () => ({
  show: (msg: string) => { toastCalls.push(msg) },
  subscribe: () => () => {},
  get: () => [],
}))

type Call = [string, unknown]

function setup(data: SettingsSnapshot = snap(), over: Partial<SkillsToolsSource> = {}): { calls: Call[] } {
  const calls: Call[] = []
  const rec = (name: string, args: unknown): SettingsSnapshot => { calls.push([name, args]); return data }
  const src: SkillsToolsSource = {
    load: async () => data,
    set: async (key, value) => rec('set', { key, value }),
    everosSet: async (section, model, provider, protocol) =>
      rec('everosSet', { section, model, provider, ...(protocol ? { protocol } : {}) }),
    addModel: async (slug, model, tags) => rec('addModel', { slug, model, tags }),
    inspectSkill: async (name) => {
      calls.push(['inspectSkill', name])
      return { name, description: 'Skill details', path: `/skills/${name}`, body: '---\nname: test\n---\n# Heading\n\nBody text', files: ['SKILL.md', 'notes.md'], always: false, install: null }
    },
    openSkillFile: async (name, file) => { calls.push(['openSkillFile', { name, file }]) },
    uninstallSkill: async (name) => rec('uninstallSkill', name),
    blockSkill: async (name, blocked) => rec('blockSkill', { name, blocked }),
    serverDetail: async (name) => {
      calls.push(['serverDetail', name])
      return {
        known: true,
        fields: [{ key: 'token', label: 'Token', help_url: 'https://github.com/settings/tokens' }],
        address: 'https://api.githubcopilot.com/mcp',
        tools: ['list_issues', 'get_pr'],
      }
    },
    toggleServer: async (name, on) => rec('toggleServer', { name, on }),
    retryServer: async (name) => rec('retryServer', name),
    revokeServer: async (name) => rec('revokeServer', name),
    configureServer: async (name, form) => rec('configureServer', { name, form }),
    authServer: async (name) => rec('authServer', name),
    agentPlugins: async () => pkgList(),
    setPackageMcp: async (name, on) => { calls.push(['setPackageMcp', { name, on }]); return pkgList() },
    ...over,
  }
  const modelProviders: Provider[] = data.providers.map((p) => ({
    id: p.id, name: p.name, models: p.models, configured: p.configured,
    on: p.on, kind: p.kind, labels: p.labels, gateway: p.gateway, imageApi: p.imageApi, videoApi: p.videoApi,
    current: p.id === 'anthropic',
  }))
  const modelSrc: ModelSource = {
    providers: () => modelProviders,
    persist: async (model, provider, scope) => { calls.push(['persist', { model, provider, scope }]) },
    addModel: async (model, provider, kind) => { calls.push(['addModel', { model, provider, kind }]) },
    openSettings: () => { calls.push(['openSettings', null]) },
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ skillsTools: src, model: modelSrc })
  document.body.innerHTML =
    '<div class="app"></div>'
    + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"><div class="work"><div class="wrap" id="${p.bodyId}"></div></div></section>`).join('')
    + '<aside class="detail" id="detail" data-open="false"><div class="dpanel"><header><b id="dTitle"></b></header><div class="body" id="dBody"></div></div></aside>'
  return { calls }
}

async function mountPage(tab: 'skills' | 'tools' | 'mcp' | 'plugins' = 'skills'): Promise<void> {
  render(createElement(SkillsToolsApp), { container: document.getElementById('skillsToolsBody')! })
  render(createElement(ModelApp), { container: document.body.appendChild(document.createElement('div')) })
  await act(async () => { await store.open(tab) })
}

beforeEach(() => {
  toastCalls.length = 0
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  modelStore._resetForTests()
  detail._resetForTests()
  page._resetForTests()
  resetSources()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('SkillsToolsApp', () => {
  it('renders header tabs with keyboard switching across all four sections', async () => {
    setup()
    await mountPage('skills')

    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(4)
    expect(tabs[0]!.getAttribute('aria-selected')).toBe('true')

    await act(async () => { fireEvent.keyDown(tabs[0]!, { key: 'ArrowRight' }) })
    expect(store.get().tab).toBe('tools')
    expect(tabs[1]!.getAttribute('aria-selected')).toBe('true')

    await act(async () => { fireEvent.keyDown(tabs[1]!, { key: 'End' }) })
    expect(store.get().tab).toBe('plugins')
  })

  it('toggles Skill Hub, filters skills, opens centered skill modal, and handles uninstall', async () => {
    const { calls } = setup()
    const askMock = vi.fn()
    store.lendAskInChat(askMock)
    vi.spyOn(confirm, 'ask').mockImplementation((_t, _b, _l, onYes) => { onYes() })
    await mountPage('skills')

    const hubSwitch = document.querySelector<HTMLButtonElement>('.skillsTools-hub-bento .skillsTools-swi')!
    await act(async () => { fireEvent.click(hubSwitch) })
    expect(calls).toContainEqual(['set', { key: 'skillForge.router.hub.enabled', value: true }])

    await act(async () => { fireEvent.click(screen.getByText('gui.skillsTools.skills.add')) })
    expect(askMock).toHaveBeenCalledWith('gui.skillsTools.skills.add_prompt')

    await act(async () => { fireEvent.click(screen.getByText('sql-style')) })
    expect(detail.get().owner).toBe('skillsTools')
    expect(document.querySelector('#dBody .skillsTools-sheet')).toBeTruthy()
    expect(calls).toContainEqual(['inspectSkill', 'sql-style'])

    await act(async () => { fireEvent.click(screen.getByText('gui.skillsTools.skills.uninstall')) })
    expect(calls).toContainEqual(['uninstallSkill', 'sql-style'])
  })

  it('opens centered ToolModal for configurable tools, saves keys, and explains locked builtin switches', async () => {
    const { calls } = setup()
    await mountPage('tools')

    expect(screen.getByText('gui.skillsTools.tools.featured_title')).toBeTruthy()
    expect(screen.getByText('gui.skillsTools.tools.matrix_title')).toBeTruthy()

    const featuredCard = document.querySelector<HTMLElement>('.skillsTools-cfg-card')!
    await act(async () => { fireEvent.click(featuredCard) })
    expect(detail.get().owner).toBe('skillsTools')
    expect(document.querySelector('#dBody .skillsTools-sheet')).toBeTruthy()

    const input = document.querySelector<HTMLInputElement>('#dBody .keyfield input')!
    fireEvent.change(input, { target: { value: 'sk-test-key' } })
    const saveBtn = document.querySelector<HTMLButtonElement>('#dBody .skillsTools-dinput-row .skillsTools-btn')!
    await act(async () => { fireEvent.click(saveBtn) })
    expect(calls).toContainEqual(['set', { key: 'tools.web.providers.serper.apiKey', value: 'sk-test-key' }])

    const fixedSwitch = document.querySelector<HTMLButtonElement>('.skillsTools-swi-fixed')!
    fireEvent.click(fixedSwitch)
    expect(toastCalls).toContain('gui.skillsTools.tools.builtin_locked')
  })

  it('opens centered McpModal for failed servers with retry and credential fields', async () => {
    const { calls } = setup()
    await mountPage('mcp')

    await act(async () => { fireEvent.click(screen.getByText('github')) })
    expect(detail.get().owner).toBe('skillsTools')
    expect(calls).toContainEqual(['serverDetail', 'github'])

    const retryBtn = document.querySelector<HTMLButtonElement>('#dBody .skillsTools-failbox .skillsTools-btn')!
    await act(async () => { fireEvent.click(retryBtn) })
    expect(calls).toContainEqual(['retryServer', 'github'])
  })

  it('renders Agent Plugin packages with inline MCP switch and detail modal', async () => {
    const { calls } = setup()
    await mountPage('plugins')

    expect(screen.getByText('docs-kit')).toBeTruthy()
    expect(screen.getByText('fresh-pkg')).toBeTruthy()

    const switches = document.querySelectorAll<HTMLButtonElement>('.skillsTools-pkg-grid .skillsTools-tray-card .skillsTools-swi')
    expect(switches).toHaveLength(2)
    await act(async () => { fireEvent.click(switches[1]!) })
    expect(calls).toContainEqual(['setPackageMcp', { name: 'fresh-pkg', on: true }])

    await act(async () => { fireEvent.click(screen.getByText('docs-kit')) })
    expect(detail.get().owner).toBe('skillsTools')
    expect(document.querySelector('#dBody .skillsTools-sheet')).toBeTruthy()
  })
})
