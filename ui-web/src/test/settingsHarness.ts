/* The settings tests' stand-ins: a snapshot with one of everything the pages
   draw, a recording SettingsSource on the seam, the shell's four elements,
   and a translator that answers its key. Test-only; nothing in the page
   imports it. */
import { act, render } from '@testing-library/react'
import { createElement } from 'react'
import { vi } from 'vitest'

import { ModelApp } from '../features/model/ModelPicker'
import * as modelStore from '../features/model/store'
import { SettingsApp } from '../features/settings/SettingsApp'
import * as store from '../features/settings/store'
import { setTranslator } from '../i18n/t'
import * as settingsDialog from '../state/settings'

import type { ModelSource, Provider } from '../features/model/types'
import type { ProviderRow, SettingsSnapshot, SettingsSource } from '../features/settings/types'

/* Every label carries the `kind` the wire carries, because the surfaces read
   it: `model.options` runs `registry_data.kind_of` over each row, so a fixture
   without it describes a reply the gateway cannot send -- and every slot would
   see the whole list as text. The values here are what `kind_of` answers for
   these ids (the embedding one through `inferred_tags`, from its name). */
export const providers = (): ProviderRow[] => [
  { id: 'anthropic', name: 'Anthropic', models: ['claude-opus-4-5', 'claude-sonnet-4-5'], configured: ['claude-opus-4-5', 'claude-sonnet-4-5'],
    on: true, kind: 'key', acceptsKey: true, keyUrl: 'https://console.anthropic.com/settings/keys',
    labels: { 'claude-opus-4-5': { label: 'Opus', description: 'the big one', context_window: 200000, kind: 'text' } } },
  { id: 'openrouter', name: 'OpenRouter', models: ['openai/gpt-4o', 'anthropic/claude-sonnet-4-5', 'text-embedding-3-small', 'google/gemini-2.5-flash-image'],
    configured: ['openai/gpt-4o', 'anthropic/claude-sonnet-4-5', 'text-embedding-3-small', 'google/gemini-2.5-flash-image'],
    on: true, kind: 'key', acceptsKey: true, gateway: true,
    /* Only the two the wire would describe: `_model_labels` skips a model no
       catalogue names and no tag reaches, and these two are reached by
       `inferred_tags` and by a catalogue row. The label is the id, which is what
       `describe` falls back to. */
    labels: {
      'text-embedding-3-small': { label: 'text-embedding-3-small', kind: 'embedding' },
      'google/gemini-2.5-flash-image': { label: 'gemini-2.5-flash-image', kind: 'image' },
    },
    apiBase: 'https://openrouter.ai/api/v1', defaultApiBase: 'https://openrouter.ai/api/v1', headers: { 'X-Title': '****set****' } },
  { id: 'openai', name: 'OpenAI', models: [], configured: [], on: false, kind: 'key', acceptsKey: true, keyUrl: 'https://platform.openai.com/api-keys' },
  { id: 'minimax_global', name: 'MiniMax Global', models: [], configured: [], on: false, kind: 'oauth', acceptsKey: false },
  { id: 'ollama', name: 'Ollama', models: [], configured: [], on: false, kind: 'local', acceptsKey: true, needsBase: true },
]

/* The same rows on the model domain's seam. The live page fills both from one
   `model.options` reply, so a test that gave them different lists would be
   testing a page that cannot exist -- and the roles card now opens the
   composer's picker, which reads this one. */
const asModelProvider = (p: ProviderRow): Provider => ({
  id: p.id, name: p.name, models: p.models, configured: p.configured,
  on: p.on, kind: p.kind, labels: p.labels, gateway: p.gateway,
  current: p.id === 'anthropic',
})

function modelSourceFor(rows: ProviderRow[], calls: Call[]): ModelSource {
  return {
    providers: () => rows.map(asModelProvider),
    persist: async (model, provider, scope) => { calls.push(['persist', { model, provider, scope }]) },
    addModel: async (model, provider, kind) => { calls.push(['addModel', { model, provider, kind }]) },
    openSettings: () => { calls.push(['openSettings', null]) },
  }
}

let liveModel: ModelSource | null = null

