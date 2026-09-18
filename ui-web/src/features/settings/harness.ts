/* The settings tests' stand-ins: a snapshot with one of everything the pages
   draw, a recording SettingsSource on the seam, the shell's four elements,
   and a translator that answers its key. Test-only; nothing in the page
   imports it. */
import { act, render } from '@testing-library/react'
import { createElement } from 'react'
import { vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as settingsDialog from '../../state/settings'
import { setSources } from '../../state/sources'
import { SettingsApp } from './SettingsApp'
import * as store from './store'

import type { ProviderRow, SettingsSnapshot, SettingsSource } from './types'

export const providers = (): ProviderRow[] => [
  { id: 'anthropic', name: 'Anthropic', models: ['claude-opus-4-5', 'claude-sonnet-4-5'], configured: ['claude-opus-4-5', 'claude-sonnet-4-5'],
    on: true, kind: 'key', acceptsKey: true, keyUrl: 'https://console.anthropic.com/settings/keys',
    labels: { 'claude-opus-4-5': { label: 'Opus', description: 'the big one', context_window: 200000 } } },
  { id: 'openrouter', name: 'OpenRouter', models: ['openai/gpt-4o', 'anthropic/claude-sonnet-4-5', 'text-embedding-3-small'],
    configured: ['openai/gpt-4o', 'anthropic/claude-sonnet-4-5', 'text-embedding-3-small'], on: true, kind: 'key', acceptsKey: true,
    apiBase: 'https://openrouter.ai/api/v1', defaultApiBase: 'https://openrouter.ai/api/v1', headers: { 'X-Title': '****set****' } },
  { id: 'openai', name: 'OpenAI', models: [], configured: [], on: false, kind: 'key', acceptsKey: true, keyUrl: 'https://platform.openai.com/api-keys' },
  { id: 'minimax_global', name: 'MiniMax Global', models: [], configured: [], on: false, kind: 'oauth', acceptsKey: false },
  { id: 'ollama', name: 'Ollama', models: [], configured: [], on: false, kind: 'local', acceptsKey: false, needsBase: true },
]

export function snap(over: Partial<SettingsSnapshot> = {}): SettingsSnapshot {
  return {
    raw: {
      agents: { defaults: { model: 'claude-opus-4-5', provider: 'anthropic', reasoningEffort: 'low', maxToolIterations: 40 } },
      context: { curatorModel: 'anthropic/claude-sonnet-4-5', curatorProvider: 'openrouter' },
      tools: { disabledTools: ['image_generate', 'deep_research'], web: { search: { provider: 'serper' } } },
      skillForge: { blocklist: ['sql-style'] },
      sessions: { autoArchiveAfterDays: null },
    },
    configPath: '/home/me/.raven/config.json',
    everos: { available: true, sections: { llm: { model: 'openai/gpt-4o', base_url: 'https://openrouter.ai/api/v1', api_key_set: true } } },
    providers: providers(),
    curProvider: 'anthropic',
    model: 'claude-opus-4-5',
    tools: [
      { id: 'read_file', name: 'read', group: 'file', reach: 'local', one: 'reads', on: true },
      { id: 'exec', name: 'exec', group: 'run', reach: 'local', one: 'runs', on: true, danger: true },
      { id: 'web_search', name: 'search', group: 'net', reach: 'net', one: 'searches', on: true },
      { id: 'web_fetch', name: 'fetch', group: 'net', reach: 'net', one: 'fetches', on: true },
      { id: 'deep_research', name: 'research', group: 'net', reach: 'net', one: 'researches', on: false },
      { id: 'image_generate', name: 'draw', group: 'generate', reach: 'net', one: 'draws', on: false },
      { id: 'spawn', name: 'spawn', group: 'collab', reach: 'local', one: 'spawns', on: true },
    ],
    skills: [
      { name: 'git-flow', description: 'Branch and merge the house way', source: 'builtin', always: true, hub: false, hub_id: '' },
      { name: 'sql-style', description: 'Names and indents for the database', source: 'workspace', always: false, hub: true, hub_id: 'sh-sql-style' },
      { name: 'notes', description: 'Personal notes', source: 'workspace', always: false, hub: false, hub_id: '' },
    ],
    mcp: [
      { name: 'github', transport: 'http', state: 'error', connected: false, tool_count: 3, enabled: true, auth: 'apikey', credentialed: true, error: 'boom' },
      { name: 'asana', transport: 'sse', state: 'disconnected', connected: false, tool_count: 0, enabled: true, auth: 'oauth', credentialed: false },
      { name: 'context7', transport: 'http', state: 'connected', connected: true, tool_count: 2, enabled: true, auth: 'none', credentialed: null },
      { name: 'notion', transport: 'http', state: 'disconnected', connected: false, tool_count: 0, enabled: false, auth: 'oauth', credentialed: true },
    ],
    ...over,
  }
}

export type Call = [string, unknown]

/* A source that records every write and answers the same snapshot, so a
   test asserts what was asked of the gateway and nothing about the answer. */
export function install(data: SettingsSnapshot = snap(), over: Partial<SettingsSource> = {}): { source: SettingsSource; calls: Call[]; data: SettingsSnapshot } {
  const calls: Call[] = []
  const rec = (name: string, args: unknown): SettingsSnapshot => { calls.push([name, args]); return data }
  const source: SettingsSource = {
    load: async () => data,
    set: async (key, value) => rec('set', { key, value }),
    everosSet: async (section, fields, borrowFrom) => rec('everosSet', { section, fields, ...(borrowFrom ? { borrowFrom } : {}) }),
    usage: async (range) => { calls.push(['usage', range]); return null },
    provider: async (op, params) => rec('provider', { op, ...params }),
    fetchModels: async (slug) => { calls.push(['fetchModels', slug]); return { models: [], status: 'ok' } },
    addModels: async (slug, models) => rec('addModels', { slug, models }),
    setFields: async (slug, fields) => rec('setFields', { slug, fields }),
    oauthLogin: async (slug) => { calls.push(['oauthLogin', slug]); return { verification_uri: 'https://v.example/device', user_code: 'ABCD', expires_in: 900 } },
    pickModel: async (model, provider) => { calls.push(['pickModel', { model, provider }]) },
    model: () => data.model,
    defaultProvider: () => data.curProvider,
    archived: async () => { calls.push(['archived', null]); return [] },
    restore: async (id) => { calls.push(['restore', id]) },
    removeSession: async (id) => { calls.push(['removeSession', id]) },
    inspectSkill: async (name) => { calls.push(['inspectSkill', name]); return { name, description: 'd', path: `/skills/${name}`, body: '# Hi\n\nbody', files: ['SKILL.md', 'notes.md'], always: false, install: null } },
    openSkillFile: async (name, file) => { calls.push(['openSkillFile', { name, file }]) },
    uninstallSkill: async (name) => rec('uninstallSkill', name),
    toggleServer: async (name, on) => rec('toggleServer', { name, on }),
    retryServer: async (name) => rec('retryServer', name),
    revokeServer: async (name) => rec('revokeServer', name),
    configureServer: async (name, form) => rec('configureServer', { name, form }),
    authServer: async (name) => rec('authServer', name),
    version: () => '0.2.1',
    checkUpdate: (btn) => { calls.push(['checkUpdate', btn.textContent]) },
    setLang: (lang) => { calls.push(['setLang', lang]) },
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(settingsDialog, 'open').mockImplementation(() => {})
  vi.spyOn(settingsDialog, 'close').mockImplementation(() => {})
  setSources({ settings: source })
  document.body.innerHTML =
    '<div id="setModal"><div class="snavlist" id="snavList"></div><h3 id="setTitle"></h3>' +
    '<p class="sub" id="setSub"></p><div class="spanels" id="spanels"></div></div>'
  return { source, calls, data }
}

export async function mount(tab = 'general'): Promise<ReturnType<typeof render>> {
  settingsDialog.settingsTab.id = tab
  const view = render(createElement(SettingsApp), { container: document.getElementById('spanels')! })
  await act(async () => { await store.open() })
  return view
}

/* Every pending write settled and the page redrawn. */
export const settle = (): Promise<void> => act(async () => { await Promise.resolve(); await Promise.resolve() })
