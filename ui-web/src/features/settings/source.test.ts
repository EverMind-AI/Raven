// @vitest-environment happy-dom
/* The rpc source, against a fake gateway: which method each verb calls, with
   what, and what it says afterwards. */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

type Source = typeof import('./source')

const seen: Array<[string, unknown]> = []
const toasts: string[] = []
const opened: string[] = []
const railReloads: number[] = []

async function load(answers: Record<string, unknown> = {}): Promise<Source> {
  seen.length = 0
  toasts.length = 0
  opened.length = 0
  railReloads.length = 0
  const mod = await loadPart(() => import('./source'), {
    fakes: {
      'src/state/toast': { show: (text: string) => { toasts.push(text) } },
      'src/lib/openUrl': { open: (url: string) => { opened.push(url) } },
      'src/features/rail/source': { loadSessions: async () => { railReloads.push(1) } },
      'src/state/banner': { draw: () => {} },
      'src/i18n/t': { t: (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key) },
    },
  }) as Source
  await fakeGateway(async (method: string, params: unknown) => {
    seen.push([method, params])
    if (method in answers) return answers[method]
    if (method === 'settings.get') return { settings: {}, config_path: '/c.json' }
    if (method === 'model.options') return { model: 'm', provider: 'p', providers: [] }
    if (method === 'ext.list') return { skills: [], plugins: [], tools: [], mcp: [] }
    if (method === 'settings.everos') return { sections: {}, config_path: '', available: false }
    if (method === 'config.get') return { config: {} }
    return {}
  })
  return mod
}

afterEach(() => { vi.restoreAllMocks() })

describe('settings source', () => {
  it('set writes the key, reloads, and speaks the server warning over the fixed saved', async () => {
    const mod = await load({ 'settings.set': { applied: true, previous: null, warning: 'Applies after a restart' } })
    await mod.settingsSource.set('agents.defaults.maxToolIterations', 50)
    expect(seen[0]).toEqual(['settings.set', { key: 'agents.defaults.maxToolIterations', value: 50 }])
    expect(seen.map(([m]) => m)).toContain('settings.get')
    expect(toasts).toEqual(['Applies after a restart'])
  })

  it('set with no warning says saved, and a refusal toasts the detail and throws handled', async () => {
    const mod = await load({ 'settings.set': { applied: true, previous: null } })
    await mod.settingsSource.set('language', 'en')
    expect(toasts).toEqual(['gui.settings.saved'])
    await fakeGateway(async () => { throw { message: 'nope', data: { detail: 'tools.web.proxy is not editable here' } } })
    await expect(mod.settingsSource.set('tools.web.proxy', 'x')).rejects.toEqual({ handled: true })
    expect(toasts[1]).toContain('tools.web.proxy is not editable here')
  })

  it('usage asks for the two dates, inclusive', async () => {
    const mod = await load({ 'settings.usage': { days: 2 } })
    await mod.settingsSource.usage({ from: '2026-09-01', to: '2026-09-02' })
    expect(seen).toEqual([['settings.usage', { from: '2026-09-01', to: '2026-09-02' }]])
  })

  it('restore un-archives and then reloads the rail, which is what puts the row back', async () => {
    const mod = await load({ 'session.archive': { archived: false, session_key: 'tui:1', pending: false } })
    await mod.settingsSource.restore('tui:1')
    expect(seen).toEqual([['session.archive', { session_id: 'tui:1', archived: false }]])
    expect(railReloads).toEqual([1])
  })

  it('archived lists only the archived sessions', async () => {
    const mod = await load({ 'session.list': { sessions: [{ id: 'a' }] } })
    expect(await mod.settingsSource.archived()).toEqual([{ id: 'a' }])
    expect(seen).toEqual([['session.list', { archived: true }]])
  })

  it('oauthLogin opens the verification page on this browser and returns the code', async () => {
    const mod = await load({ 'model.oauth_login': { verification_uri: 'https://v.example/device', user_code: 'ABCD-1234', expires_in: 900 } })
    const r = await mod.settingsSource.oauthLogin('minimax_global')
    expect(r.user_code).toBe('ABCD-1234')
    expect(opened).toEqual(['https://v.example/device'])
    expect(seen[0]).toEqual(['model.oauth_login', { slug: 'minimax_global' }])
  })

  it('authServer opens the consent page the gateway hands back, then reloads the inventory', async () => {
    const mod = await load({ 'plug.auth': { name: 'asana', mcp: { name: 'asana', auth_url: 'https://asana.example/consent' } } })
    await mod.settingsSource.authServer('asana')
    expect(opened).toEqual(['https://asana.example/consent'])
    expect(seen.map(([m]) => m)).toEqual(['plug.auth', 'ext.list', 'settings.get'])
  })

  it('setFields sends the patch as given, and addModels sends the whole batch', async () => {
    const mod = await load({ 'model.set_fields': { previous: {} }, 'model.add_models': { provider: {} } })
    await mod.settingsSource.setFields('openrouter', { extra_headers: { 'X-Title': 'raven' } })
    await mod.settingsSource.addModels('openrouter', ['a', 'b'])
    expect(seen.filter(([m]) => m.startsWith('model.') || m === 'settings.get')).toEqual([
      ['model.set_fields', { slug: 'openrouter', fields: { extra_headers: { 'X-Title': 'raven' } } }],
      ['settings.get', {}],
      ['model.options', {}],
      ['model.add_models', { slug: 'openrouter', models: ['a', 'b'] }],
      ['settings.get', {}],
      ['model.options', {}],
    ])
  })

  it('inspectSkill and openSkillFile speak skills.manage with the action and the file', async () => {
    const mod = await load({ 'skills.manage': { info: { name: 'x', body: '# x' }, opened: true } })
    const d = await mod.settingsSource.inspectSkill('x')
    expect(d.body).toBe('# x')
    await mod.settingsSource.openSkillFile('x', 'notes.md')
    expect(seen).toEqual([
      ['skills.manage', { action: 'inspect', query: 'x' }],
      ['skills.manage', { action: 'open', query: 'x', file: 'notes.md' }],
    ])
  })

  it('the plugin verbs name the server and reload the inventory afterwards', async () => {
    const mod = await load()
    await mod.settingsSource.toggleServer('github', false)
    await mod.settingsSource.retryServer('github')
    await mod.settingsSource.revokeServer('asana')
    await mod.settingsSource.configureServer('github', { token: '' })
    expect(seen.filter(([m]) => m.startsWith('plug.'))).toEqual([
      ['plug.toggle', { name: 'github', enabled: false }],
      ['plug.retry', { name: 'github' }],
      ['plug.revoke', { name: 'asana' }],
      ['plug.configure', { name: 'github', form: { token: '' } }],
    ])
    expect(seen.filter(([m]) => m === 'ext.list')).toHaveLength(4)
  })

  it('the snapshot carries the wire rows the inventory read, untouched', async () => {
    const mod = await load({ 'ext.list': {
      skills: [{ name: 's', description: 'd', source: 'builtin', always: true, hub: false, hub_id: '' }],
      plugins: [], tools: [],
      mcp: [{ name: 'm', transport: 'http', state: 'connected', connected: true, tool_count: 1, enabled: true, auth: 'oauth', credentialed: false }],
    } })
    const snap = await mod.settingsSource.load()
    expect(snap.skills[0]!.always).toBe(true)
    expect(snap.mcp[0]!.auth).toBe('oauth')
    expect(snap.configPath).toBe('/c.json')
  })
})
