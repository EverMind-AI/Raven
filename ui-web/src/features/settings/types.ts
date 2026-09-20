/* The settings domain's shapes: what one read of the dialog fills, and the
   seam (`SettingsSource`) the rpc layer implements for it. */
import type { ModelTagFacts } from '../../components/ModelTags'
import type {
  ApiUsageModel,
  ExtSkillRow,
  McpSnapshot,
  ResultOf,
  SettingsUsageResult,
  ToolSetupNeed,
} from '../../rpc/generated'

/* One provider row of the model page. Each source owns its provider list;
   the live source shares its fetched rows with the composer's model picker. */
export interface ProviderRow {
  id: string
  name: string
  homepage?: string
  /* The vendor's model index. Distinct from `homepage`: the question this page
     asks is which model to put in the list, and a front page does not answer
     it. Absent for a provider the registry carries no docs link for. */
  docs?: string
  /* Where the vendor hands out API keys, drawn as the "get a key" link beside
     the key field. Absent for a vendor the registry has no page for. */
  keyUrl?: string
  /* Custom request headers by name, each value redacted by the server. */
  headers?: Record<string, string>
  /* The picker's offer: this section's list plus a curated shortlist plus a
     catalogue. What the settings page manages is `configured` below. */
  models: string[]
  configured?: string[]
  /* Name and tags per model id, straight off `model.options`. A model with no
     entry is one the registry knows nothing about and nobody has described --
     it lists as its id with no icons. */
  labels?: Record<string, ModelTagFacts & { label?: string; description?: string }>
  on: boolean
  /* 'key' | 'oauth' | 'local' | 'endpoint' -- the registry's auth shape. */
  kind?: string
  /* Resells other vendors' models under vendor/model ids (the registry's
     `is_gateway`). The catalogue's filter reads it; no client can derive it
     from a slug. */
  gateway?: boolean
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
/* Two ISO dates, inclusive; the server clamps to 90 days back. */
export interface UsageRange {
  from: string
  to: string
}

/* One built-in tool. In live mode `on` is an accessor over
   `tools.disabledTools`, so assigning it persists the flip. */
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
  /* The registry's word: a tool the model reaches only through `tool_call`, so
     no switch belongs on it. Read from the row rather than inferred from the
     card it sits in -- the DAG controls belong beside the tool they control
     and are still not switchable. */
  builtin?: boolean
}

export type SkillRow = ExtSkillRow
export type SkillDetail = NonNullable<ResultOf<'skills.manage'>['info']>
export type ArchivedSession = ResultOf<'session.list'>['sessions'][number]
export type OauthStart = ResultOf<'model.oauth_login'>

/* Everything the dialog draws from, in one read. `raw` is the config
   settings.get returned (camelCased keys, one level per dot). The skill and
   MCP rows are `ext.list`'s own, untouched, because the two pages read fields
   the inventory's row shapes drop (`always`, `auth`, `credentialed`). */
export interface SettingsSnapshot {
  raw: Record<string, unknown>
  configPath: string
  everos: EverosInfo | null
  providers: ProviderRow[]
  curProvider: string
  model: string
  tools: ToolRow[]
  skills: SkillRow[]
  mcp: McpSnapshot[]
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

/* The DS.settings contract the rpc source implements. A write that fails
   toasts where the wording lives and throws { handled: true }, so the island
   only redraws; a write that succeeds resolves to the fresh snapshot where the
   page behind it changed. */
/* One credential field of a catalogue entry's MCP contribution: the key it is
   written under, and what to call it. `label` arrives from the hub as an i18n
   object and is flattened to this language by the source. */
export interface AuthField {
  key: string
  label?: string
  secret?: boolean
  help_url?: string
}

export interface SettingsSource {
  load(): Promise<SettingsSnapshot>
  set(key: string, value: unknown): Promise<SettingsSnapshot>
  /* `borrowFrom` names a provider raven is already connected to: the server
     copies its key and address into the section. It has to resolve there --
     the page is only ever shown a redacted key, so it has nothing to send. */
  everosSet(section: string, fields: Record<string, string> | null,
    borrowFrom?: string): Promise<SettingsSnapshot>
  usage(range: UsageRange): Promise<UsageStats | null>
  provider(op: ProviderOp, params: Record<string, unknown>): Promise<SettingsSnapshot>
  /* Ask the provider what it serves right now. A read: nothing is written
     until a row is added. */
  fetchModels(slug: string): Promise<ModelCatalogue>
  addModels(slug: string, models: string[]): Promise<SettingsSnapshot>
  /* The non-credential fields: api_base, deployment, api_version, and an
     extra_headers patch ({name: value | null}). */
  setFields(slug: string, fields: Record<string, unknown>): Promise<SettingsSnapshot>
  oauthLogin(slug: string): Promise<OauthStart>
  /* The chat role: agents.defaults.model and .provider, through the same write
     the composer's picker makes. */
  /** Resolves true when the write landed in a gateway that has to restart
      before it can chat on it (a first run). */
  pickModel(model: string, provider: string): Promise<boolean>
  model(): string
  defaultProvider(): string
  archived(): Promise<ArchivedSession[]>
  restore(id: string): Promise<void>
  removeSession(id: string): Promise<void>
  inspectSkill(name: string): Promise<SkillDetail>
  openSkillFile(name: string, file: string): Promise<void>
  uninstallSkill(name: string): Promise<SettingsSnapshot>
  /* The credential fields the catalogue declares for this server, empty for
     one nobody installed from the catalogue. A read, so no snapshot back. */
  serverAuthFields(name: string): Promise<AuthField[]>
  toggleServer(name: string, on: boolean): Promise<SettingsSnapshot>
  retryServer(name: string): Promise<SettingsSnapshot>
  revokeServer(name: string): Promise<SettingsSnapshot>
  /* An empty form value clears that credential field. */
  configureServer(name: string, form: Record<string, string>): Promise<SettingsSnapshot>
  /* Resolves once the browser tab is open; the token lands later. */
  authServer(name: string): Promise<SettingsSnapshot>
  version(): string | null
  /* The newer version when there is one, so the About row can offer the
     upgrade the way the design asked -- a check that silently starts an
     upgrade is a second action the reader did not ask for. */
  checkUpdate(btn: HTMLButtonElement): Promise<void>
  /* The newer version the page knows about, or null. Read on every draw
     rather than handed back by the check: the check redraws the dialog, which
     replaces the row that asked. */
  newerVersion(): string | null
  upgrade(): void
  /* The language pick. On the source rather than the shell because what a flip
     MEANS differs between the modes -- live persists it through config.language,
     which also drives the TUI and the language the agent replies in. */
  setLang(lang: string): void
}
