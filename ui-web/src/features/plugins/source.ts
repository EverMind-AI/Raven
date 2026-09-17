/* -- the installed surfaces: the rpc source ---------------------------
   One read fills three pages. `ext.list` answers the tools, the skills, the
   python plugins and the MCP servers together, and `settings.get` carries
   which of them are switched off, so the two are fetched as a pair and the
   rows built here -- the tools for the settings dialog, the skills for the
   skills island, the plugins and MCP servers for the plugins island.

   Row mutations go back through the same place they are read from: `on` and
   `state` are accessors that persist through settings.set, which is what lets
   a page toggle a row without knowing there is a config file.

   The plugins island's own market calls (plughub.* / plug.*) are here too;
   the renderers are ui-web/src/features/plugins/ and features/skills/. */

import type { InstalledRow, DetailEntry, PluginsSource } from './types'
import type { CapabilitiesSource } from '../../state/sources'
import type { ResultOf } from '../../rpc/generated'
import type { ToolRow } from '../settings/types'
import type { InstalledSkill, SkillsSource } from '../skills/types'

import { islands } from '../../islands'
import { code as LANG } from '../../i18n/t'
import { t } from '../../i18n/t'
import { show as toast } from '../../shell/toast'
import { gateway } from '../../state/gateway'

type ExtList = ResultOf<'ext.list'>
/** The four row shapes one `ext.list` read answers with. */
export type ExtToolRow = ExtList['tools'][number]
export type ExtSkillRow = ExtList['skills'][number]
export type ExtPluginRow = ExtList['plugins'][number]
export type ExtMcpRow = ExtList['mcp'][number]

// Display names come from the catalogue; an unknown tool keeps its raw id.
const toolLabel = (n: string): string => t('tool.' + n, undefined, n)
const TOOL_GROUP_OF = (n: string): string => /file|dir|grep|glob|sheet|pdf/.test(n) ? 'file'
  : /web|fetch|search|research|browser/.test(n) ? 'net'
  : /ask|message|clarify/.test(n) ? 'ask' : 'run'
const TOOL_DANGER = new Set(['write_file', 'edit_file', 'exec'])

let disabledToolsLive: string[] = []
let pluginsDisabledLive: string[] = []
let toolsLive: ToolRow[] = []
let skillsLive: InstalledSkill[] = []
let pluginsLive: InstalledRow[] = []
let extLoaded = false

/* The engine re-reads tools.disabledTools once per assembled tool array, so the
   toggle lands on the next turn. Not the same promise as the plugin toggle below:
   plugins.disabled is still read at startup, and its toast still says so. */
function persistDisabledTools(): void {
  gateway().call('settings.set', { key: 'tools.disabledTools', value: disabledToolsLive })
    .then(() => toast(t('gui.op.saved_next_turn')))
    .catch((e) => toast(t('gui.op.save_failed', { detail: e.message || e })))
}

export function mkToolRow(tool: ExtToolRow): ToolRow {
  const id = tool.name
  const o = { id, name: toolLabel(id), group: TOOL_GROUP_OF(id),
    reach: TOOL_GROUP_OF(id) === 'net' ? 'net' : 'local',
    danger: TOOL_DANGER.has(id), one: tool.description || '',
    /* Present but withheld for want of a key. The row exists so the reader
       learns the tool exists and what it wants -- before this, a key-gated
       tool was simply absent, which reads as removed. */
    needs: tool.needs || null } as ToolRow
  if (tool.needs) {
    o.on = false
    return o
  }
  Object.defineProperty(o, 'on', {
    get: () => !disabledToolsLive.includes(id),
    set: (v) => {
      disabledToolsLive = v ? disabledToolsLive.filter((x) => x !== id) : [...new Set([...disabledToolsLive, id])]
      persistDisabledTools()
    },
  })
  return o
}

export function mkSkillRow(s: ExtSkillRow): InstalledSkill {
  const o = { id: s.name, name: s.name, reach: 'local', glyph: (s.name[0] || 'S').toUpperCase(),
    ver: '—', src: s.source, one: s.description || '', cat: s.source, hub: !!s.hub, hubId: s.hub_id || '' }
  Object.defineProperty(o, 'state', {
    get: () => 'on',
    set: () => toast(t('gui.hub.auto_use')),
  })
  return o
}

