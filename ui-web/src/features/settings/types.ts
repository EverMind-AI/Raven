/* One provider row of the model panel. Each source owns its provider list;
   the live source shares its fetched rows with the composer's model picker. */
export interface ProviderRow {
  id: string
  name: string
  models: string[]
  on: boolean
  /* 'api_key' | 'oauth' | 'local' | 'endpoint' */
  kind?: string
  needsBase?: boolean
  env?: string
  warn?: string
  key?: string
}

export interface EverosSection {
  model?: string
  base_url?: string
  api_key_set?: boolean
}

export interface EverosInfo {
  sections?: Record<string, EverosSection>
}

export interface UsageModelRow {
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface UsageStats {
  days: number
  llm: {
    total: { calls: number; input_tokens: number; output_tokens: number; cost_usd: number }
    models: UsageModelRow[]
  }
  tools: {
    total: number
    counts: Array<{ name: string; count: number }>
  }
}

/* One group heading of the toolset panel. */
export interface ToolGroup {
  id: string
  label: string
  hint?: string
}

/* One built-in tool. Each settings source owns its list. In live mode `on`
   is an accessor over `tools.disabledTools`, so assigning it persists the
   flip. In the demo it is a plain field and the flip is local, which is what
   the offline page always did. */
export interface ToolRow {
  id: string
  name: string
  group: string
  reach: string
  one: string
  on: boolean
  danger?: boolean
  needs?: string | null
}

/* Everything the dialog draws from, in one read. `raw` is the config
   settings.get returned (camelCased keys, one level per dot); the fixture
   answers an empty object so every V() read falls back to the schema
   default, exactly as the demo page always painted. */
export interface SettingsSnapshot {
  raw: Record<string, unknown>
  configPath: string
  everos: EverosInfo | null
  providers: ProviderRow[]
  curProvider: string
  model: string
  toolGroups: ToolGroup[]
  tools: ToolRow[]
}

export type ProviderOp = 'save_key' | 'add_model' | 'remove_model' | 'disconnect'

/* The DS.settings contract both the fixture source (demo shell) and the rpc
   source (live layer) implement. Writes in the fixture throw { notLive: true },
   which the island renders as the in-row refusal the demo page always spoke;
   the rpc source speaks its own toasts and throws { handled: true } so the
   island only redraws. `usage` resolving null means "no counter behind this
   page" (the demo), not zero usage. `pickModel` is the live layer's model
   picker popover -- absent in the fixture, so the demo refuses the button. */
export interface SettingsSource {
  load(): Promise<SettingsSnapshot>
  set(key: string, value: unknown): Promise<SettingsSnapshot>
  /* `borrowFrom` names a provider raven is already connected to: the server
     copies its key and address into the section. It has to resolve there --
     the page is only ever shown a redacted key, so it has nothing to send. */
  everosSet(section: string, fields: Record<string, string> | null,
    borrowFrom?: string): Promise<SettingsSnapshot>
  usage(): Promise<UsageStats | null>
  provider(op: ProviderOp, params: Record<string, unknown>): Promise<SettingsSnapshot>
  model(): string
  /* The configured default provider, paired with model() above: the default
     badge must move with a cross-provider default pick without reopening the
     page. Optional because the offline demo has no live default to track. */
  defaultProvider?(): string
  version(): string | null
  checkUpdate(btn: HTMLButtonElement): void | Promise<void>
  pickModel?(anchor: HTMLElement, after: () => void): void
  /* The language pick. On the source rather than the shell because what a flip
     MEANS differs between the modes -- live persists it through config.language,
     which also drives the TUI and the language the agent replies in, while the
     offline page repaints and has nowhere to persist to. Required, since a
     settings page that cannot answer the pick would draw a dead control. */
  setLang(lang: string): void
}
