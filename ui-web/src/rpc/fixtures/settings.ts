/* What this install is configured as, and the three rungs a conversation runs
 * at.
 *
 * `settings.get` and `config.get` are one store here: the settings dialog
 * writes a key and reads the whole thing back, so a write that did not land in
 * the same place the read comes from would be a canvas that forgets. The tier
 * sentences below are raven's own, carried because the row draws the
 * description.
 */

import type { ExtFixture } from './ext'
import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

import { LANG } from '../../legacy/demo/010-kernel.js'

type Rung = NonNullable<ResultOf<'session.set_mode'>['availableModes']>[number]

/* The tier fixture: three rungs behind the same `session.set_mode` the live
   page calls.

   The sentences are `raven/config/schema.py:_TIER_TEXTS` and their entries in
   `raven/i18n/zh.py`, verbatim in both languages, because ONE PER RUNG is the
   part of this control worth reviewing: the row draws the description, so a
   sentence of this file's own invention -- and especially one sentence repeated
   with the id swapped in -- previews a line height and a wrap the live UI never
   receives. Keyed to `LANG` for the same reason: raven translates these three
   itself, so the canvas can only show the Chinese lines by carrying them. */
const TIER_SUB: Record<string, Record<string, string>> = {
  en: {
    medium: 'The least effort a sub-agent is asked for.',
    high: 'The middle amount of effort, between the other two.',
    max: 'The most effort a sub-agent is asked for.',
  },
  zh: {
    medium: '子代理被要求付出的最少努力。',
    high: '居中的投入，介于另外两档之间。',
    max: '子代理被要求付出的最多努力。',
  },
}
const tierMenu = (): Rung[] => ['medium', 'high', 'max'].map((id) => ({
  id,
  name: id.charAt(0).toUpperCase() + id.slice(1),
  description: (TIER_SUB[LANG] || TIER_SUB.en!)[id]!,
}))

/* The config a settings read answers with. Deep enough to exercise the rows
   the dialog draws and nothing more: the default pair the model control edits,
   and the two disabled lists the tool and plugin rows toggle. */
function seed(ext: ExtFixture): Record<string, unknown> {
  return {
    /* No `language`: an install that never picked one, which is what a fresh
       config is. The page keeps the catalogue it booted with -- `loadLang`
       only moves it for an explicit 'en' or 'zh' -- and the pick still writes
       one here, so the setting works from the offline page too. */
    model: 'claude-fable-5',
    agents: { defaults: { model: 'claude-fable-5', provider: 'anthropic' } },
    tools: { disabledTools: ext.disabledTools },
    plugins: { disabled: ext.disabledPlugins },
    permissions: { mode: 'ask' },
  }
}

/* One dotted key into the nested config, which is the shape `settings.set`
   writes and `config.get` reads back. */
function put(into: Record<string, unknown>, key: string, value: unknown): void {
  const parts = key.split('.')
  let at = into
  for (const part of parts.slice(0, -1)) {
    if (typeof at[part] !== 'object' || at[part] === null) at[part] = {}
    at = at[part] as Record<string, unknown>
  }
  at[parts[parts.length - 1]!] = value
}

function pick(from: Record<string, unknown>, key: string): unknown {
  return key.split('.').reduce<unknown>((at, part) => (
    at && typeof at === 'object' ? (at as Record<string, unknown>)[part] : undefined
  ), from)
}

/* A ledger with nothing in it. The four `*_missing_calls` counts are part of
   the shape rather than decoration: they are how the panel says an answer is
   incomplete, and zero is the truthful value where nothing was spent. */
const ZERO_TOTALS: ResultOf<'settings.usage'>['llm']['total'] = {
  calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
  cost_usd: 0, cost_missing_calls: 0, cache_read_missing_calls: 0,
  cache_write_missing_calls: 0, legacy_cost_calls: 0,
}

export interface SettingsFixture {
  fixtures: Fixtures
}

export function createSettings(env: FixtureEnv, ext: ExtFixture): SettingsFixture {
  const config = seed(ext)
  let tier = 'high'

  return {
    fixtures: {
      /* What the page learns about the gateway it is talking to. The
         capability list is the one a current gateway announces
         (raven/rpc/methods/system.py), because a canvas that announced less
         would be exercising the tolerances rather than the page. */
      'system.hello': () => ({
        server_version: '0.1.0',
        server_capabilities: ['jsonrpc-2.0', 'subscriptions', 'cli-dispatch'],
        session: { default_channel: 'gui', default_session_key: '' },
      }),
      /* No `update_available`: the notice row stays hidden, which is the state
         a page with nothing to install is in. */
      'system.version': () => ({ server_version: '0.1.0', schema_version: '1', raven_version: '0.1.0' }),
      'system.ping': () => ({ pong: true, server_time_ms: env.now() }),
      'setup.status': () => ({ provider_configured: true }),
      'config.get': (p) => {
        const keys = (p as { keys?: string[] }).keys || []
        const out: Record<string, unknown> = {}
        for (const key of keys) out[key] = pick(config, key)
        return { config: out } as ResultOf<'config.get'>
      },
      'config.set': (p) => {
        const params = p as { key: string; value: unknown }
        const previous = pick(config, params.key)
        put(config, params.key, params.value)
        /* `null`, not an omission: the contract makes `previous` required and
           nullable, and "there was nothing here" is one of its answers. */
        return { applied: true, previous: (previous ?? null) as ResultOf<'config.set'>['previous'] }
      },
      'config.unset': (p) => {
        const key = (p as { key: string }).key
        const previous = pick(config, key)
        put(config, key, null)
        return {
          removed: previous !== undefined,
          previous: (previous ?? null) as ResultOf<'config.unset'>['previous'],
          default: null,
        }
      },
      'settings.get': () => ({
        settings: JSON.parse(JSON.stringify(config)) as ResultOf<'settings.get'>['settings'],
        config_path: '~/.raven/config.json',
        raven_version: '0.1.0',
      }),
      'settings.set': (p) => {
        const params = p as { key: string; value: unknown }
        put(config, params.key, params.value)
        if (params.key === 'tools.disabledTools') ext.disabledTools = (params.value as string[]) || []
        if (params.key === 'plugins.disabled') ext.disabledPlugins = (params.value as string[]) || []
        return { applied: true, previous: null as ResultOf<'settings.set'>['previous'] }
      },
      /* Nothing has been spent on this canvas, so the usage panel draws an
         empty ledger rather than a number nothing produced. */
      'settings.usage': () => ({
        days: 30,
        llm: { total: ZERO_TOTALS, models: [] },
        tools: { total: 0, counts: [] },
      }),
      'settings.everos': () => ({ sections: {}, config_path: '~/.raven/config.json', available: false }),
      'settings.everosSet': () => ({ applied: true }),
      'session.set_mode': (p) => {
        const mode = (p as { mode?: string }).mode
        if (mode) tier = mode
        return { mode: tier, availableModes: tierMenu() }
      },
      /* An upgrade with no gateway to replace: nothing was started, and the
         card reads the status rather than waiting on a restart that will not
         come. */
      'system.upgrade': () => ({
        status: 'refused', from_version: '0.1.0', to_version: '0.1.0', relaunch: false,
      }),
    },
  }
}

export { TIER_SUB }
