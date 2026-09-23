/* The plugin market, as much of it as the offline canvas carries.
 *
 * Four entries behind `plughub.search` and `plughub.detail`, plus the four
 * writes the card offers. An install grows `ext.list`'s MCP array, so the
 * installed shelf and the rail's attention badge both answer the change.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { ExtFixture } from './ext'

/* One market entry, in the shape the hub serves and the card reads. `kind` and
   the connection block inside `contributes` are what the card draws the
   permissions and the tool preview from. */
interface Entry {
  id: string
  name: string
  publisher: string
  verified: boolean
  summary: string
  category: string
  tool_preview_count: number
  skill_count?: number
  risk_tier: number
  installed: boolean
  version: string
  homepage: string
  description: string
  contributes: Array<Record<string, unknown>>
}

const CATS = ['developer', 'productivity', 'data']

const MARKET: Entry[] = [
  { id: 'github', name: 'GitHub', publisher: 'github.com', verified: true,
    summary: '读 issue 与 PR，提交评论', category: 'developer',
    tool_preview_count: 3, risk_tier: 1, installed: false,
    version: '1.4.0', homepage: 'https://github.com/mcp',
    description: '连上 GitHub 的官方 MCP 服务，读写 issue、PR 与评论。',
    contributes: [{ kind: 'mcp',
      connection: { type: 'http', url: 'https://api.githubcopilot.com/mcp' },
      auth: { mode: 'apikey', fields: [{ key: 'token', label: 'Token', secret: true, help_url: 'https://github.com/settings/tokens' }] },
      tools_preview: ['list_issues', 'get_pr', 'create_comment'] }] },
  { id: 'websearch', name: '网页搜索', publisher: 'serper.dev', verified: true,
    summary: '让 Raven 查得到网上的实时信息', category: 'data',
    tool_preview_count: 2, risk_tier: 1, installed: false,
    version: '0.9.2', homepage: 'https://serper.dev',
    description: '接入 serper.dev 的搜索接口，Raven 可以自己找资料。',
    contributes: [{ kind: 'mcp',
      connection: { type: 'http', url: 'https://mcp.serper.dev' },
      auth: { mode: 'oauth' },
      tools_preview: ['web_search', 'web_news'] }] },
  { id: 'sqlite', name: 'SQLite', publisher: 'raven-tools', verified: false,
    summary: '在本机查询与修改 SQLite 数据库', category: 'data',
    tool_preview_count: 2, skill_count: 1, risk_tier: 2, installed: false,
    version: '0.3.1', homepage: '',
    description: '本地运行的 stdio 服务，直接读写你机器上的数据库文件。',
    contributes: [
      { kind: 'mcp', connection: { command: 'npx', args: ['-y', '@raven/sqlite-mcp'] },
        auth: { mode: 'none' }, tools_preview: ['query', 'execute'] },
      { kind: 'skill', name: 'sql-review', skillhub_id: 'sql-review' },
    ] },
  { id: 'notion', name: 'Notion', publisher: 'notion.so', verified: true,
    summary: '把结果写进你的 Notion 库', category: 'productivity',
    tool_preview_count: 3, risk_tier: 1, installed: false,
    version: '2.1.0', homepage: 'https://notion.so/mcp',
    description: '开启后 Raven 可以直接建页面，建议先确认目标库。',
    contributes: [{ kind: 'mcp',
      connection: { type: 'http', url: 'https://mcp.notion.com' },
      auth: { mode: 'oauth' },
      tools_preview: ['search_pages', 'create_page', 'update_page'] }] },
];

export interface PlughubFixture {
  fixtures: Fixtures
}