export function mkPluginRow(p: ExtPluginRow): InstalledRow {
  const id = p.id
  const o = { id, name: p.display_name || id, reach: 'local', glyph: '◧', ver: p.version,
    src: t(p.bundled ? 'gui.ext.builtin_plugin' : 'gui.ext.plugin'), one: '',
    cat: t('gui.ext.plugin'), tools: [], perms: [] }
  Object.defineProperty(o, 'state', {
    get: () => (pluginsDisabledLive.includes(id) ? 'off' : p.enabled ? 'on' : 'off'),
    set: (v) => {
      pluginsDisabledLive = v === 'on'
        ? pluginsDisabledLive.filter((x) => x !== id)
        : [...new Set([...pluginsDisabledLive, id])]
      gateway().call('settings.set', { key: 'plugins.disabled', value: pluginsDisabledLive })
        .then(() => toast(t('gui.op.saved_restart')))
        .catch((e) => toast(t('gui.op.save_failed', { detail: e.message || e })))
    },
  })
  return o
}

/* Legacy-state mapping keeps the capability badge's need/fail vocabulary;
   the plugin page itself renders from the raw snapshot in o.m. */
const MCP_LEGACY: Record<string, string> = {
  connected: 'on', connecting: 'on', auth_required: 'need', error: 'fail', disconnected: 'off',
}

export function mkMcpRow(m: ExtMcpRow): InstalledRow {
  const o = { id: `mcp:${m.name}`, name: m.name, reach: m.transport === 'stdio' ? 'local' : 'net',
    glyph: '◇', ver: '—', src: `${m.transport} · MCP`, cat: 'MCP', m,
    one: m.connected ? t('gui.ext.mcp_tools', { n: m.tool_count }) : '', tools: [], perms: [] }
  Object.defineProperty(o, 'state', {
    get: () => (!m.enabled ? 'off' : MCP_LEGACY[m.state] || 'off'),
    set: (v) => islands.plugins.toggleMcp(m.name, v === 'on'),
  })
  return o
}

export async function loadExt(): Promise<void> {
  const [ext, cfg] = await Promise.all([gateway().call('ext.list', {}), gateway().call('settings.get', {})])
  const raw = (cfg.settings || {}) as { tools?: { disabledTools?: string[] }; plugins?: { disabled?: string[] } }
  disabledToolsLive = (raw.tools && raw.tools.disabledTools) || []
  pluginsDisabledLive = (raw.plugins && raw.plugins.disabled) || []
  toolsLive = ext.tools.filter((tool) => !tool.mcp_server).map(mkToolRow)
  skillsLive = ext.skills.map(mkSkillRow)
  pluginsLive = ext.plugins.map(mkPluginRow).concat(ext.mcp.map(mkMcpRow))
  extLoaded = true
}

/** The rows the one boot-time read filled, each read at call time. */
export const extTools = (): ToolRow[] => toolsLive
export const extSkills = (): InstalledSkill[] => skillsLive
export const extPlugins = (): InstalledRow[] => pluginsLive
export const extIsLoaded = (): boolean => extLoaded

const pmText = (v: unknown): string =>
  (v && typeof v === 'object' ? (v as Record<string, string>)[LANG] || (v as Record<string, string>).en || '' : String(v || ''))

export const pmErrText = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* The hub serves i18n objects for its text fields; which language wins is
   decided here, once, so the island only ever sees plain strings. */
export function pmNormEntry(entry: DetailEntry): DetailEntry {
  return {
    ...entry,
    name: pmText(entry.name),
    summary: pmText(entry.summary),
    description: pmText(entry.description),
    contributes: (entry.contributes || []).map((c) => {
      if (c.kind !== 'mcp' || !c.auth || !c.auth.fields) return c
      return { ...c, auth: { ...c.auth, fields: c.auth.fields.map((f) => ({ ...f, label: pmText(f.label) })) } }
    }),
  }
}

