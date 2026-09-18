/* Which providers this install can reach, and the two writes the key form
 * makes.
 *
 * Two libraries in one file: the seven rows the settings page has always shown,
 * and the nine-row first-run canvas `?onboard=demo` opens -- which is an
 * override group rather than part of the offline library, because it is asked
 * for by URL and is wanted on a live page too (a first-run flow that writes
 * nothing is exactly what there was no way to look at before).
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { Overrides } from '../overrideTransport'

type Provider = ResultOf<'model.options'>['providers'][number]

/* One row as it is written here; the rest of the wire shape is filled in by
   `wire` below, since a canned list has no protocol overrides or platform
   tags to report. */
interface Entry {
  slug: string
  name: string
  homepage: string
  models: string[]
  authenticated: boolean
  auth_type: string
  default_api_base?: string
  needs_api_base?: boolean
  /** Where the vendor hands out keys; the registry carries it for a few. */
  key_url?: string
}

const PROVIDERS: Entry[] = [
  { slug: 'minimax', name: 'MiniMax (Global)', homepage: 'https://platform.minimax.io/',
    models: ['minimax/MiniMax-M3', 'minimax/MiniMax-M2'], authenticated: true, auth_type: 'key' },
  { slug: 'minimax_cn_api', name: 'MiniMax (CN)', homepage: 'https://platform.minimaxi.com/',
    models: ['minimax-cn-api/MiniMax-M3', 'minimax-cn-api/MiniMax-M2'], authenticated: false,
    auth_type: 'endpoint', default_api_base: 'https://api.minimaxi.com/v1/' },
  { slug: 'anthropic', name: 'Anthropic', homepage: 'https://anthropic.com/', key_url: 'https://console.anthropic.com/settings/keys',
    models: ['claude-opus-4-5', 'claude-sonnet-4-6'], authenticated: true, auth_type: 'key' },
  { slug: 'openai', name: 'OpenAI', homepage: 'https://openai.com/', key_url: 'https://platform.openai.com/api-keys',
    models: ['gpt-5.1', 'gpt-5-mini'], authenticated: false, auth_type: 'key' },
  { slug: 'deepseek', name: 'DeepSeek', homepage: 'https://deepseek.com/', key_url: 'https://platform.deepseek.com/api_keys',
    models: ['deepseek-v3.2'], authenticated: false, auth_type: 'key' },
  { slug: 'nvidia_nim', name: 'NVIDIA', homepage: 'https://build.nvidia.com/explore/discover',
    models: ['nvidia-nim/nvidia/nemotron-3-super-120b-a12b', 'nvidia-nim/openai/gpt-oss-120b'],
    authenticated: false, auth_type: 'key', default_api_base: 'https://integrate.api.nvidia.com/v1' },
  { slug: 'lm_studio', name: 'LM Studio', homepage: 'https://lmstudio.ai/', models: [],
    authenticated: false, auth_type: 'local', needs_api_base: true,
    default_api_base: 'http://localhost:1234/v1' },
]

/* The first-run canvas's own list. Nine rows rather than seven, and every one
   of them unauthenticated: this is the screen a fresh install opens on, so a
   row that is already signed in would hide the whole of what it is for. */
const ONBOARD: Entry[] = [
  { slug: 'anthropic', name: 'Anthropic', auth_type: 'key', authenticated: false,
    homepage: 'https://anthropic.com/',
    models: ['claude-fable-5', 'claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5'] },
  { slug: 'openai', name: 'OpenAI', auth_type: 'key', authenticated: false,
    homepage: 'https://openai.com/', models: ['gpt-5.2', 'gpt-5.2-mini', 'o5', 'gpt-4.1'] },
  { slug: 'minimax_global', name: 'MiniMax Global', auth_type: 'oauth', authenticated: false,
    homepage: 'https://platform.minimax.io/', models: ['MiniMax-M2.5', 'MiniMax-M2'] },
  { slug: 'deepseek', name: 'DeepSeek', auth_type: 'key', authenticated: false,
    homepage: 'https://deepseek.com/', models: ['deepseek-chat', 'deepseek-reasoner'] },
  { slug: 'minimax', name: 'MiniMax (Global)', auth_type: 'key', authenticated: false,
    homepage: 'https://platform.minimax.io/', models: ['minimax/MiniMax-M3', 'minimax/MiniMax-M2.5'] },
  { slug: 'minimax_cn_api', name: 'MiniMax (CN)', auth_type: 'endpoint', authenticated: false,
    homepage: 'https://platform.minimaxi.com/', default_api_base: 'https://api.minimaxi.com/v1/',
    models: ['minimax-cn-api/MiniMax-M3'] },
  { slug: 'nvidia_nim', name: 'NVIDIA', auth_type: 'key', authenticated: false,
    homepage: 'https://build.nvidia.com/explore/discover',
    default_api_base: 'https://integrate.api.nvidia.com/v1',
    models: ['nvidia-nim/nvidia/nemotron-3-super-120b-a12b', 'nvidia-nim/openai/gpt-oss-120b'] },
  { slug: 'lm_studio', name: 'LM Studio', auth_type: 'local', needs_api_base: true, authenticated: false,
    homepage: 'https://lmstudio.ai/', default_api_base: 'http://localhost:1234/v1', models: [] },
  { slug: 'ollama', name: 'Ollama', auth_type: 'local', needs_api_base: true, authenticated: false,
    homepage: 'https://ollama.com/', models: ['qwen3:32b', 'llama4:70b'] },
]

