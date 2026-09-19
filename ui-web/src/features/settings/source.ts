/* -- settings: the rpc source -----------------------------------------
   The settings island (ui-web/src/features/settings/) owns the dialog's
   drawing; this module speaks the gateway for it -- settings.*, the provider
   writes the model page makes, the archive's session calls, the skill and MCP
   calls of their two pages -- and holds what one read fills: the raw config,
   where it is on disk, and the EverOS sections. The model half is beside it in
   features/model/source.ts, which this one asks for the provider list and the
   default pair.

   The permission chip's refresh is here too: it mirrors what the gate reads
   for the visible conversation, which is a settings question rather than the
   chip's own. */

import { code as LANG, t } from '../../i18n/t'
import { open as openUrl } from '../../lib/openUrl'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { draw as drawBanner } from '../../state/banner'
import { setFromConfig as setPermMode } from '../../state/perm'
import { generation } from '../../state/session/generation'
import { staging } from '../../state/session/staging'
import { show as toast } from '../../state/toast'
import { extMcpRows, extSkillRows, extTools, loadExt } from '../installed/source'
import {
  defaultModel,
  defaultProvider,
  loadProviders,
  persistModel,
  providers,
  setDefaultPair,
  showModel,
} from '../model/source'
import { loadSessions } from '../rail/source'

import type { ParamsOf, ResultOf } from '../../rpc/generated'
import type { BannerSource } from '../../state/banner'
import type { AuthField, ProviderOp, SettingsSnapshot, SettingsSource, SkillDetail } from './types'

/* The config settings.get returned. Keys arrive camelCased
   (agents.defaults.reasoningEffort), one level per dot. Handed to the island
   inside its snapshot, which is the only reader. */
let RAW: Record<string, unknown> = {}

let configPathLive = '~/.raven/config.json'
let everosLive: ResultOf<'settings.everos'> | null = null

/* The three members that need page chrome no island owns: the version the foot
   learned from `system.version`, the check that drives the update notice and
   the upgrade prompt, and the language pick. Installed by
   features/settings/wire.ts, which owns the settings transport. */
interface SettingsChrome {
  version(): string | null
  checkUpdate(btn: HTMLButtonElement): void | Promise<void>
  setLang(v: string): void
}

let chrome: SettingsChrome = {
  version: () => null,
  checkUpdate: () => {},
  setLang: () => {},
}

export function setSettingsChrome(next: SettingsChrome): void {
  chrome = next
}

/* The permission mode a draft is staged to start on, or null. */
export function stagedPerm(): string | null {
  return staging().perm ?? null
}

async function loadEveros(): Promise<void> {
  try {
    everosLive = await gateway().call('settings.everos', {})
  } catch {
    everosLive = null
  }
}

/* Read the permission mode the gate applies to the visible conversation --
   the session's own when it has one, the default otherwise -- and paint the
   chip from it. */
export async function loadPermMode(sid?: string | null, gen?: number): Promise<void> {
  const ticket = gen !== undefined ? gen : generation()
  let r: ResultOf<'config.get'>
  try {
    r = await gateway().call('config.get', { keys: ['permissions.mode'], ...(sid ? { session_id: sid } : {}) })
  } catch {
    return
  }
  if (ticket !== generation()) return
  setPermMode(String(((r && r.config) || {})['permissions.mode'] || 'ask'))
}

export const pushPermMode = (): Promise<void> => loadPermMode(sessionCurrent())

/* The version check the About card's button runs; `check: true` asks now, not
   the daily cache. */
export const checkVersion = (): Promise<ResultOf<'system.version'>> =>
  gateway().call('system.version', { check: true })

/* The pick's write-back for a conversation. `applied` false is a refused
   write, which the caller reports. */
export const savePermMode = (mode: string, sid: string): Promise<boolean> =>
  gateway().call('config.set', { key: 'permissions.mode', value: mode, scope: 'session', session_id: sid })
    .then((r) => !!r.applied)

