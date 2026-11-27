/* Contract types for the Skills & Tools module page: tabs, modal targets,
 * snapshots, and the data seam (`SkillsToolsSource`).
 */
import type { McpSnapshot } from '../../rpc/generated'
import type {
  AgentPluginList,
  AgentPluginRow,
  AuthField,
  McpDetail,
  ProjectSkillRow,
  ProjectSkillsInfo,
  SettingsSnapshot,
  SkillDetail,
  SkillRow,
  ToolRow,
} from '../settings/types'
import type { DirListing, DirPick } from '../workspace/types'

export type {
  AgentPluginList,
  AgentPluginRow,
  AuthField,
  DirListing,
  DirPick,
  McpDetail,
  McpSnapshot,
  ProjectSkillRow,
  ProjectSkillsInfo,
  SettingsSnapshot,
  SkillDetail,
  SkillRow,
  ToolRow,
}

export type SkillsToolsTab = 'skills' | 'tools' | 'mcp' | 'plugins'

export type SkillScopeFilter = 'all' | 'builtin' | 'global' | 'project'

export type DetailTarget =
  | { kind: 'skill'; name: string; projectDir?: string }
  | { kind: 'tool'; id: string }
  | { kind: 'mcp'; name: string }
  | { kind: 'plugin'; name: string }

export interface SkillsToolsSource {
  load(): Promise<SettingsSnapshot>
  set(key: string, value: unknown): Promise<SettingsSnapshot>
  everosSet(section: string, model: string | null, provider?: string, protocol?: string): Promise<SettingsSnapshot>
  addModel(slug: string, model: string, tags: Record<string, unknown>): Promise<SettingsSnapshot>
  inspectSkill(name: string, projectDir?: string): Promise<SkillDetail>
  openSkillFile(name: string, file: string, projectDir?: string): Promise<void>
  projectSkills?(projectDir: string): Promise<ProjectSkillsInfo>
  listDirs?(path?: string): Promise<DirListing>
  pickDir?(): Promise<DirPick>
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
