/* What this install is configured as, and the three rungs a conversation runs
 * at.
 *
 * `settings.get` and `config.get` are one store here: the settings dialog
 * writes a key and reads the whole thing back, so a write that did not land in
 * the same place the read comes from would be a canvas that forgets. The tier
 * sentences below are raven's own, carried because the row draws the
 * description.
 */

import { code as LANG } from '../../i18n/t'

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { ExtFixture } from './ext'

type Rung = NonNullable<ResultOf<'session.set_mode'>['availableModes']>[number]
/* What a config value is on the wire: the contract's own JsonValue, which the
   store below holds so a read and a write speak the same type as the answer. */
type Json = ResultOf<'config.get'>['config'][string]

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
    medium: 'Faster and cheaper, for small, well-defined tasks.',
    high: 'The default: a balance of speed and quality.',
    max: 'Deepest reasoning and full sub-agent effort, for complex or open-ended work.',
  },
  zh: {
    medium: '子智能体投入最少。',
    high: '居中的投入。',
    max: '子智能体投入最多。',
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
function seed(ext: ExtFixture): Record<string, Json> {
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
function put(into: Record<string, Json>, key: string, value: Json): void {
  const parts = key.split('.')
  let at = into
  for (const part of parts.slice(0, -1)) {
    if (typeof at[part] !== 'object' || at[part] === null) at[part] = {}
    at = at[part] as Record<string, Json>
  }
  at[parts[parts.length - 1]!] = value
}

/* The two keys whose value the ext library holds as a list of names. A
   `settings.set` carries a JsonValue, so the list is read out of it rather than
   asserted to be one: a page sending something else is a page bug, and an
   empty list is the honest reading of it. */
function asStrings(value: Json): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

/* A deep copy, so a later write to the store is not a write into an answer the
   page is already holding. JSON rather than structuredClone: the offline page
   runs inside a happy-dom Window in the boot probes and in every gate that
   boots it, and that Window has no structuredClone -- a responder that threw
   there would take `loadExt`'s Promise.all down with it and leave three pages
   empty, which is what the walk probe caught. */
const clone = (value: Record<string, Json>): Record<string, Json> => JSON.parse(JSON.stringify(value))

const isoDay = (ms: number): string => new Date(ms).toISOString().slice(0, 10)
const DAY_MS = 86_400_000

/* The gateway's range rule (raven/rpc/methods/console.py _usage_range): from/to
   inclusive and clamped to 90 days back, else `days` back from today. */
function usageRange(p: { from?: string | null; to?: string | null; days?: number | null }, nowMs: number) {
  const today = Date.UTC(new Date(nowMs).getUTCFullYear(), new Date(nowMs).getUTCMonth(), new Date(nowMs).getUTCDate())
  const earliest = today - 89 * DAY_MS
  const parse = (v: string | null | undefined): number | null => (v ? Date.parse(`${v}T00:00:00Z`) : null)
  let from: number
  let to: number
  const f = parse(p.from)
  const t = parse(p.to)
  if (f !== null || t !== null) {
    to = Math.min(t ?? today, today)
    from = Math.max(f ?? earliest, earliest)
    if (from > to) from = to
  } else {
    const days = Math.max(1, Math.min(typeof p.days === 'number' ? p.days : 30, 90))
    to = today
    from = today - (days - 1) * DAY_MS
  }
  const dates: string[] = []
  for (let at = from; at <= to; at += DAY_MS) dates.push(isoDay(at))
  return { from: isoDay(from), to: isoDay(to), dates }
}

function pick(from: Record<string, Json>, key: string): Json | undefined {
  return key.split('.').reduce<Json | undefined>((at, part) => (
    at && typeof at === 'object' ? (at as Record<string, Json>)[part] : undefined
  ), from)
}

/* A ledger with nothing in it. The four `*_missing_calls` counts are part of
   the shape rather than decoration: they are how the panel says an answer is
   incomplete, and zero is the truthful value where nothing was spent. */
const ZERO_TOTALS: ResultOf<'settings.usage'>['llm']['total'] = {
  calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
  cost_usd: 0, cost_missing_calls: 0, cache_read_missing_calls: 0,
  cache_write_missing_calls: 0, legacy_cost_calls: 0,
  input_missing_calls: 0,
  output_missing_calls: 0,
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
        const out: Record<string, Json> = {}
        for (const key of p.keys || []) out[key] = pick(config, key) ?? null
        return { config: out }
      },
      'config.set': (p) => {
        const previous = pick(config, p.key)
        put(config, p.key, p.value)
        /* `null`, not an omission: the contract makes `previous` required and
           nullable, and "there was nothing here" is one of its answers. */
        return { applied: true, previous: previous ?? null }
      },
      'config.unset': (p) => {
        const previous = pick(config, p.key)
        put(config, p.key, null)
        return { removed: previous !== undefined, previous: previous ?? null, default: null }
      },
      'settings.get': () => ({
        settings: clone(config),
        config_path: '~/.raven/config.json',
        raven_version: '0.1.0',
      }),
      'settings.set': (p) => {
        put(config, p.key, p.value)
        if (p.key === 'tools.disabledTools') ext.disabledTools = asStrings(p.value)
        if (p.key === 'plugins.disabled') ext.disabledPlugins = asStrings(p.value)
        return { applied: true, previous: null, warning: null }
      },
      /* Nothing has been spent on this canvas, so the usage panel draws an
         empty ledger rather than a number nothing produced -- one zero bucket
         per day of whatever range is asked for, the way the gateway answers. */
      'settings.usage': (p) => {
        const { from, to, dates } = usageRange(p, env.now())
        return {
          days: dates.length,
          from,
          to,
          daily: dates.map((date) => ({ date, ...ZERO_TOTALS })),
          llm: { total: ZERO_TOTALS, models: [] },
          tools: { total: 0, counts: [] },
        }
      },
      'settings.everos': () => ({
        sections: {}, config_path: '~/.raven/config.json', available: false, owned: true, supports: {},
        required: ['llm', 'embedding'],
      }),
      'settings.everosSet': () => ({ applied: true, warning: null }),
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
