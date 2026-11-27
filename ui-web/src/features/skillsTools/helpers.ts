/* Pure view helpers for Skills & Tools: tool grouping, blocker detection,
 * MCP/plugin chip states, SKILL.md frontmatter stripping, and icon-tile tones.
 */
import { ROLES, TOOL_ROLE_OF, disabledTools, roleValue } from '../../lib/modelRoles'
import { webKeyReady } from './source'
import GROUPS from './toolGroups.json'

import type { Role } from '../../lib/modelRoles'
import type { AgentPluginList, AgentPluginRow, McpSnapshot, SettingsSnapshot, ToolRow } from './types'

export const TOOL_GROUPS = GROUPS as Record<string, string[]>

export const CONFIGURABLE_TOOLS = [
  'web_search',
  'web_fetch',
  'image_generate',
  'text_to_speech',
  'video_generate',
  'understand_media',
] as const

export const isBuiltin = (row: ToolRow | undefined): boolean => !!(row && row.builtin)

export const hasPanel = (id: string): boolean => (CONFIGURABLE_TOOLS as readonly string[]).includes(id)

export function blocker(id: string, raw: Record<string, unknown>, snap: SettingsSnapshot): '' | 'key' | 'model' {
  if (id === 'web_search' || id === 'web_fetch') {
    return webKeyReady(id, raw) ? '' : 'key'
  }
  const roleId = TOOL_ROLE_OF[id]
  if (roleId) {
    const role = ROLES.find((r) => r.id === roleId) as Role
    return roleValue(role, snap) ? '' : 'model'
  }
  return ''
}

export type McpChip = 'none' | 'connected' | 'connecting' | 'setup' | 'failed'

export function mcpChip(m: McpSnapshot, busy: boolean): McpChip {
  if (!m.enabled) return 'none'
  if (busy || m.state === 'connecting') return 'connecting'
  if (m.auth && m.auth !== 'none' && !m.credentialed) return 'setup'
  if (m.state === 'connected') return 'connected'
  if (m.state === 'error' || m.state === 'auth_required') return 'failed'
  return 'none'
}

export type PkgChip = 'none' | 'restart' | 'problem' | 'unimported'

export function pkgChip(p: AgentPluginRow): PkgChip {
  if (p.mcp.allowed !== p.mcp.live) return 'restart'
  if (p.problems.length || p.mcp.problems.length) return 'problem'
  if (p.skills.length && !p.skills_imported) return 'unimported'
  return 'none'
}

export function stripFrontmatter(body: string): string {
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(body)
  return m ? body.slice(m[0].length) : body
}

export const importCommand = (source: string): string =>
  `raven agent-plugins import ${/\s/.test(source) ? `"${source}"` : source}`

export type TileTone = 'blue' | 'amber' | 'moss' | 'clay' | 'plum' | 'teal' | 'slate'

const PALETTE: readonly TileTone[] = ['blue', 'amber', 'moss', 'plum', 'teal', 'clay']

function hashTone(seed: string): TileTone {
  let h = 0
  for (let i = 0; i < seed.length; i += 1) h = (h * 31 + seed.charCodeAt(i)) >>> 0
  return PALETTE[h % PALETTE.length]!
}

const TOOL_TONE: Record<string, TileTone> = {
  web_search: 'blue',
  web_fetch: 'teal',
  image_generate: 'plum',
  text_to_speech: 'amber',
  video_generate: 'clay',
  understand_media: 'moss',
}

export const toolTone = (id: string): TileTone => TOOL_TONE[id] ?? 'slate'

export const skillTone = (name: string): TileTone => hashTone(name)

export const mcpTone = (name: string): TileTone => hashTone(`mcp:${name}`)

export const pluginTone = (name: string): TileTone => hashTone(`pkg:${name}`)

export function tabAttention(
  snap: SettingsSnapshot,
  pkgs: AgentPluginList | null,
): { tools: boolean; mcp: boolean; plugins: boolean } {
  const disabled = disabledTools(snap.raw)
  const known = new Map(snap.tools.map((row) => [row.id, row]))
  const tools = CONFIGURABLE_TOOLS.some((id) => {
    const r = known.get(id)
    return !!r && !isBuiltin(r) && !disabled.includes(id) && blocker(id, snap.raw, snap) !== ''
  })
  const mcp = snap.mcp.some((m) => {
    const kind = mcpChip(m, false)
    return kind === 'setup' || kind === 'failed'
  })
  const plugins = (pkgs?.packages ?? []).some((p) => {
    const kind = pkgChip(p)
    return kind === 'restart' || kind === 'problem'
  })
  return { tools, mcp, plugins }
}