export async function loadSettings(): Promise<void> {
  const r = await gateway().call('settings.get', {})
  RAW = (r.settings || {}) as Record<string, unknown>
  configPathLive = r.config_path || configPathLive
  drawBanner()
  const agents = RAW.agents as { defaults?: { model?: string; provider?: string } } | undefined
  const defaults = (agents && agents.defaults) || {}
  // The configured default, kept apart from the visible session's model: the
  // settings default-model control shows and edits THIS pair (model AND
  // provider), while the composer chip shows whatever the open conversation
  // runs. Sharing one value made the settings control display the session's
  // model -- and badge the session's provider -- as the default.
  setDefaultPair(defaults.model || '', defaults.provider || '')
  if (defaults.model) showModel(defaults.model)
}

/* The settings plus the provider catalogue behind `model.options`. Deliberately
   not one function with the above: that catalogue is a live read of every
   configured vendor -- seconds on a home with several of them -- and no
   settings key changes what it answers. Paying it on every write is what made
   a tools switch sit busy for two seconds while the model list was fetched. */
export async function loadSettingsWithProviders(): Promise<void> {
  await loadSettings()
  try { await loadProviders() } catch { /* model options unavailable: keep the rows already shown */ }
}

export const settingsSnapshot = (): SettingsSnapshot => ({
  raw: RAW, configPath: configPathLive, everos: everosLive,
  // Both default-scoped on purpose: the settings page describes what new
  // conversations start on, so pairing the default model with the visible
  // session's provider badged the wrong row whenever the two scopes differ.
  providers: providers(), curProvider: defaultProvider(), model: defaultModel(),
  tools: extTools(), skills: extSkillRows(), mcp: extMcpRows(),
}) as SettingsSnapshot

/* The hub serves its text fields as i18n objects; which language wins is
   decided here, once, so the page only ever sees a plain string. */
const hubText = (v: unknown): string =>
  (v && typeof v === 'object'
    ? (v as Record<string, string>)[LANG] || (v as Record<string, string>).en || ''
    : String(v || ''))

/* `plughub.detail` declares its entry as a free JSON object, so the shape read
   out of it is this page's own. Only the MCP contribution's credential fields
   are read, which is all the plugins page asks for. */
interface AuthContribution {
  kind: string
  auth?: { fields?: AuthField[] }
}

const settingsErr = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* Every write goes through here: the failure is toasted where the wording
   lives, and the thrown handled tag tells the island to only redraw. */
async function run<T>(work: Promise<T>): Promise<T> {
  try {
    return await work
  } catch (e) {
    toast(t('gui.plug.op_failed', { err: settingsErr(e) }))
    throw { handled: true }
  }
}

/* The reload that follows a write, chosen by what the write changed. A
   provider write reloads the config too: the display names and the Azure
   fields are read from the provider's own section, not from model.options. */
const afterExt = async (): Promise<SettingsSnapshot> => { await loadExt(); return settingsSnapshot() }
const afterProviders = async (): Promise<SettingsSnapshot> => {
  await loadSettingsWithProviders()
  void pushPermMode()
  return settingsSnapshot()
}

/* The banner's draw is the shell's; what it draws for is this page's business.
   No websearch notice in live mode: a config gap belongs in the settings page,
   not as a strip above every conversation. A memory fault is not a config gap:
   it means the backend has stopped storing and has been handing back
   normal-looking replies the whole time, and live mode is the only mode where
   it can happen at all. Refusing one notice is a decision about that notice. */
export const bannerSource: BannerSource = {
  websearchNeeds: () => false,
}