export function createPlughub(_env: FixtureEnv, ext: ExtFixture): PlughubFixture {
  const market = MARKET.map((m) => ({ ...m }))
  /* The card-sized projection of an entry, which is a different shape from the
     entry itself: which contribution kinds it carries and how it authorizes are
     derived from `contributes` rather than written out twice. */
  const card = (it: Entry): ResultOf<'plughub.search'>['items'][number] => {
    const kinds = [...new Set(it.contributes.map((c) => String(c.kind)))]
    const mcp = it.contributes.find((c) => c.kind === 'mcp') as
      { auth?: { mode?: string }; connection?: { type?: string } } | undefined
    return {
      id: it.id, name: it.name, summary: it.summary, category: it.category,
      verified: it.verified, publisher: it.publisher, risk_tier: it.risk_tier,
      auth_mode: ((mcp && mcp.auth && mcp.auth.mode) || 'none') as 'none' | 'apikey' | 'oauth',
      tool_preview_count: it.tool_preview_count, skill_count: it.skill_count || 0,
      kinds, installed: !!serverOf(it.id), version: it.version,
      ...(mcp && mcp.connection && mcp.connection.type ? { transport: mcp.connection.type } : {}),
    }
  }
  const find = (id: string): Entry | undefined => market.find((x) => x.id === id)
  const entryOf = (it: Entry): ResultOf<'plughub.detail'>['item'] => ({
    id: it.id, name: it.name, version: it.version, summary: it.summary,
    description: it.description, homepage: it.homepage,
    publisher: { name: it.publisher, verified: it.verified },
    contributes: it.contributes,
  })
  const serverOf = (name: string): ExtFixture['mcp'][number] | undefined =>
    ext.mcp.find((m) => m.name === name)

  return {
    fixtures: {
      'plughub.search': (p) => {
        const params = p as { q?: string; category?: string }
        const q = (params.q || '').toLowerCase()
        return {
          items: market.filter((x) => (!params.category || x.category === params.category)
            && (!q || (x.name + (x.summary || '')).toLowerCase().includes(q)))
            .map(card),
          categories: CATS,
        }
      },
      'plughub.detail': (p) => {
        const it = find(p.id)
        if (!it) throw new Error(`no plugin ${p.id}`)
        return { item: entryOf(it), installed: !!serverOf(it.id) }
      },
      'plug.install': (p) => {
        const it = find(p.id)
        if (!it) throw new Error(`no plugin ${p.id}`)
        const preview = (it.contributes[0] || {}) as { tools_preview?: string[] }
        const server: ExtFixture['mcp'][number] = {
          name: it.id, transport: 'http', state: 'connected', connected: true,
          tool_count: (preview.tools_preview || []).length, enabled: true,
          /* A fresh install from this canvas holds whatever its form asked for. */
          auth: 'none', credentialed: true,
        }
        if (!serverOf(it.id)) ext.mcp.push(server)
        return {
          installed: true, pending: false, mcp: server,
          ledger: { catalog_id: it.id, pieces: [{ kind: 'mcp', server: it.id }] },
        }
      },
      'plug.remove': (p) => {
        const at = ext.mcp.findIndex((m) => m.name === p.name)
        if (at >= 0) ext.mcp.splice(at, 1)
        return { removed: true, origin: 'market' as const }
      },
      'plug.toggle': (p) => {
        const server = serverOf(p.name)
        if (server) server.enabled = !!p.enabled
        return { name: p.name, enabled: !!p.enabled, ...(server ? { mcp: server } : {}) }
      },
      /* Nothing to authorize against: the card keeps the state it had, which
         is what an authorization that cannot be started looks like. */
      'plug.auth': (p) => ({ name: p.name }),
      'plug.retry': (p) => {
        const server = serverOf(p.name)
        if (server) { server.state = 'connected'; server.connected = true; delete server.error }
        return { name: p.name, ...(server ? { mcp: server } : {}) }
      },
      'plug.revoke': (p) => {
        const server = serverOf(p.name)
        if (server) { server.credentialed = false; server.state = 'auth_required'; server.connected = false }
        return { name: p.name, ...(server ? { mcp: server } : {}) }
      },
      /* An empty value retires the credential; anything else is one. */
      'plug.configure': (p) => {
        const server = serverOf(p.name)
        const set = Object.values(p.form || {}).some((v) => !!v)
        if (server) { server.credentialed = set; server.state = set ? 'connected' : 'disconnected'; server.connected = set }
        return { name: p.name, ...(server ? { mcp: server } : {}) }
      },
    },
  }
}