export const pluginsSource: PluginsSource = {
  // A search failure is rendered in the page (market_down + retry), not
  // toasted -- the island owns that surface.
  search: (q, category) => gateway().call('plughub.search', { q, category })
    .then((r) => ({ items: r.items || [], categories: r.categories || [] }))
    .catch((e) => { throw new Error(pmErrText(e)) }),
  detail: (id) => gateway().call('plughub.detail', { id })
    /* `plughub.detail` declares its entry as a free JSON object, so the shape
       the card reads is the island's own; the normaliser is what makes the two
       agree, and nothing but the three text fields is touched. */
    .then((r) => ({ entry: pmNormEntry(r.item as unknown as DetailEntry), installed: !!r.installed }))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      throw { handled: true }
    }),
  // Failure is NOT toasted here: whether it lands in the progress sheet or
  // a toast depends on whether the sheet is showing, which only the island
  // knows. The error detail travels normalized.
  install: (id, form) => gateway().call('plug.install', { id, form: form || {} })
    .catch((e) => { throw new Error(pmErrText(e)) }),
  // Same shape: the user-facing uninstall toasts from the island, and the
  // silent auth rollback swallows the failure -- one call serves both.
  remove: (name) => gateway().call('plug.remove', { name })
    .then(() => loadExt())
    .catch((e) => { throw new Error(pmErrText(e)) }),
  toggle: (name, enabled) => gateway().call('plug.toggle', { name, enabled })
    .then((r) => r.mcp || null)
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      return loadExt().catch(() => {}).then(() => { throw { handled: true } })
    }),
  // The row is the loader's own object: assigning state runs its setter,
  // which persists plugins.disabled through settings.set.
  togglePy: async (row, on) => { row.state = on ? 'on' : 'off' },
  auth: (name) => gateway().call('plug.auth', { name })
    .then((r) => r.mcp || null)
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: pmErrText(e) }))
      throw { handled: true }
    }),
  /* The two names the contract does not declare, so they cannot be typed
     calls: a resident gateway answers both with -32601 and the card shows
     that refusal, which is the behaviour to keep until the manual-add path
     has a declared method (rpc-schema/openrpc.json has no raven.mcp.*). */
  manual: async (name, address) => {
    const listed = await gateway().callUnchecked('raven.mcp.list', {}) as { servers?: unknown[] }
    await gateway().callUnchecked('raven.mcp.set', {
      servers: [...(listed.servers || []), { name, address }],
    })
    await loadExt()
  },
  rows: () => pluginsLive,
  /* See skillsSource.loaded: the same one boot-time read feeds both. */
  loaded: () => extLoaded,
  reload: () => loadExt(),
}

const skillhubErr = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* -- skills: the hub half ---------------------------------------------
   skillhub.* is the hub's /skills/search -- 93k skills, paged, filterable by
   category. Search rejections travel back raw: the island renders them in
   place with a retry. install/remove toast their own failures here and reject
   `{handled: true}`, the contract the island's catch reads. Both refresh the
   live rows through loadExt so every installed surface answers the change. */
export const skillsSource: SkillsSource = {
  search: (p) => gateway().call('skillhub.search', {
    query: p.query, category: p.category, page: p.page, limit: p.limit,
  }),
  detail: (id) => gateway().call('skillhub.detail', { id }),
  install: (id) => gateway().call('skillhub.install', { id })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: skillhubErr(e) }))
      throw { handled: true }
    }),
  remove: (name) => gateway().call('skillhub.remove', { name })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(t('gui.plug.op_failed', { err: skillhubErr(e) }))
      throw { handled: true }
    }),
  installed: () => skillsLive,
  /* Whether the one boot-time read actually landed. Without it an empty
     list means both "nothing is installed" and "the read never happened",
     and the page has to draw the same nothing for a working install and a
     broken socket. */
  loaded: () => extLoaded,
}

/* Whether that read has happened, and making it happen. The capabilities page
   is still legacy chrome, so this one has no island of its own. */
export const capabilitiesSource: CapabilitiesSource = {
  loaded: () => extLoaded,
  load: async () => { await loadExt(); return true },
}