export const settingsSource: SettingsSource = {
  load: async () => {
    /* The tool, skill and MCP inventory is part of settings. Loading it here
       keeps every opener on the island's one refresh path. */
    try { await loadExt() } catch (e) { toast(t('gui.op.load_failed', { detail: settingsErr(e) })) }
    await loadSettingsWithProviders()
    void pushPermMode()
    await loadEveros()
    return settingsSnapshot()
  },
  set: (key, value) => run((async () => {
    const r = await gateway().call('settings.set', { key, value: value as ParamsOf<'settings.set'>['value'] })
    await loadSettings()
    void pushPermMode()
    /* The server says when a save costs something -- a reload-only key, a
       swapped embedding model that invalidates every stored vector.
       Discarding the answer and toasting a fixed "saved" is how that reached
       nobody. */
    toast(r.warning || t('gui.settings.saved'))
    return settingsSnapshot()
  })()),
  /* A null fields object means "clear the section" (optional roles only). */
  everosSet: (section, fields, borrowFrom) => run((async () => {
    const p: ParamsOf<'settings.everosSet'> = fields ? { section, fields } : { section, clear: true }
    /* Only the name travels. The key stays where it is and the server copies
       it across -- what this page holds is `****set****`. */
    if (borrowFrom) p.borrow_from = borrowFrom
    const r = await gateway().call('settings.everosSet', p)
    await loadEveros()
    toast(r.warning || t('gui.settings.saved'))
    return settingsSnapshot()
  })()),
  usage: (range) => gateway().call('settings.usage', { from: range.from, to: range.to }),
  /* The four writes spelled out rather than built as `'model.' + op`: a
     composed name is a string nothing can check. */
  provider: (op: ProviderOp, params) => run((async () => {
    if (op === 'save_key') await gateway().call('model.save_key', params as unknown as ParamsOf<'model.save_key'>)
    else if (op === 'add_model') await gateway().call('model.add_model', params as unknown as ParamsOf<'model.add_model'>)
    else if (op === 'remove_model') await gateway().call('model.remove_model', params as unknown as ParamsOf<'model.remove_model'>)
    else if (op === 'disconnect') await gateway().call('model.disconnect', params as unknown as ParamsOf<'model.disconnect'>)
    else throw new Error(`no provider op ${String(op)}`)
    return afterProviders()
  })()),
  /* A read, so no reload after it: the fetched list is the sheet's own state
     and the page behind it has not changed. */
  fetchModels: (slug) => gateway().call('model.fetch_models', { slug }),
  addModels: (slug, models) => run(gateway().call('model.add_models', { slug, models }).then(afterProviders)),
  setFields: (slug, fields) => run(
    gateway().call('model.set_fields', { slug, fields: fields as ParamsOf<'model.set_fields'>['fields'] })
      .then(afterProviders),
  ),
  /* The browser tab is opened here, on the page: the code shown beside it is
     the same one the vendor's page asks for. */
  oauthLogin: (slug) => run(gateway().call('model.oauth_login', { slug }).then((r) => {
    openUrl(r.verification_uri)
    return r
  })),
  pickModel: (model, provider) => run(persistModel(model, provider, 'default').then(() => undefined)),
  model: () => defaultModel(),
  defaultProvider: () => defaultProvider(),
  archived: () => gateway().call('session.list', { archived: true }).then((r) => r.sessions || []),
  /* The rail lists by its own read, so it is that read that puts the row back. */
  restore: (id) => run(gateway().call('session.archive', { session_id: id, archived: false })
    .then(() => loadSessions())),
  removeSession: (id) => run(gateway().call('session.delete', { session_id: id }).then(() => undefined)),
  inspectSkill: (name) => run(gateway().call('skills.manage', { action: 'inspect', query: name })
    .then((r) => (r.info || {}) as SkillDetail)),
  openSkillFile: (name, file) => run(gateway().call('skills.manage', { action: 'open', query: name, file })
    .then(() => undefined)),
  uninstallSkill: (name) => run(gateway().call('skillhub.remove', { name }).then(afterExt)),
  serverAuthFields: (name) => gateway().call('plughub.detail', { id: name })
    .then((r) => {
      const entry = r.item as unknown as { contributes?: AuthContribution[] }
      const mcp = (entry.contributes || []).find((c) => c.kind === 'mcp')
      return ((mcp && mcp.auth && mcp.auth.fields) || []).map((f) => ({ ...f, label: hubText(f.label) }))
    })
    /* A server the catalogue does not carry is the ordinary case, not a
       failure to report: the panel says so itself when the list comes back
       empty. */
    .catch(() => []),
  toggleServer: (name, on) => run(gateway().call('plug.toggle', { name, enabled: on }).then(afterExt)),
  retryServer: (name) => run(gateway().call('plug.retry', { name }).then(afterExt)),
  revokeServer: (name) => run(gateway().call('plug.revoke', { name }).then(afterExt)),
  configureServer: (name, form) => run(gateway().call('plug.configure', { name, form }).then(afterExt)),
  authServer: (name) => run(gateway().call('plug.auth', { name }).then((r) => {
    const url = r.mcp && r.mcp.auth_url
    if (url) openUrl(url)
    return afterExt()
  })),
  version: () => chrome.version(),
  checkUpdate: (btn) => chrome.checkUpdate(btn),
  /* Not awaited: the pick repaints synchronously and the persist speaks for
     itself if it fails. */
  setLang: (v) => chrome.setLang(v),
}

