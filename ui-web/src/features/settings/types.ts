/* One provider row of the model panel. Each source owns its provider list;
   the live source shares its fetched rows with the composer's model picker. */
import type { ModelTagFacts } from '../../components/ModelTags'
import type { ApiUsageModel, SettingsUsageResult, ToolSetupNeed } from '../../rpc/generated'

export interface ProviderRow {
  id: string
  name: string
  homepage?: string
  /* The vendor's model index. Distinct from `homepage`: the question this page
     asks is which model to put in the list, and a front page does not answer
     it. Absent for a provider the registry carries no docs link for. */
  docs?: string
  /* The picker's offer: this section's list plus a curated shortlist plus a
     catalogue. What the settings page manages is `configured` below. */
  models: string[]
  configured?: string[]
  /* Name and tags per model id, straight off `model.options`. A model with no
     entry is one the registry knows nothing about and nobody has described --
     it lists as its id with no icons. */
  labels?: Record<string, ModelTagFacts & { label?: string; description?: string }>
  on: boolean
  /* 'api_key' | 'oauth' | 'local' | 'endpoint' */
  kind?: string
  needsBase?: boolean
  /* Whether the provider takes an API key at all. Absent from a source that
     predates the field, where every non-local provider took one. */
  acceptsKey?: boolean
  apiBase?: string
  defaultApiBase?: string
  /* Addresses this provider serves the same account model from, each with the
     signup that issues a key for it. Present only where the choice is the
     reader's; everywhere else the pane offers a host field instead. */
  platforms?: Array<{ label: string; api_base: string; signup_url: string }>
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
  /* False when this install has no EverOS to configure. The rows used to
     render "not set" in that case -- indistinguishable from an install where
     the plugin is present and merely unconfigured -- so a person could fill
     in a model and a key and have nothing happen. */
  available?: boolean
  note?: string | null
}

export type UsageModelRow = ApiUsageModel
export type UsageStats = SettingsUsageResult

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
  /* Set when the tool exists but is withheld for want of a key. The contract's
     own shape (`ToolSetupNeed`): the page reads it for truth only -- the row is
     here so a key-gated tool is visible rather than simply absent. */
  needs?: ToolSetupNeed | null
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

/* One model a provider reports. `added` is about this provider's configured
   list, not about the vendor: the same model behind two gateways is added to
   each separately. */
export interface ModelCandidate extends ModelTagFacts {
  id: string
  label: string
  kind: string
  added: boolean
  description?: string
}

/* `status` is the probe's own vocabulary -- `ok`, or why the vendor did not
   answer. An empty list with a status is not the same as a provider that
   genuinely serves nothing, so both travel. */
export interface ModelCatalogue {
  models: ModelCandidate[]
  status: string
  error?: string | null
}

/* The DS.settings contract both the offline fixture library and the rpc
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
  usage(sessionKey?: string): Promise<UsageStats | null>
  provider(op: ProviderOp, params: Record<string, unknown>): Promise<SettingsSnapshot>
  /* Ask the provider what it serves right now. Optional because the offline
     demo has no provider to ask -- absent, the button reports that rather than
     pretending to fetch. A read: nothing is written until a row is added. */
  fetchModels?(slug: string): Promise<ModelCatalogue>
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
