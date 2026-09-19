/* The installed surfaces: one read, three pages.
 *
 * `ext.list` answers the tools, the skills, the python plugins and the MCP
 * servers together, and `settings.get` carries which of them are switched off,
 * so the two are fetched as a pair here and the rows built once -- the tools for
 * the settings dialog, the skills for the skills island, the plugins and MCP
 * servers for the plugins island. Three domains read this one; none of them owns
 * it, which is why it is a domain of its own rather than a file inside whichever
 * page happened to need it first.
 *
 * Row mutations go back through the same place they are read from: `on` and
 * `state` are accessors that persist through settings.set, which is what lets a
 * page toggle a row without knowing there is a config file.
 */

import { t } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../../state/toast'
import { toggleMcp } from '../plugins/store'

import type { ResultOf } from '../../rpc/generated'
import type { CapabilitiesSource } from '../../state/sources'
import type { InstalledRow } from '../plugins/types'
import type { ToolRow } from '../settings/types'
import type { InstalledSkill } from '../skills/types'

// Display names come from the catalogue; an unknown tool keeps its raw id.
const toolLabel = (n: string): string => t('tool.' + n, undefined, n)
const TOOL_GROUP_OF = (n: string): string => /file|dir|grep|glob|sheet|pdf/.test(n) ? 'file'
  : /web|fetch|search|research|browser/.test(n) ? 'net'
  : /ask|message|clarify/.test(n) ? 'ask' : 'run'
const TOOL_DANGER = new Set(['write_file', 'edit_file', 'exec'])

type ExtList = ResultOf<'ext.list'>
/** The four row shapes one `ext.list` read answers with. */
export type ExtToolRow = ExtList['tools'][number]
export type ExtSkillRow = ExtList['skills'][number]
export type ExtPluginRow = ExtList['plugins'][number]
export type ExtMcpRow = ExtList['mcp'][number]

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
    needs: tool.needs || null, builtin: !!tool.builtin } as ToolRow
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
    set: (v) => toggleMcp(m.name, v === 'on'),
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
  skillRowsLive = ext.skills
  mcpRowsLive = ext.mcp
  extLoaded = true
}

/** The rows the one boot-time read filled, each read at call time. */
export const extTools = (): ToolRow[] => toolsLive
/* The wire rows themselves, for the settings pages: the skill and MCP rows
   above drop the fields those pages switch on (`always`, `auth`,
   `credentialed`). */
let skillRowsLive: ResultOf<'ext.list'>['skills'] = []
let mcpRowsLive: ResultOf<'ext.list'>['mcp'] = []
export const extSkillRows = (): ResultOf<'ext.list'>['skills'] => skillRowsLive
export const extMcpRows = (): ResultOf<'ext.list'>['mcp'] => mcpRowsLive
export const extSkills = (): InstalledSkill[] => skillsLive
export const extPlugins = (): InstalledRow[] => pluginsLive
export const extIsLoaded = (): boolean => extLoaded
/* Whether that read has happened, and making it happen. The capabilities page
   is page chrome (chrome/CapsPage.tsx, state/caps.ts), so this one has no
   domain of its own. */
export const capabilitiesSource: CapabilitiesSource = {
  loaded: () => extLoaded,
  load: async () => { await loadExt(); return true },
}
/* Test seam only: the one ext.list read that fills three pages is cached here,
   so a case that primed it must not answer the next one from that cache. */
export function _resetForTests(): void {
  disabledToolsLive = []
  pluginsDisabledLive = []
  toolsLive = []
  skillsLive = []
  pluginsLive = []
  extLoaded = false
}
