/* The installed surfaces: one read, one page.
 *
 * `ext.list` answers the tools, the skills, the python plugins and the MCP
 * servers together, and `settings.get` carries which of them are switched off,
 * so the two are fetched as a pair here. Only the tools are built into rows --
 * the skills island and the plugins island are gone, and building a row was
 * their need. The settings dialog's skill and plugin cards read the other two
 * answers as they came off the wire, which is the shape they want anyway.
 *
 * Still a domain of its own rather than a file inside the settings dialog: the
 * read happens at boot (src/app/boot.ts) and the dialog is opened long after.
 *
 * Row mutations go back through the same place they are read from: `on` and
 * `state` are accessors that persist through settings.set, which is what lets a
 * page toggle a row without knowing there is a config file.
 */

import { t } from '../../i18n/t'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../../state/toast'

import type { ResultOf } from '../../rpc/generated'
import type { CapabilitiesSource } from '../../state/sources'
import type { ToolRow } from '../settings/types'

// Display names come from the catalogue; an unknown tool keeps its raw id.
const toolLabel = (n: string): string => t('tool.' + n, undefined, n)
const TOOL_GROUP_OF = (n: string): string => /file|dir|grep|glob|sheet|pdf/.test(n) ? 'file'
  : /web|fetch|search|research|browser/.test(n) ? 'net'
  : /ask|message|clarify/.test(n) ? 'ask' : 'run'
const TOOL_DANGER = new Set(['write_file', 'edit_file', 'exec'])

type ExtList = ResultOf<'ext.list'>
/** The row shape this builds from one `ext.list` read. */
export type ExtToolRow = ExtList['tools'][number]

let disabledToolsLive: string[] = []
let toolsLive: ToolRow[] = []
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
  /* A tool no switch on this page can move keeps the state the loop reported.
     The accessor below answers `tools.disabledTools`, which is the right
     question for a switchable tool and a meaningless one here: `tool_search`
     is reported precisely because the loop did not register it, and it is not
     in that list either, so the accessor called it on. */
  if (tool.builtin) {
    o.on = !!tool.enabled
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

export async function loadExt(): Promise<void> {
  const [ext, cfg] = await Promise.all([gateway().call('ext.list', {}), gateway().call('settings.get', {})])
  const raw = (cfg.settings || {}) as { tools?: { disabledTools?: string[] } }
  disabledToolsLive = (raw.tools && raw.tools.disabledTools) || []
  toolsLive = ext.tools.filter((tool) => !tool.mcp_server).map(mkToolRow)
  skillRowsLive = ext.skills
  mcpRowsLive = ext.mcp
  extLoaded = true
}

/** The rows the one boot-time read filled, each read at call time. */
export const extTools = (): ToolRow[] => toolsLive
/* The wire rows themselves, for the settings dialog: the tool rows above are
   built shapes, and the dialog's skill and plugin cards switch on fields
   (`always`, `auth`, `credentialed`) a built row would have dropped. */
let skillRowsLive: ResultOf<'ext.list'>['skills'] = []
let mcpRowsLive: ResultOf<'ext.list'>['mcp'] = []
export const extSkillRows = (): ResultOf<'ext.list'>['skills'] => skillRowsLive
export const extMcpRows = (): ResultOf<'ext.list'>['mcp'] => mcpRowsLive
export const extIsLoaded = (): boolean => extLoaded
/** Whether that read has happened, and making it happen. */
export const capabilitiesSource: CapabilitiesSource = {
  loaded: () => extLoaded,
  load: async () => { await loadExt(); return true },
}
/* Test seam only: the one ext.list read is cached here, so a case that primed
   it must not answer the next one from that cache. */
export function _resetForTests(): void {
  disabledToolsLive = []
  toolsLive = []
  skillRowsLive = []
  mcpRowsLive = []
  extLoaded = false
}
