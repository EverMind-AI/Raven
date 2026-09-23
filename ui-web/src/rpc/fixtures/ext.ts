/* What this install has: the tools, the skills, the python plugins and the MCP
 * servers, as one `ext.list` answer.
 *
 * The offline canvas's inventory, which three pages read: the tool rows in the
 * settings dialog, the installed shelf in the skills island, and the plugin
 * rows beside the market. It was three fixture tables in the islands' own
 * shapes; it is one wire answer now, and the rows are built from it by the
 * same mappers the live page uses -- so a
 * tool's display name comes from the catalogue and its one-liner from this
 * `description`, which is why none of them are written here in Chinese any
 * more.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type ExtList = ResultOf<'ext.list'>
export type ExtSkill = ExtList['skills'][number]
export type ExtPlugin = ExtList['plugins'][number]
export type ExtTool = ExtList['tools'][number]
export type ExtMcp = ExtList['mcp'][number]

/* Raven's own actions: a fixed list, switched off through
   `tools.disabledTools` rather than removed. `needs` is a tool present but
   withheld for want of a key, which the row says out loud. */
const TOOLS: ExtTool[] = [
  { name: 'read_file', description: 'Read a file in the working directory', enabled: true , builtin: false },
  { name: 'write_file', description: 'Create a file or overwrite one', enabled: true , builtin: false },
  { name: 'edit_file', description: 'Change lines of an existing file', enabled: true , builtin: false },
  { name: 'list_dir', description: 'Look at the shape of a directory', enabled: true , builtin: false },
  { name: 'grep', description: 'Find text across code and documents', enabled: true , builtin: false },
  { name: 'exec', description: 'Run a command, run the tests, read the output', enabled: true , builtin: false },
  { name: 'spawn', description: 'Split a large job across several sub-agents', enabled: true , builtin: false },
  { name: 'web_fetch', description: 'Read a given address as prose', enabled: true , builtin: false },
  { name: 'deep_research', description: 'Many rounds of search and cross-checking; slow and expensive', enabled: false , builtin: false },
  { name: 'ask_user', description: 'Stop and ask rather than guess', enabled: true , builtin: false },
  /* The meta-tools, the only two whose switch the loop would not honour: this
     page writes `tools.disabledTools`, and neither is registered from it.
     `tool_search` is drawn with the fold off, which is the default -- absent
     from the list, the page showed a meta card with one row in it. */
  { name: 'tool_search', description: 'Find the tool for a job among the ones installed', enabled: false, builtin: true },
  { name: 'tool_call', description: 'Call a tool found that way', enabled: true, builtin: true },
  /* Schema-hidden and switchable, which is the pair the offline page exists to
     show apart: the model reaches these by name through `tool_call`, and an
     entry in `tools.disabledTools` still takes them away. */
  { name: 'run_subagent_dag', description: 'Run a graph of sub-agent steps', enabled: true, builtin: false },
  { name: 'cancel_dag', description: 'Stop a running graph', enabled: true, builtin: false },
  { name: 'dag_status', description: 'Where a running graph has got to', enabled: true, builtin: false },
  { name: 'resolve_dag_node', description: 'Answer one node of a running graph', enabled: true, builtin: false },
]

/* The skills on disk. Their names are the hub's, because that is where these
   two came from -- see ./skillhub.ts, whose rows they are matched against. */
const SKILLS: ExtSkill[] = [
  { name: '代码审查清单', description: '按你团队的规矩审代码，而不是通用建议',
    source: 'skillhub', always: false, hub: true, hub_id: 'sh-code-review' },
  { name: 'SQL 规范', description: '命名、缩进、禁用写法按你们的库来',
    source: 'skillhub', always: false, hub: true, hub_id: 'sh-sql-style' },
]

const PLUGINS: ExtPlugin[] = [
  { id: 'sheets', display_name: 'Sheets', version: '0.9.0', enabled: true, bundled: false },
  { id: 'pdf', display_name: 'PDF', version: '0.1.4', enabled: false, bundled: false },
  { id: 'raven-media', display_name: 'Media', version: '1.0.0', enabled: true, bundled: true },
]

/* One server per state the rows have to be able to draw: connected, waiting
   for an authorization, failed, and switched off. The rail's attention badge
   counts the middle two, which is why both are here. */
const MCP: ExtMcp[] = [
  { name: 'websearch', transport: 'http', state: 'auth_required', connected: false, tool_count: 2, enabled: true,
    auth: 'oauth', credentialed: false },
  { name: 'github', transport: 'http', state: 'error', connected: false, tool_count: 3, enabled: true,
    error: 'handshake failed: token expired (HTTP 401)', auth: 'apikey', credentialed: true },
  { name: 'notion', transport: 'http', state: 'disconnected', connected: false, tool_count: 3, enabled: false,
    auth: 'oauth', credentialed: true },
  { name: 'sqlite', transport: 'stdio', state: 'connected', connected: true, tool_count: 2, enabled: true,
    auth: 'none', credentialed: true },
]

/** The inventory, shared with the two hubs and with the settings answer. */
export interface ExtFixture {
  fixtures: Fixtures
  skills: ExtSkill[]
  plugins: ExtPlugin[]
  mcp: ExtMcp[]
  /** Which tools the config switches off, which `settings.get` carries. */
  disabledTools: string[]
  /** Which python plugins the config switches off, same. */
  disabledPlugins: string[]
  /** Whether web search is configured, which one scripted conversation forks on. */
  websearchOn(): boolean
}

export function createExt(_env: FixtureEnv): ExtFixture {
  const skills = SKILLS.map((s) => ({ ...s }))
  const plugins = PLUGINS.map((p) => ({ ...p }))
  const mcp = MCP.map((m) => ({ ...m }))
  const state: ExtFixture = {
    skills,
    plugins,
    mcp,
    disabledTools: ['deep_research'],
    disabledPlugins: ['pdf'],
    websearchOn: () => {
      const server = mcp.find((m) => m.name === 'websearch')
      return !!server && server.enabled && server.state === 'connected'
    },
    fixtures: {
      'ext.list': () => ({
        tools: TOOLS.map((t) => ({ ...t })),
        skills: skills.map((s) => ({ ...s })),
        plugins: plugins.map((p) => ({ ...p })),
        mcp: mcp.map((m) => ({ ...m })),
      }),
    },
  }
  return state
}