const wire = (e: Entry, current: string): Provider => ({
  slug: e.slug, name: e.name, homepage: e.homepage, authenticated: e.authenticated,
  is_current: e.slug === current, auth_type: e.auth_type, models: e.models,
  configured_models: e.models, total_models: e.models.length,
  needs_api_base: !!e.needs_api_base, warning: '',
  key_url: e.key_url ?? null,
  extra_headers: {},
  ...(e.default_api_base ? { default_api_base: e.default_api_base } : {}),
})

export interface ModelFixture {
  fixtures: Fixtures
}

export function createModel(_env: FixtureEnv): ModelFixture {
  const rows = PROVIDERS.map((p) => ({ ...p, models: [...p.models] }))
  const model = 'claude-fable-5'
  const provider = 'anthropic'
  const find = (slug: string): Entry | undefined => rows.find((p) => p.slug === slug)

  return {
    fixtures: {
      'model.options': () => ({ model, provider, providers: rows.map((p) => wire(p, provider)) }),
      'model.save_key': (p) => {
        const row = find(p.slug)
        if (row) row.authenticated = true
        return { provider: wire(row || rows[0]!, provider) }
      },
      'model.disconnect': (p) => {
        const row = find(p.slug)
        if (row) row.authenticated = false
        return { disconnected: true }
      },
      'model.add_model': (p) => {
        const params = p as { slug: string; model: string }
        const row = find(params.slug)
        if (row && !row.models.includes(params.model)) row.models.push(params.model)
        return { provider: wire(row || rows[0]!, provider) }
      },
      'model.remove_model': (p) => {
        const params = p as { slug: string; model: string }
        const row = find(params.slug)
        if (row) row.models = row.models.filter((m) => m !== params.model)
        return { provider: wire(row || rows[0]!, provider) }
      },
      /* Nothing to ask: the drawer's list is whatever the row already
         declares, which is the honest answer with no provider behind it. */
      'model.fetch_models': (p) => ({
        models: (find((p as { slug: string }).slug) || { models: [] }).models.map((id) => ({
          id, label: id, kind: 'chat', added: true,
        })),
        status: 'ok',
      }),
      'model.add_models': (p) => {
        const row = find(p.slug)
        for (const m of p.models) if (row && !row.models.includes(m)) row.models.push(m)
        return { provider: wire(row || rows[0]!, provider) }
      },
      'model.set_fields': (p) => ({ previous: Object.fromEntries(Object.keys(p.fields).map((k) => [k, null])) }),
      /* A device flow with no vendor behind it: the code is shown and never
         lands, which is the page's "waiting" state. */
      'model.oauth_login': () => ({ verification_uri: 'https://example.com/device', user_code: 'ABCD-1234', expires_in: 900 }),
      'model.set_protocol': (p) => ({
        provider: wire(find((p as { slug: string }).slug) || rows[0]!, provider),
      }),
    },
  }
}

/**
 * The `?onboard=demo` group: the first-run flow on canned providers, so it
 * writes nothing.
 *
 * `setup.status` rides with the two model calls because the flow's last step
 * asks it whether a provider landed, and answering that from the real gateway
 * while the rows came from here would end the canned flow on the install's own
 * verdict.
 */
export function onboardDemoOverrides(schedule: (ms: number, fn: () => void) => void): Overrides {
  const rows = ONBOARD.map((p) => ({ ...p, models: [...p.models] }))
  let pokes = 0
  const wait = <T>(v: T, ms = 420): Promise<T> => new Promise((resolve) => { schedule(ms, () => resolve(v)) })

  return {
    'model.options': () => {
      /* Second re-probe flips the OAuth row to signed-in, so the preview can
         walk the "not yet -> try again -> through" path once. */
      pokes += 1
      if (pokes > 2) {
        const oauth = rows.find((p) => p.slug === 'minimax_global')
        if (oauth) oauth.authenticated = true
      }
      return wait({ model: '', provider: '', providers: rows.map((p) => wire(p, '')) })
    },
    'model.save_key': (p) => {
      const row = rows.find((x) => x.slug === p.slug)
      /* The contract's answer is a provider, so a slug this library does not
         have is an error rather than a null one -- which is what it used to
         answer, behind a cast, for a shape the contract forbids. */
      if (!row) throw new Error(`no provider ${p.slug}`)
      row.authenticated = true
      return wait({ provider: wire(row, '') })
    },
    'setup.status': () => wait({ provider_configured: false }, 300),
  }
}