/* The model domain's seam, the twin of `source` below. A file that mounts the
   dialog installs both: the pages read the settings source, and the roles card
   opens the model domain's picker. */
export const modelSource: ModelSource = new Proxy({} as ModelSource, {
  get: (_t, prop: string) => {
    if (!liveModel) throw new Error('install() first')
    return (liveModel as unknown as Record<string, unknown>)[prop]
  },
})

export function snap(over: Partial<SettingsSnapshot> = {}): SettingsSnapshot {
  return {
    raw: {
      agents: { defaults: { model: 'claude-opus-4-5', provider: 'anthropic', reasoningEffort: 'low', maxToolIterations: 40 } },
      context: { curatorModel: 'anthropic/claude-sonnet-4-5', curatorProvider: 'openrouter' },
      tools: { disabledTools: ['image_generate', 'write_file'], web: { search: { provider: 'serper' } } },
      skillForge: { blocklist: ['sql-style'] },
      sessions: { autoArchiveAfterDays: null },
      providers: { anthropic: { modelOverlay: { 'claude-opus-4-5': { label: 'Opus', description: 'the big one' } } } },
    },
    configPath: '/home/me/.raven/config.json',
    everos: {
      available: true,
      owned: true,
      required: ['llm', 'embedding'],
      sections: { llm: { model: 'openai/gpt-4o', provider: 'openrouter', api_key_set: true } },
      supports: {
        openrouter: ['embedding', 'llm', 'multimodal', 'rerank'],
        anthropic: ['llm'],
        custom: ['embedding', 'llm', 'multimodal', 'rerank'],
        ollama: ['embedding', 'llm', 'multimodal', 'rerank'],
      },
    },
    providers: providers(),
    curProvider: 'anthropic',
    model: 'claude-opus-4-5',
    tools: [
      { id: 'read_file', name: 'read', group: 'file', reach: 'local', one: 'reads', on: true },
      { id: 'exec', name: 'exec', group: 'run', reach: 'local', one: 'runs', on: true, danger: true },
      { id: 'web_search', name: 'search', group: 'net', reach: 'net', one: 'searches', on: true },
      { id: 'web_fetch', name: 'fetch', group: 'net', reach: 'net', one: 'fetches', on: true },
      { id: 'image_generate', name: 'draw', group: 'generate', reach: 'net', one: 'draws', on: false },
      { id: 'spawn', name: 'spawn', group: 'collab', reach: 'local', one: 'spawns', on: true },
      /* The two sides `builtin` tells apart. `tool_search` is a meta-tool, so
         no entry in `tools.disabledTools` registers it and the page draws it
         fixed; `cancel_dag` is hidden from the schema and reached by name, and
         that same entry still takes it away, so it carries a working switch.
         The page has to draw them differently. */
      { id: 'tool_search', name: 'tool search', group: 'search', reach: 'local', one: 'finds tools', on: false, builtin: true },
      { id: 'tool_call', name: 'tool call', group: 'search', reach: 'local', one: 'calls a tool by name', on: true, builtin: true },
      { id: 'cancel_dag', name: 'cancel dag', group: 'collab', reach: 'local', one: 'cancels a dag', on: true, builtin: false },
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

/* The four elements the dialog's shell owns: App.tsx draws them and the island
   portals into them, so they are the page's markup rather than the domain's.
   mountPageRoot() renders the real thing where a case can afford the whole
   page; this is the cheap stand-in for the ones that only need somewhere to
   portal into. */
export const SETTINGS_SHELL =
  '<div id="setModal"><div class="snavlist" id="snavList"></div><h3 id="setTitle"></h3>'
  + '<p class="sub" id="setSub"></p><div class="spanels" id="spanels"></div></div>'

let live: SettingsSource | null = null

/* What a test puts on the seam, once per file. `setSources` may be named only
   in a `*.test.ts` file (CONTRIBUTING section 5.3), so the harness cannot
   install itself; this stand-in keeps one identity across the installs a file
   makes and forwards every call to whatever install() built last. */
export const source: SettingsSource = new Proxy({} as SettingsSource, {
  get: (_target, key) => {
    if (!live) throw new Error('install() builds the settings source; call it before the page reads one')
    return (live as unknown as Record<string | symbol, unknown>)[key]
  },
})

/* A source that records every write and answers the same snapshot, so a
   test asserts what was asked of the gateway and nothing about the answer. */
export function install(data: SettingsSnapshot = snap(), over: Partial<SettingsSource> = {}): { source: SettingsSource; calls: Call[]; data: SettingsSnapshot } {
  const calls: Call[] = []
  const rec = (name: string, args: unknown): SettingsSnapshot => { calls.push([name, args]); return data }
  const built: SettingsSource = {
    load: async () => data,
    set: async (key, value) => rec('set', { key, value }),
    everosSet: async (section, model, provider, protocol) =>
      rec('everosSet', { section, model, provider, ...(protocol ? { protocol } : {}) }),
    usage: async (range) => { calls.push(['usage', range]); return null },
    provider: async (op, params) => rec('provider', { op, ...params }),
    fetchModels: async (slug) => { calls.push(['fetchModels', slug]); return { models: [], status: 'ok' } },
    addModels: async (slug, models) => rec('addModels', { slug, models }),
    setFields: async (slug, fields) => rec('setFields', { slug, fields }),
    oauthLogin: async (slug) => { calls.push(['oauthLogin', slug]); return { verification_uri: 'https://v.example/device', user_code: 'ABCD', expires_in: 900 } },
    pickModel: async (model, provider) => { calls.push(['pickModel', { model, provider }]); return false },
    model: () => data.model,
    defaultProvider: () => data.curProvider,
    archived: async () => { calls.push(['archived', null]); return [] },
    restore: async (id) => { calls.push(['restore', id]) },
    removeSession: async (id) => { calls.push(['removeSession', id]) },
    inspectSkill: async (name) => { calls.push(['inspectSkill', name]); return { name, description: 'd', path: `/skills/${name}`, body: '# Hi\n\nbody', files: ['SKILL.md', 'notes.md'], always: false, install: null } },
    openSkillFile: async (name, file) => { calls.push(['openSkillFile', { name, file }]) },
    uninstallSkill: async (name) => rec('uninstallSkill', name),
    /* A read the credential panel makes on its own, so it is not recorded: a
       case asserting what a row's buttons wrote would have to skip past it. */
    serverDetail: async () => ({
      known: true,
      fields: [{ key: 'token', label: 'Token', help_url: 'https://github.com/settings/tokens' }],
      address: 'https://api.githubcopilot.com/mcp',
      tools: ['list_issues', 'get_pr'],
    }),
    toggleServer: async (name, on) => rec('toggleServer', { name, on }),
    retryServer: async (name) => rec('retryServer', name),
    revokeServer: async (name) => rec('revokeServer', name),
    configureServer: async (name, form) => rec('configureServer', { name, form }),
    authServer: async (name) => rec('authServer', name),
    version: () => '0.2.1',
    checkUpdate: async (btn) => { calls.push(['checkUpdate', btn.textContent]) },
    newerVersion: () => null,
    upgrade: () => { calls.push(['upgrade', '']) },
    setLang: (lang) => { calls.push(['setLang', lang]) },
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(settingsDialog, 'open').mockImplementation(() => {})
  vi.spyOn(settingsDialog, 'close').mockImplementation(() => {})
  live = built
  document.body.innerHTML = SETTINGS_SHELL
  liveModel = modelSourceFor(data.providers, calls)
  return { source, calls, data }
}

/** Forgets the source install() built, so the next file starts from nothing. */
export function _resetForTests(): void {
  live = null
  liveModel = null
  modelStore._resetForTests()
}

export async function mount(tab = 'general'): Promise<ReturnType<typeof render>> {
  settingsDialog.settingsTab.id = tab
  const view = render(createElement(SettingsApp), { container: document.getElementById('spanels')! })
  /* The picker is one node at the body, not a child of the dialog: main.tsx
     mounts it there and a settings row opens it by a call. A harness that
     rendered only the dialog would make every "open the picker" case silently
     assert nothing. */
  render(createElement(ModelApp), { container: document.body.appendChild(document.createElement('div')) })
  await act(async () => { await store.open() })
  return view
}

/* Every pending write settled and the page redrawn. */
export const settle = (): Promise<void> => act(async () => { await Promise.resolve(); await Promise.resolve() })
