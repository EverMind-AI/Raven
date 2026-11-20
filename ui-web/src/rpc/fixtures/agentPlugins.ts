/* The Agent Plugin packages the offline settings page lists.
 *
 * Two packages behind `agent_plugins.list`: one imported, with one MCP server
 * running and one that failed to start, and one just found.
 * `agent_plugins.set_mcp` flips a package's `allowed` and leaves `live`
 * alone, the way a live engine keeps the server set it started with until
 * the next start.
 */
import type { Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type PackageRow = ResultOf<'agent_plugins.list'>['packages'][number]

const PACKAGES: PackageRow[] = [
  {
    name: 'docs-kit',
    version: '1.0.0',
    description: 'Helpers for writing and checking project documentation.',
    source: '~/agent-plugins/docs-kit',
    notes: [],
    skills: ['write-docs', 'check-links'],
    skills_imported: true,
    problems: [],
    mcp: {
      servers: [
        { name: 'docs-kit__search', state: 'connected', error: null },
        { name: 'docs-kit__lint', state: 'error', error: 'npx exited with status 1' },
      ],
      allowed: true,
      live: true,
      problems: [],
    },
  },
  {
    name: 'sql-tools',
    version: '0.3.0',
    description: '',
    source: '~/agent-plugins/sql-tools',
    notes: [],
    skills: ['sql-review'],
    skills_imported: false,
    problems: [],
    mcp: {
      servers: [{ name: 'sql-tools__sqlite', state: 'off', error: null }],
      allowed: false,
      live: false,
      problems: [],
    },
  },
]

export interface AgentPluginsFixture {
  fixtures: Fixtures
}

export function createAgentPlugins(): AgentPluginsFixture {
  const packages = PACKAGES.map((p) => ({ ...p, mcp: { ...p.mcp } }))
  return {
    fixtures: {
      'agent_plugins.list': () => ({ packages: packages.map((p) => ({ ...p, mcp: { ...p.mcp } })), problems: [] }),
      'agent_plugins.set_mcp': (p) => {
        const row = packages.find((x) => x.name === p.name)
        if (!row) throw new Error(`no Agent Plugin package named '${p.name}'`)
        const changed = row.mcp.allowed !== p.enabled
        row.mcp = { ...row.mcp, allowed: p.enabled }
        return { name: p.name, enabled: p.enabled, changed }
      },
    },
  }
}
