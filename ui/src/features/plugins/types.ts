/* The raw connection snapshot of one MCP server, as ext.list / mcp.status
   report it. The live layer keeps it fresh in place; the island only reads. */
export interface McpSnapshot {
  name: string
  enabled: boolean
  state: string
  transport?: string
  tool_count?: number
  error?: string
  /* The authorization URL this server is parked on, when it is. Carried on the
     pull because the oauth.pending notification that also carries it is dropped
     when no client is attached, which is every connect started at assembly. */
  auth_url?: string | null
}

/* One installed row: a python plugin (state 'on' | 'off', persisted by the
   source) or an MCP server (`m` carries the snapshot). Rows are the live
   layer's own objects, mutated in place, so writes must go back through the
   source rather than through a copy. */
export interface InstalledRow {
  id: string
  name: string
  src?: string
  ver?: string
  state?: string
  m?: McpSnapshot
}

export interface MarketItem {
  id: string
  name: string
  publisher?: string
  summary?: string
  verified?: boolean
  installed?: boolean
  tool_preview_count?: number
  skill_count?: number
  risk_tier?: number
}

export interface AuthField {
  key: string
  label?: string
  secret?: boolean
  help_url?: string
}

export interface Contribution {
  kind: string
  name?: string
  skillhub_id?: string
  connection?: { url?: string; command?: string; args?: string[]; type?: string }
  auth?: { mode?: string; fields?: AuthField[] }
  tools_preview?: string[]
}

/* A catalog entry, text fields already flattened to plain strings by the
   source (the hub serves i18n objects; which language wins is the shell's
   call, not the island's). */
export interface DetailEntry {
  id: string
  name: string
  version?: string
  summary?: string
  description?: string
  homepage?: string
  publisher?: { name?: string; verified?: boolean }
  contributes?: Contribution[]
}

export interface InstallResult {
  pending?: boolean
  mcp?: McpSnapshot | null
}

/* Gateway events the live source forwards into the island: connection state
   flips, the OAuth round-trip, and "the installed rows were reloaded". */
export type PluginsEvent =
  | { kind: 'status'; name: string; state: string; tool_count?: number; error?: string; auth_url?: string | null }
  | { kind: 'authPending'; server: string; url: string; expires_in?: number; interactive?: boolean }
  | { kind: 'authDone'; server: string; ok: boolean; error?: string }
  | { kind: 'rows' }

/* The DS.plugins contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. The island only ever talks to this. */
export interface PluginsSource {
  search(q: string, category: string): Promise<{ items: MarketItem[]; categories: string[] }>
  detail(id: string): Promise<{ entry: DetailEntry; installed: boolean }>
  install(id: string, form: Record<string, string>): Promise<InstallResult>
  remove(name: string): Promise<unknown>
  toggle(name: string, enabled: boolean): Promise<McpSnapshot | null>
  togglePy(row: InstalledRow, on: boolean): Promise<unknown>
  auth(name: string): Promise<McpSnapshot | null>
  manual(name: string, address: string): Promise<unknown>
  rows(): InstalledRow[]
  /* See SkillsSource.loaded: one boot-time read fills both. */
  loaded?(): boolean
  reload(): Promise<unknown>
}
