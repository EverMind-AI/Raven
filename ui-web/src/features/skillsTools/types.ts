/* Contract types for the Skills & Tools module page: tabs, modal targets,
 * snapshots, and the data seam (`SkillsToolsSource`).
 */
import type { McpSnapshot } from '../../rpc/generated'
import type {
  AgentPluginList,
  AgentPluginRow,
  AuthField,
  McpDetail,
  SettingsSnapshot,
  SkillDetail,
  SkillRow,
  ToolRow,
} from '../settings/types'

export type {
  AgentPluginList,
  AgentPluginRow,
  AuthField,
  McpDetail,
  McpSnapshot,
  SettingsSnapshot,
  SkillDetail,
  SkillRow,
  ToolRow,
}

export type SkillsToolsTab = 'skills' | 'tools' | 'mcp' | 'plugins'

export type SkillScopeFilter = 'all' | 'builtin' | 'workspace'

export type DetailTarget =
  | { kind: 'skill'; name: string }
  | { kind: 'tool'; id: string }
  | { kind: 'mcp'; name: string }
  | { kind: 'plugin'; name: string }

export interface SkillsToolsSource {
  load(): Promise<SettingsSnapshot>
  set(key: string, value: unknown): Promise<SettingsSnapshot>
  everosSet(section: string, model: string | null, provider?: string, protocol?: string): Promise<SettingsSnapshot>
  addModel(slug: string, model: string, tags: Record<string, unknown>): Promise<SettingsSnapshot>
  inspectSkill(name: string): Promise<SkillDetail>
  openSkillFile(name: string, file: string): Promise<void>
  uninstallSkill(name: string): Promise<SettingsSnapshot>
  blockSkill(name: string, blocked: boolean): Promise<SettingsSnapshot>
  serverDetail(name: string): Promise<McpDetail>
  toggleServer(name: string, on: boolean): Promise<SettingsSnapshot>
  retryServer(name: string): Promise<SettingsSnapshot>
  revokeServer(name: string): Promise<SettingsSnapshot>
  configureServer(name: string, form: Record<string, string>): Promise<SettingsSnapshot>
  authServer(name: string): Promise<SettingsSnapshot>
  agentPlugins(): Promise<AgentPluginList>
  setPackageMcp(name: string, on: boolean): Promise<AgentPluginList>
}