/* -- the web tools' vendor tables ---------------------------------------
   The vendors the two web tools can run on, in the schema's order, with the
   default each falls back to. tests/test_agent_tools_web_providers.py parses
   this table from here and holds it to the schema literals. Exported so the
   Tools page and the onboarding wizard's webStepDone read the one copy. */
interface WebVendorPick {
  path: string
  vendors: string[]
  fallback: string
}
export const WEB_VENDOR: Record<string, WebVendorPick> = {
  web_search: {
    path: 'tools.web.search.provider',
    vendors: ['serper', 'anysearch', 'serpapi', 'tavily', 'exa', 'brave', 'firecrawl', 'serply'],
    fallback: 'serper',
  },
  web_fetch: {
    path: 'tools.web.fetch.provider',
    vendors: ['jina', 'anysearch', 'tavily', 'exa', 'firecrawl'],
    fallback: 'jina',
  },
}
export const WEB_VENDOR_LABEL: Record<string, string> = {
  serper: 'Serper',
  anysearch: 'AnySearch',
  serpapi: 'SerpApi',
  jina: 'Jina Reader',
  tavily: 'Tavily',
  exa: 'Exa',
  brave: 'Brave Search',
  firecrawl: 'Firecrawl',
  serply: 'Serply',
}
/* Where each vendor hands out keys. */
export const WEB_VENDOR_URL: Record<string, string> = {
  serper: 'https://serper.dev', anysearch: 'https://anysearch.com', serpapi: 'https://serpapi.com',
  jina: 'https://jina.ai/reader', tavily: 'https://tavily.com', exa: 'https://exa.ai',
  brave: 'https://brave.com/search/api', firecrawl: 'https://firecrawl.dev', serply: 'https://serply.io',
}
/* Jina reads without a key; every other reader needs one. */
export const FETCH_KEYLESS = new Set(['jina'])

const dig = (raw: Record<string, unknown>, path: string): unknown =>
  path.split('.').reduce<unknown>((o, k) => (o && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined), raw)
export const str = (raw: Record<string, unknown>, path: string): string => {
  const v = dig(raw, path)
  return typeof v === 'string' ? v : ''
}

/* The vendor a web tool runs on, from the config or the default. */
export const webVendor = (tool: string, raw: Record<string, unknown>): string => {
  const pick = WEB_VENDOR[tool]!
  return str(raw, pick.path) || pick.fallback
}
/* The slot the tools read a vendor's key from, and the pre-vendor leaf a key
   may still sit in: the tools read that too, so the row counts it as set and
   a clear retires both. */
export const vendorKey = (vendor: string): string => `tools.web.providers.${vendor}.apiKey`
export const legacyKey = (tool: string, vendor: string): string | null =>
  (tool === 'web_search' && vendor === 'serper') ? 'tools.web.search.apiKey'
    : (tool === 'web_fetch' && vendor === 'jina') ? 'tools.web.jinaApiKey' : null
export const keySet = (tool: string, vendor: string, raw: Record<string, unknown>): boolean => {
  const legacy = legacyKey(tool, vendor)
  return !!str(raw, vendorKey(vendor)) || (!!legacy && !!str(raw, legacy))
}

/* The onboarding wizard's two step-done predicates: whether its model step
   already has a connected provider and a chat model, and whether its web
   step already has a key on file for either web tool's vendor. */
export function modelStepDone(): boolean {
  return providers().some((p) => p.on) && !!defaultModel()
}

export function webStepDone(): boolean {
  return keySet('web_search', webVendor('web_search', RAW), RAW) || keySet('web_fetch', webVendor('web_fetch', RAW), RAW)
}

/* The manager's word on a server, pushed as it changes; the plugins page
   redraws its chips from it. Returns the unsubscribe. */
export const watchMcp = (onStatus: () => void): (() => void) => gateway().on('mcp.status', onStatus)

/* Test seam only: the raw config, the path it came from and the chrome the page
   registered are all the module's. */
export function _resetForTests(): void {
  RAW = {}
  configPathLive = '~/.raven/config.json'
  everosLive = null
  chrome = { version: () => null, checkUpdate: () => {}, setLang: () => {} }
}
