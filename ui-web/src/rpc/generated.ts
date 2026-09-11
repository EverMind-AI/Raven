// AUTO-GENERATED -- DO NOT EDIT -- run `npm run gen`
//
// Source of truth: rpc-schema/openrpc.json (OpenRPC 1.2.6).
// Drift check: `npm run gen:check` (CI runs this; a stale file fails the build).
//
// 168 methods, 96 component schemas.

/* eslint-disable */
/**
 * Recursive JSON value type. Implemented as an unconstrained object in JSON Schema; downstream Pydantic uses typing.Any.
 */
export type JsonValue = string | number | boolean | null | unknown[] | {};
/**
 * Per-node status reported by the DAG runner. 'interrupted' never comes off the wire -- it is what a client infers for a node still called running when the run stopped reporting. 'exception' is a node that finished without accomplishing its task and is waiting on a decision; it is not terminal.
 */
export type DagNodeStatus =
  'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'cancelled' | 'exception';
/**
 * Per-node status in a run read back off disk. Unlike the event vocabulary this includes 'interrupted', which the server infers for a node the registry still calls running on a run nothing is executing. 'exception' is a node that finished without accomplishing its task and is waiting on a decision; it is not terminal.
 */
export type DagSnapshotNodeStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'cancelled'
  | 'interrupted'
  | 'exception';
/**
 * Discriminated union of turn streaming events. The 'type' field is the discriminator.
 */
export type TurnEvent =
  | MessageStartEvent
  | TurnStartedEvent
  | EpisodeStartEvent
  | NoticeEvent
  | PermissionReviewEvent
  | TokenDeltaEvent
  | ThinkingDeltaEvent
  | ToolStartEvent
  | ToolProgressEvent
  | ToolCompleteEvent
  | MessageCompleteEvent
  | ErrorEvent
  | CronDeliveredEvent
  | SubagentDeliveredEvent
  | SubagentStatusEvent
  | DagRunStartedEvent
  | DagNodeUpdatedEvent
  | DagRunReplannedEvent
  | DagNodeStalledEvent
  | DagRunCompletedEvent
  | CronMissedEvent
  | MediaEvent
  | SessionTitledEvent
  | SessionNamingEndedEvent;

export interface BrowserTab {
  index: number;
  url: string;
  title: string;
  active: boolean;
  loading?: boolean;
}
export interface BrowserConsoleLine {
  type: string;
  text: string;
}
/**
 * One actionable element, with the id a click can be aimed at.
 */
export interface BrowserRef {
  ref: string;
  role: string;
  name: string;
  x: number;
  y: number;
  href?: string;
  /**
   * Never present for a password field.
   */
  value?: string;
  disabled?: boolean;
}
export interface SessionCompressSummary {
  headline: string;
  /**
   * True when nothing moved, including when the context engine owns compaction.
   */
  noop: boolean;
  note?: string;
  token_line?: string;
}
/**
 * The banner bundle: which model, which tools and skills, how full.
 */
export interface SessionInitInfo {
  model: string;
  model_id: string;
  provider: string;
  context_window: number;
  /**
   * True when no agent loop was running, so tools/skills are empty.
   */
  lazy: boolean;
  /**
   * Skill names grouped by source.
   */
  skills: {
    [k: string]: string[];
  };
  /**
   * Tool names in a single 'builtin' bucket.
   */
  tools: {
    [k: string]: string[];
  };
  usage: SessionUsage;
  version: string;
  cwd: string;
  mcp_servers: JsonValue[];
  update_available?: boolean;
  /**
   * The command that would install the newer release.
   */
  update_command?: string;
  /**
   * What a config migration changed on the user's behalf during this boot. Drained, so only the first session of a launch carries them.
   */
  config_notices?: string[];
  /**
   * Which of a multi-endpoint provider's endpoints this session is on; null for single-endpoint ones.
   */
  endpoint?: string;
  /**
   * The resumed session's name, when it has one. Absent on a fresh session, which has nothing to name yet. Carried on the bundle rather than fetched separately because a client resuming a session is already being told what it is resuming.
   */
  title?: string;
}
/**
 * ``info.usage`` — the boot baseline, refreshed by each turn's completion.
 *
 * Distinct from :class:`TurnUsage`, which is the per-turn event payload:
 * this one carries the context-window fill a banner draws, and its counters
 * are named for the session rather than for one LLM call.
 */
export interface SessionUsage {
  input: number;
  output: number;
  cost_usd: number;
  calls: number;
  context_max: number;
  context_used: number;
  context_percent: number;
  /**
   * True when context_used is a tiktoken estimate of a resumed transcript, not a measurement.
   */
  context_estimated?: boolean;
}
/**
 * One stored message in wire form: ``content`` renamed to ``text``.
 */
export interface TranscriptMessage {
  role: string;
  text?: string;
  context?: JsonValue;
  name?: string;
  tool_call_id?: string;
  /**
   * The run a run_subagent_dag call started, so a resumed transcript can fetch its graph through dag.get. Absent on every other tool, and on a graph that was rejected before it ran.
   */
  dag_run_id?: string;
  /**
   * The task id a spawn call's result names, so a resumed transcript can find the run's record through subagent.list. Absent on every other tool, and on a spawn that was refused before it ran.
   */
  spawn_task_id?: string;
  timestamp?: string;
  reasoning_content?: string;
  /**
   * How long the thought on this assistant entry took, measured server-side from the first reasoning delta to the first answer token or tool call. Absent means unknown (an unstreamed call, or a session written before it was recorded) -- render the bare header, never a zero.
   */
  reasoning_ms?: number;
  tool_calls?: TranscriptToolCall[];
  /**
   * How long the call this role='tool' entry answers ran, dispatch to result. Absent means unknown, same rule as reasoning_ms.
   */
  duration_ms?: number;
  /**
   * A file tool's unified diff of the change it made, on its role='tool' entry.
   */
  diff?: string;
  turn_ended?: TranscriptTurnEnded;
  notice?: TranscriptNotice;
  /**
   * Present on the user entry of a turn the runtime opened, naming what opened it (`subagent`, `cron`, `sentinel`, `heartbeat`). Absent means a person typed it. Same rule as `notice`, one role over: the model reads `text`, a reader must not -- a sub-agent's announce carries an untrusted fence, an instance handle and an instruction not to repeat either to the user, and a cron reminder carries how to word the reply.
   */
  origin?: string;
  delegated?: TranscriptDelegated;
}
export interface TranscriptToolCall {
  id: string;
  name: string;
  /**
   * JSON-encoded arguments; re-serialized when stored as an object.
   */
  arguments: string;
}
/**
 * Why a turn's transcript stops where it does.
 */
export interface TranscriptTurnEnded {
  /**
   * 'cancelled' (a person stopped it) or 'failed'.
   */
  status: string;
  reason?: string;
}
/**
 * Marks an assistant entry the runtime wrote rather than the model.
 */
export interface TranscriptNotice {
  /**
   * Which runtime decision this reports; `action_blocked` today.
   */
  kind: string;
  detail?: string;
}
/**
 * Which delegated run re-entered the conversation at this entry. The same identity `subagent.delivered` carries, so a replayed transcript and a live stream draw the same row from the same fields.
 */
export interface TranscriptDelegated {
  kind: 'spawn' | 'dag';
  label: string;
  status: 'ok' | 'error' | 'exception' | 'notice';
  /**
   * Set for kind=dag, so a client can open the run.
   */
  run_id?: string;
  /**
   * Set for a dag node's own message, so a client can place it against that row.
   */
  node_id?: string;
}
export interface ExtPluginRow {
  id: string;
  display_name: string;
  version: string;
  enabled: boolean;
  bundled: boolean;
}
export interface ExtSkillRow {
  name: string;
  description: string;
  source: string;
  always: boolean;
  /**
   * Installed from the skill hub, so skillhub.remove can uninstall it.
   */
  hub: boolean;
  hub_id: string;
}
export interface ExtToolRow {
  name: string;
  description: string;
  enabled: boolean;
  /**
   * Owning MCP server, or null for a built-in tool.
   */
  mcp_server?: string;
  needs?: ToolSetupNeed;
}
/**
 * Set when the tool exists but is withheld for want of a key. The model cannot call it; the row is here so the page can offer the field instead of the tool simply being absent.
 */
export interface ToolSetupNeed {
  /**
   * Dotted config key that unlocks the tool.
   */
  setting: string;
  /**
   * Environment variable accepted instead.
   */
  env?: string;
}
export interface ToolSetupNeed1 {
  /**
   * Dotted config key that unlocks the tool.
   */
  setting: string;
  /**
   * Environment variable accepted instead.
   */
  env?: string;
}
export interface CronJobInfo {
  id: string;
  name: string;
  enabled: boolean;
  kind: 'at' | 'every' | 'cron';
  expr?: string;
  every_ms?: number;
  at_ms?: number;
  tz?: string;
  message: string;
  next_run_at_ms?: number;
  last_run_at_ms?: number;
  last_status?: 'ok' | 'error' | 'skipped';
  last_error?: string;
}
export interface CronRun {
  at_ms?: number;
  ok: boolean;
  preview: string;
}
export interface ApiUsageModel {
  calls: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cost_usd?: number | null;
  model: string;
  cache_write_tokens?: number | null;
  cost_missing_calls: number;
  cache_read_missing_calls: number;
  cache_write_missing_calls: number;
  legacy_cost_calls: number;
  input_missing_calls?: number;
  output_missing_calls?: number;
}
export interface ApiUsageTotals {
  calls: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cost_usd?: number | null;
  cache_write_tokens?: number | null;
  cost_missing_calls: number;
  cache_read_missing_calls: number;
  cache_write_missing_calls: number;
  legacy_cost_calls: number;
  input_missing_calls?: number;
  output_missing_calls?: number;
}
export interface LlmUsage {
  total: ApiUsageTotals;
  /**
   * Most expensive first.
   */
  models: ApiUsageModel[];
}
export interface ToolUsage {
  total: number;
  /**
   * Most called first.
   */
  counts: ToolUsageCount[];
}
export interface ToolUsageCount {
  name: string;
  count: number;
}
export interface EverosSection {
  /**
   * Empty when the shipped placeholder is still in place.
   */
  model: string;
  base_url: string;
  provider: string;
  /**
   * Whether a key is stored; the value never goes on the wire.
   */
  api_key_set: boolean;
}
export interface ChannelField {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
  /**
   * Whether a value is stored; the value itself never rides the wire.
   */
  set: boolean;
}
export interface ChannelStatusRow {
  name: string;
  enabled: boolean;
  configured: boolean;
  /**
   * Required fields still empty.
   */
  missing: string[];
  /**
   * Every field the channel takes, so a client can render its configure form.
   */
  fields?: ChannelField[];
  /**
   * Whether the adapter is up, read from the live gateway. Null when no gateway answered.
   */
  running?: boolean;
  /**
   * Whether the account is paired. Only the QR-login channels report this; null means the channel does not report a pairing and must not be drawn as disconnected.
   */
  connected?: boolean;
  /**
   * Whether this channel signs in by scanning a code.
   */
  qr_login?: boolean;
}
export interface FsEntry {
  name: string;
  dir: boolean;
  /**
   * Zero for a directory.
   */
  size: number;
}
/**
 * One row, projected card-sized. ``kind`` decides which optional fields
 * carry a value: the four memory types share only ``id`` and ``kind``.
 */
export interface MemoryItem {
  id: string;
  kind: 'episode' | 'profile' | 'agent_case' | 'agent_skill';
  /**
   * Present on search hits only.
   */
  score?: number;
  session_id?: string;
  timestamp?: string;
  subject?: string;
  summary?: string;
  body?: string;
  profile_data?: {
    [k: string]: JsonValue;
  };
  key_insight?: string;
  quality_score?: number;
  confidence?: number;
  maturity_score?: number;
}
/**
 * The card-sized projection of a catalogue entry.
 */
export interface PlughubCatalogItem {
  id: string;
  version?: string;
  name: string;
  summary: string;
  category: string;
  verified: boolean;
  publisher: string;
  risk_tier: number;
  auth_mode: 'none' | 'apikey' | 'oauth';
  /**
   * None when the entry contributes no MCP server.
   */
  transport?: string;
  tool_preview_count: number;
  skill_count: number;
  /**
   * Which contribution kinds the entry carries: mcp, skill, python.
   */
  kinds: string[];
  installed: boolean;
}
/**
 * One server's live connection state, as `MCPConnectionManager` reports it.
 *
 * Every mutating `plug.*` call answers with this, and the gateway broadcasts the
 * same shape as an `mcp.status` notification, so a client renders one state
 * machine rather than two.
 */
export interface McpSnapshot {
  name: string;
  /**
   * stdio | sse | streamableHttp, or 'unknown'.
   */
  transport: string;
  state: 'disconnected' | 'connecting' | 'connected' | 'auth_required' | 'error';
  connected: boolean;
  tool_count: number;
  error?: string;
  enabled: boolean;
  /**
   * The authorization URL this server is parked on, when it is. Carried on the pull because the `oauth.pending` notification that also carries it is dropped when no client is attached, which is every connect started at assembly time.
   */
  auth_url?: string;
}
/**
 * What the install actually landed, which is what uninstall replays.
 */
export interface PlugLedger {
  catalog_id: string;
  /**
   * One entry per landed piece: {kind: 'mcp', server} or {kind: 'skill', name, skillhub_id}.
   */
  pieces: {
    [k: string]: JsonValue;
  }[];
}
export interface SkillhubItem {
  id: string;
  skill_id: string;
  name: string;
  description: string;
  source: string;
  source_url: string;
  category: string;
  quality_score: number;
  install_count: number;
  github_star: number;
  license: string;
  tags: string[];
  installed: boolean;
  /**
   * The local directory name when installed, else empty.
   */
  installed_name: string;
}
export interface SkillhubSubscores {
  utility: number;
  robustness: number;
  safety: number;
  flags: string[];
}
export interface SessionInfo {
  /**
   * <channel>:<chat_id> composite key.
   */
  session_key: string;
  channel: string;
  chat_id: string;
  /**
   * ISO-8601 timestamp.
   */
  created_at: string;
  /**
   * ISO-8601 timestamp.
   */
  updated_at: string;
  message_count: number;
  metadata: {
    [k: string]: JsonValue;
  };
  has_pending_clarification: boolean;
}
/**
 * One row in the TUI session picker (gatewayTypes.ts SessionListItem).
 */
export interface SessionListItem {
  /**
   * Full session_key: <channel>:<chat_id>.
   */
  id: string;
  message_count: number;
  /**
   * First user message, used as the untitled-session identity fallback.
   */
  preview: string;
  /**
   * Latest non-empty user or assistant message text.
   */
  last_message_preview: string;
  source?: string;
  /**
   * Unix timestamp derived from created_at.
   */
  started_at: number;
  /**
   * Unix timestamp of the latest user or assistant message.
   */
  updated_at: number;
  title: string;
  /**
   * User pinned this session to the top of the picker.
   */
  pinned?: boolean;
}
export interface SessionMessage {
  /**
   * 0-based position within session.messages.
   */
  index: number;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  /**
   * ISO-8601 timestamp.
   */
  timestamp: string;
  metadata?: {
    [k: string]: JsonValue;
  };
}
export interface McpServerInfo {
  name: string;
  transport: 'stdio' | 'sse' | 'streamableHttp';
  connected: boolean;
  tool_count: number;
}
export interface McpToolInfo {
  /**
   * Raw tool name (without mcp_<server>_ prefix).
   */
  name: string;
  description: string;
  /**
   * JSON Schema for the tool's input arguments.
   */
  parameters: {
    [k: string]: JsonValue;
  };
}
export interface SkillInfo {
  name: string;
  source: 'local' | 'remote';
  pinned: boolean;
  description: string;
  tags: string[];
}
export interface SubagentCall {
  /**
   * A spawn's call id, or '<run_id>/<node>' for a graph node. Opaque to a client: a spawn row opens through subagent.context and a dag row through dag.node, which is what kind is for.
   */
  id: string;
  /**
   * spawn | dag. Which delegation path made this, and so how it is opened.
   */
  kind?: string;
  /**
   * dag rows only: the graph run this node belongs to.
   */
  run_id?: string;
  /**
   * dag rows only: the node's id inside that run.
   */
  node?: string;
  /**
   * What it was asked, in short.
   */
  label: string;
  /**
   * run | ok | error | cancelled | queued | skipped. The last two only occur for graph nodes.
   */
  status: string;
  /**
   * The external agent that ran it, or null for a raven subagent.
   */
  agent?: string;
  /**
   * The handle sharing one stateful agent session, if any.
   */
  instance?: string;
  /**
   * ISO 8601; the record stores epoch millis.
   */
  started_at?: string;
  /**
   * Absent while it is still running.
   */
  ended_at?: string;
  /**
   * What was asked, plus the answer once there is one.
   */
  message_count: number;
  /**
   * Input plus output tokens this run spent. Null, not zero, when the transport that ran it cannot report usage -- the cli lane never can.
   */
  tokens?: number;
  /**
   * How many tools it called. Null where the transport has no per-step visibility.
   */
  tool_call_count?: number;
}
export interface SubagentRow {
  name: string;
  preset?: string;
  kind: 'builtin' | 'cli' | 'openai' | 'acp';
  description: string;
  enabled: boolean;
  configured: boolean;
  /**
   * Discovered under the `agents/` product tree rather than written into config: one of the agent products that ship with this raven, materialized as a row on every table build. Like `builtin` it leaves `configured` false -- there is no config entry to delete, and removing it means removing its folder -- but unlike `builtin` it is a real subprocess with a command, so it is probed and it can be unready (a missing engine wheel leaves it listed and disabled). The wire name predates the tree's rename. Absent from a server that predates discovery.
   */
  vendored?: boolean;
  /**
   * Always false from this server: the fork-era venv build is gone with the venvs, and `subagents.build` answers that there is nothing to build. Kept for wire compatibility with clients that predate the product tree. Absent from a server that predates discovery.
   */
  building?: boolean;
  /**
   * Reusing a handle continues this agent's conversation rather than starting a fresh one (`agent_meta`). What makes a row direct-chattable at all: `chat` refuses a stateless agent, so a picker that offers one is offering a refusal. Absent from a server that predates this, which reads as 'not offered' rather than as an error.
   */
  stateful?: boolean;
  /**
   * A built-in agent: raven's own in-process loop, on the agent table whether or not config mentions it. Distinct from `configured`, which stays false for one -- not writing a row is how 'use the package's default' is spelled, so there is nothing to delete and no transport to connect. It takes no action at all: `subagents.toggle` answers `config_field_readonly` for it, because an unnamed spawn and a dag node with no sub-agent both dispatch to this row, so it cannot leave the roster. A client must not offer a switch, a test or a delete for it.
   */
  builtin?: boolean;
  /**
   * The transport this entry's preset has since moved to, or null when it is current. A configured entry is never rewritten underneath the user, so the mismatch is shown instead.
   */
  upgrade_to?: string;
  group: 'builtin' | 'installed' | 'uninstalled';
  probe_status: 'ready' | 'attention' | 'missing' | 'unknown';
  probe_detail: string;
  has_api_key: boolean;
  mcps: string[];
  allow_mcp_secrets: boolean;
  last_test_ok?: boolean;
  last_test_detail?: string;
  last_test_at_ms?: number;
  test_running: boolean;
}
export interface ModelOptionProvider {
  slug: string;
  name: string;
  homepage?: string;
  /**
   * The vendor's own model index. Distinct from `homepage`: the question a settings page asks is which model to put here, and a marketing front page does not answer it.
   */
  docs?: string;
  authenticated: boolean;
  is_current: boolean;
  auth_type: string;
  key_env?: string;
  api_base?: string;
  default_api_base?: string;
  models: string[];
  /**
   * Only what the provider's config section lists. `models` is the picker's offer -- config plus a curated shortlist plus a catalogue -- so a page managing the list reads this one or it shows models nobody added.
   */
  configured_models?: string[];
  /**
   * Whether to draw a key field. False for an OAuth flow and for a local deployment reached by address alone; true for the local servers that can be put behind a token, which the registry declares rather than each surface matching on the slug.
   */
  accepts_api_key?: boolean;
  /**
   * Effective API protocol keyed by model id.
   */
  protocols?: {
    [k: string]: string;
  };
  /**
   * Explicit user protocol overrides keyed by model id.
   */
  protocol_overrides?: {
    [k: string]: string;
  };
  total_models: number;
  needs_api_base: boolean;
  warning: string;
  /**
   * Keyed by the model id as it appears in `models`.
   */
  model_labels?: {
    [k: string]: ModelLabel;
  };
}
/**
 * How a model reads to a person, and what it can do. Absent for a model the registry knows nothing about -- one released since the bundled files, or served by a local deployment -- in which case the id is all there is to show. An empty tag list means nothing is published, not that the model cannot: a surface renders absence as no icon, never as a denial.
 */
export interface ModelLabel {
  label: string;
  description?: string;
  /**
   * Closed vocabulary, drawn as icons: function-call, reasoning, structured-output, image-recognition, audio-recognition, video-recognition, file-input, image-generation, audio-generation, video-generation, embedding, rerank, computer-use.
   */
  capabilities?: string[];
  /**
   * What the model reads: text, image, video, audio, vector.
   */
  input_modalities?: string[];
  /**
   * What the model writes: text, image, video, audio, vector.
   */
  output_modalities?: string[];
  /**
   * Tokens the model reads in one request, from the tables that also route rather than from the display registry -- the number shown has to be the number a request is sized with. Absent where no such table names the model.
   */
  context_window?: number;
}
/**
 * One model a provider reports. `added` is about this provider's configured list, not about the vendor: the same model offered by two gateways is added to each separately.
 */
export interface ModelCandidate {
  id: string;
  label: string;
  /**
   * The bucket the list filters by: text, image, embedding, reranker, audio or video. Derived from what the model writes -- reading pictures is something a text model does.
   */
  kind: string;
  added: boolean;
  /**
   * `live` when the vendor named this model just now, `registry` when only the bundled catalogue does. A registry row is not less real: it is how a provider lists at all before a key is entered.
   */
  source?: string;
  description?: string;
  capabilities?: string[];
  input_modalities?: string[];
  output_modalities?: string[];
  context_window?: number;
}
/**
 * One of a provider section's several url/key groups. `label` is the idempotency key the write methods address an entry by.
 */
export interface ProviderEndpointInfo {
  label: string;
  /**
   * Redacted for display: `****set****` or `(empty)`.
   */
  api_key: string;
  api_base?: string;
  extra_headers?: {
    [k: string]: string;
  };
}
/**
 * One sub-agent instance a turn is addressed to. Null means the main conversation.
 */
export interface DirectTarget {
  agent: string;
  handle: string;
}
/**
 * One sub-agent instance this session has used, verbatim from the instance registry apart from `resumable`. camelCase because the web RPC serves the same record unchanged. `runId` / `nodeId` name the DAG node an instance belongs to: set on a dag-node row, and on the ordinary row of a stateful node, which is what lets one invocation's two rows be reported as one.
 */
export interface InstanceRow {
  sessionKey: string;
  agent: string;
  handle: string;
  kind: string;
  status?: string;
  agentId?: string;
  runId?: string;
  nodeId?: string;
  createdAtMs?: number;
  updatedAtMs?: number;
  /**
   * Whether a conversation can be opened with this row. False for a dag-node row, whose handle names a node rather than a conversation, and for an agent whose backend is not stateful. Answered by the server because statefulness is a property of the agent's configured backend, which no front end can read off the row.
   */
  resumable?: boolean;
  /**
   * What this instance was asked, in one line: a spawn's task_summary, or a graph node's node_summary, or -- for an instance nobody dispatched, one the user made by hand -- the first line of the message that opened it, taken once so later messages do not rename it. Absent only for work that ran before any of those existed, or when that first message yielded nothing; a reader falls back to the handle.
   */
  title?: string;
  /**
   * What the graph this instance belongs to was dispatched for. Absent, not empty, for an instance that came from no orchestration, so its presence is what says the row has a source.
   */
  runTitle?: string;
}
/**
 * One row of one instance's conversation. Preferred source is the instance's own log, which is written turn by turn and holds what the run did on the way; a conversation with no log falls back to its record directories, where a turn is a pair of files.
 */
export interface DirectTurn {
  /**
   * How this turn is addressed, which depends on which lane wrote it. A spawn turn carries the node id the model chose, and subagent.context reads it. A DAG node turn carries <run_id>/<node_id>, which subagent.context rejects (it refuses any id containing a slash) -- open those through dag.node. A direct-chat turn keeps its minted call id. Still spelled call_id on the wire for every lane.
   */
  call_id: string;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  at_ms: number;
  prompt_path?: string;
  out_path?: string;
  /**
   * The thought that preceded this turn, where the transport reports one.
   */
  reasoning_content?: string;
  /**
   * What this turn called on the way, matched to a later role='tool' row by id.
   */
  tool_calls?: TranscriptToolCall[];
  /**
   * On a role='tool' row, the call it answers.
   */
  tool_call_id?: string;
  /**
   * Set on a row from a turn still running, which no record holds yet. Such rows are a snapshot: the next read replaces them, and the record replaces them once the turn lands.
   */
  live?: boolean;
  /**
   * Set on a user row that was a steer: words merged into a turn already running, not the prompt that opened one. A reader draws it inside the turn rather than as a new one.
   */
  steer?: boolean;
  /**
   * Set on a trailing user row whose turn died with its session: nothing is running now and no reply ever landed. A reader marks the question as interrupted rather than leaving it hanging.
   */
  interrupted?: boolean;
}
export interface TurnUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd?: number | null;
  context_used?: number;
  context_max?: number;
  context_percent?: number;
  cost_missing_calls?: number;
}
export interface CliResult {
  /**
   * Rich-rendered output with ANSI SGR sequences.
   */
  stdout: string;
  /**
   * Error / warning output with ANSI SGR sequences.
   */
  stderr: string;
  /**
   * CLI command exit code; 0 = success.
   */
  exit_code: number;
  /**
   * Only present for timeout / not-dispatch-compatible cases (mirrors a JSON-RPC error code).
   */
  error_code?: number;
}
export interface StubResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface CommandsCatalogResponse {
  /**
   * alias (with leading /) -> canonical mapping. v0.1: alias = canonical 1:1 (no short aliases generated; TS-side prefix-1-match handles partials). Group + subcommand are space-separated (e.g. '/channels status'), not slash- or dot-separated.
   */
  canon: {
    [k: string]: string;
  };
  /**
   * Ordered (alias, canonical) tuples. TS-side createGatewayEventHandler.ts:198 gates on non-empty pairs; empty pairs degrades to slash.exec direct dispatch.
   */
  pairs: [unknown, unknown][];
  /**
   * group -> [subcommand]. Blacklisted entries (e.g. channels login) and agent-REPL are filtered out.
   */
  sub: {
    [k: string]: string[];
  };
  /**
   * Ordered category list: '(top-level)' first then alphabetical group names. Fully filtered groups (e.g. tui) do not appear.
   */
  categories: string[];
  /**
   * Total skill count from skill_forge store SQL count; 0 if DB missing (warning field then populated).
   */
  skill_count: number;
  /**
   * Optional warning pushed to TUI activity strip (e.g. 'skill store not initialized').
   */
  warning?: string;
}
/**
 * A turn the runtime opened (a delegated result re-entering the conversation) has begun. `turn.send` owns `message.start`, and the spine suppresses it for these turns, so this event is the client's only live signal that a new turn started. It advances the client's live turn bookkeeping (the workspace record's turn number, which the artifact bar reads), enters the busy state, and -- when `delegated` is present -- carries the delivery identity AND the injected text, so the delivery row is drawn at the moment the result is actually visible (its turn started), not when it was submitted. A stored delegated user entry draws the same row on replay, which is what keeps the two views in step.
 */
export interface TurnStartedEvent {
  type: 'turn.started';
  payload: {
    turn_id: string;
    delegated?: {
      kind: 'spawn' | 'dag';
      label: string;
      status: 'ok' | 'error' | 'exception' | 'notice';
      /**
       * Which node of the run this is about. Present only on `kind: dag` with `status: exception`, where the report concerns one node rather than the whole run.
       */
      node_id?: string;
      run_id?: string;
      /**
       * The text that re-entered the conversation, verbatim; a client shows the reader-facing part of it by keeping only what sits INSIDE the untrusted fence.
       */
      content?: string;
    };
    target?: DirectTarget;
  };
}
export interface MessageStartEvent {
  type: 'message.start';
  /**
   * `target` names the conversation this event belongs to; absent is the main agent.
   */
  payload: {
    turn_id: string;
    /**
     * The message that started the turn. Present so a client that did not send it can draw the question: the user entry reaches the transcript only at turn end.
     */
    content?: string;
    target?: DirectTarget;
  };
}
export interface EpisodeStartEvent {
  type: 'episode.start';
  payload: {
    index: number;
  };
}
/**
 * The smart-mode permission reviewer started or finished looking at one tool call. Presentational only: it lets a surface name the pause on the running tool row instead of showing an unexplained stall; decisions never depend on it.
 */
export interface PermissionReviewEvent {
  type: 'permission.review';
  payload: {
    /**
     * started | ended.
     */
    phase: string;
    /**
     * The tool name under review.
     */
    tool: string;
  };
}
/**
 * Prose the runtime wrote, not the model.
 *
 * It must not arrive as `token.delta`: that buffer is the model's voice, so the text would render as the answer -- glued to whatever the model narrated just before it, carrying the answer's copy and branch actions, and stuck in English whatever language the turn was in.
 */
export interface NoticeEvent {
  type: 'notice';
  payload: {
    /**
     * Which runtime decision this reports; `action_blocked` today.
     */
    kind: string;
    /**
     * The blocking tool's own first line, when it gave one.
     */
    detail?: string;
  };
}
export interface TokenDeltaEvent {
  type: 'token.delta';
  payload: {
    text: string;
    target?: DirectTarget;
  };
}
export interface ThinkingDeltaEvent {
  type: 'thinking.delta';
  payload: {
    text: string;
  };
}
export interface ToolStartEvent {
  type: 'tool.start';
  payload: {
    tool_call_id: string;
    name: string;
    arguments: {
      [k: string]: JsonValue;
    };
    display?: string | null;
    /**
     * The call is a blocking interaction with no automatic deadline. A client that clocks the event stream for liveness must suspend that clock while it is in flight.
     */
    blocking?: boolean;
  };
}
export interface ToolProgressEvent {
  type: 'tool.progress';
  payload: {
    tool_call_id: string;
    preview: string;
  };
}
/**
 * One file a tool call wrote, as contents rather than as a rendering of them. Beside ToolCompleteEvent.diff rather than instead of it: a client that draws its own diff needs the text, and a unified diff cannot be turned back into the file.
 */
export interface FileChange {
  /**
   * Absolute path of the file that was written.
   */
  path: string;
  /**
   * The file's full contents after the write.
   */
  after: string;
  /**
   * The contents the write replaced. Absent when the file did not exist, so a client renders a creation differently from a rewrite; an empty string means the file existed and was empty.
   */
  before?: string;
}
export interface ToolCompleteEvent {
  type: 'tool.complete';
  payload: {
    tool_call_id: string;
    result_preview: string;
    truncated: boolean;
    /**
     * Whether the tool call succeeded, by the emit site's verdict.
     */
    ok?: boolean;
    metadata?: {
      [k: string]: JsonValue;
    };
    /**
     * Unified diff of what the call changed on disk, when the tool could produce one.
     */
    diff?: string;
    file_change?: FileChange;
  };
}
export interface MessageCompleteEvent {
  type: 'message.complete';
  payload: {
    turn_id: string;
    usage: TurnUsage;
    target?: DirectTarget;
    /**
     * How long the whole turn took, measured server-side from the moment the runner picked the turn up to the moment it returned. Sent so a live client does not have to time the turn with its own clock: a browser stopwatch starts when the events arrive rather than when the work did, and only exists while that page is open, so the same turn came out one number live and another after a reload. Absent means unknown, same rule as reasoning_ms -- fall back to timing it locally, never to zero.
     */
    duration_ms?: number;
  };
}
export interface ErrorEvent {
  type: 'error';
  payload: {
    code: number;
    /**
     * The turn this failure belongs to. Empty when the emitter did not know it; a consumer that correlates a request to a turn must not treat an empty value as its own.
     */
    turn_id?: string;
    message: string;
    reason?: 'cancelled_by_client' | 'internal';
    detail?: string;
    target?: DirectTarget;
  };
}
export interface CronDeliveredEvent {
  type: 'cron.delivered';
  payload: {
    job_id: string;
    name: string;
    text: string;
    fired_at: string;
  };
}
export interface CronMissedEvent {
  type: 'cron.missed';
  payload: {
    count: number;
    items: {
      name: string;
      scheduled_at: string;
      message: string;
    }[];
  };
}
/**
 * The model named this session, reading the opening message alongside the turn that carried it. Conversation-scoped, and emitted only when the title actually changed -- a client can replace what it is showing without comparing. A front end that parks a placeholder where the title goes fills it in here instead of showing a truncated first line and rewriting it a moment later.
 */
export interface SessionTitledEvent {
  type: 'session.titled';
  payload: {
    session_id: string;
    title: string;
  };
}
/**
 * The naming call for this session finished without publishing a title. The other half of `session.titled`: exactly one of the two follows a turn whose `turn.send` reported `naming: true`, so a client parking a placeholder where the title goes can stop waiting on either instead of waiting out a grace period. `reason` is for the log and the bug report rather than for the reader -- 'timeout' the call outran its budget, 'no_title' it came back with nothing usable (the model answered without calling the naming tool, or the provider failed and generate_title swallowed it, logging the cause at debug), 'error' the naming code itself raised, 'renamed' a person named the session while the call ran.
 */
export interface SessionNamingEndedEvent {
  type: 'session.naming_ended';
  payload: {
    session_id: string;
    reason: 'timeout' | 'error' | 'no_title' | 'renamed';
  };
}
/**
 * A delegated run's result re-entered its conversation here. Emitted just after the result is submitted to the main loop, so a client can mark the seam where the sub-agent's answer came back. The turn it opens is submitted through the spine, which emits no `message.start`, so this event is the only live signal that it happened -- and a client REPLAYING the session later reads the same identity off the stored user entry's `delegated` field, which is what keeps the two views agreeing.
 */
export interface SubagentDeliveredEvent {
  type: 'subagent.delivered';
  payload: {
    kind: 'spawn' | 'dag';
    /**
     * The spawn's display label, or the dag's run_id.
     */
    label: string;
    status: 'ok' | 'error' | 'exception' | 'notice';
    /**
     * Set for kind=dag, so a client can open the run.
     */
    run_id?: string;
    /**
     * The text that re-entered the conversation, verbatim. A client shows the reader-facing part of it by keeping only what sits INSIDE the untrusted fence -- and a client replaying this turn later reads the same string from the stored entry's `text`, so one rule over one input keeps the live view and the reloaded one from disagreeing about what was delivered.
     */
    content?: string;
    /**
     * Set for a dag node's own message, so a client can place it against that row.
     */
    node_id?: string;
  };
}
/**
 * One spawned run's lifecycle transition, as it happens. 'subagent.delivered' marks where a finished result re-entered the conversation; this event is the run itself moving -- queued, started, finished -- so a client can render live delegation without polling the disk-backed lists. Terminal transitions are not replayed: a client that reconnects reconciles against subagent.list instead.
 */
export interface SubagentStatusEvent {
  type: 'subagent.status';
  payload: {
    /**
     * The manager's short id for this run, stable across its lifecycle.
     */
    task_id: string;
    agent: string;
    label: string;
    status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
    /**
     * The node id subagent.context reads -- the model's own name for the task, shared with run_subagent_dag's node ids. Sent from 'running' onward: the id is settled before dispatch, but a pending run has written no record, and advertising an id whose read comes back empty would draw a transcript that does not exist yet.
     */
    call_id?: string;
    /**
     * The spawn tool call that dispatched this run, when the host correlates the two. What lets a client pin the run onto the tool row that made it, the same way dag.run_started names its call.
     */
    tool_call_id?: string;
    /**
     * The addressable handle, when the caller named or minted one.
     */
    instance?: string;
    started_at?: number;
    ended_at?: number;
  };
}
export interface DagSnapshotNode {
  node: string;
  subagent?: string;
  depends_on?: string[];
  instance?: string;
  status: DagSnapshotNodeStatus;
  started_at?: number;
  ended_at?: number;
  prompt_file?: string;
  output_file?: string;
  error?: string;
  prompt_template?: string;
  /**
   * One line, for the user, on what this node was asked to do. Absent on a run that predates the field.
   */
  node_summary?: string;
  /**
   * What this node was handed, per key: a literal string, {file: path}, or {node: id}. The other half of prompt_template -- a template's {{ inputs.k }} does not say where k came from.
   */
  inputs?: {
    [k: string]: JsonValue;
  };
}
/**
 * One run rebuilt from its run dir. 'finalized' is false while it is still executing, in which case per-node state came from the instance registry overlay rather than a manifest.
 */
export interface DagRunSnapshot {
  run_id: string;
  dir: string;
  finalized: boolean;
  /**
   * What the whole graph was dispatched for, in one line. Absent for a run written before the field existed. Per-node the equivalent is DagSnapshotNode.node_summary.
   */
  task_summary?: string;
  files: DagSnapshotNode[];
  terminal_outputs?: {
    node: string;
    text: string;
  }[];
  summary: {
    total?: number;
    completed?: number;
    failed?: number;
    skipped?: number;
    cancelled?: number;
  };
}
/**
 * One node's rendered prompt and the head of its output. Either text is absent when its file does not exist: a node that never ran has no prompt, a failed one no output.
 */
export interface DagNodeDetail {
  run_id: string;
  node: string;
  prompt?: string;
  prompt_file?: string;
  output?: string;
  output_file?: string;
  output_chars: number;
  output_truncated: boolean;
  /**
   * Per-node status reported by the DAG runner. 'interrupted' never comes off the wire -- it is what a client infers for a node still called running when the run stopped reporting. 'exception' is a node that finished without accomplishing its task and is waiting on a decision; it is not terminal.
   */
  status?: 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'cancelled' | 'exception';
  /**
   * Why the node failed. A failed node has no output, so without this it reads as unanswered.
   */
  error?: string;
  messages?: TranscriptMessage[];
}
/**
 * A run_subagent_dag call accepted a graph. Carries the whole topology so a client can lay the graph out before any node reports.
 */
export interface DagRunStartedEvent {
  type: 'dag.run_started';
  payload: {
    run_id: string;
    /**
     * The call this run belongs to. Absent on hosts that do not correlate progress with a tool row.
     */
    tool_call_id?: string;
    /**
     * What the whole graph was dispatched for, in one line. Absent for a run started before the field existed. Per-node the equivalent is the node's node_summary.
     */
    task_summary?: string;
    nodes: {
      id: string;
      subagent: string;
      depends_on: string[];
      /**
       * Shared stateful handle. Nodes naming the same one run sequentially.
       */
      instance?: string;
      /**
       * One line, for the user, on what this node was asked to do. Absent on a run that predates the field.
       */
      node_summary?: string;
    }[];
  };
}
/**
 * One node changed state.
 */
export interface DagNodeUpdatedEvent {
  type: 'dag.node_updated';
  payload: {
    run_id: string;
    tool_call_id?: string;
    node: string;
    status: DagNodeStatus;
    started_at?: number;
    ended_at?: number;
  };
}
/**
 * A replan decision started a successor run for a node that could not proceed; replan_run_id names the new run.
 */
export interface DagRunReplannedEvent {
  type: 'dag.run_replanned';
  payload: {
    run_id: string;
    tool_call_id?: string;
    replan_run_id: string;
    from_node: string;
    reason: string;
  };
}
/**
 * A running node has shown no sign of life for quiet_ms. Information only: the node is still running and nothing about the run changed.
 */
export interface DagNodeStalledEvent {
  type: 'dag.node_stalled';
  payload: {
    run_id: string;
    tool_call_id?: string;
    node: string;
    quiet_ms: number;
  };
}
/**
 * The run finished. Terminal node output text is deliberately absent -- it is already in the tool result.
 */
export interface DagRunCompletedEvent {
  type: 'dag.run_completed';
  payload: {
    run_id: string;
    tool_call_id?: string;
    dir: string;
    summary: {
      total?: number;
      completed?: number;
      failed?: number;
      skipped?: number;
      cancelled?: number;
    };
    files: {
      node: string;
      status: DagNodeStatus;
      output_file?: string;
      error?: string;
    }[];
  };
}
/**
 * One file the agent produced as part of its reply, by local path.
 */
export interface MediaItem {
  /**
   * Absolute path of the file on the machine the agent runs on.
   */
  path: string;
  /**
   * MIME type as declared by the emit site. Today every producer declares application/octet-stream, so a client that needs the real type should sniff the extension rather than trust this.
   */
  mime: string;
  /**
   * Coarse media class; "file" is the only value emitted today.
   */
  kind: string;
}
export interface MediaEvent {
  type: 'media';
  payload: {
    /**
     * The files, in the order the turn produced them. Never empty: an event with nothing to deliver is not emitted.
     */
    items: MediaItem[];
  };
}
/**
 * One base as the list view needs it.
 *
 * ``embedding_model`` and ``dimensions`` are the base's own, recorded when it
 * was created rather than read from today's config -- a base outlives a change
 * to what the operator has configured, and the page has to be able to show the
 * mismatch.
 */
export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embedding_model: string;
  dimensions: number;
  created_at: string;
  updated_at: string;
  documents: number;
}
/**
 * One uploaded document and where its indexing got to.
 *
 * ``error`` is empty unless ``status`` is ``failed``; a row carries the reason
 * with it so a reader does not have to go looking for why nothing is
 * searchable.
 */
export interface KnowledgeDocument {
  id: string;
  base_id: string;
  source: string;
  media_type: string;
  size: number;
  status: string;
  chunk_count: number;
  error: string;
  created_at: string;
  updated_at: string;
}
/**
 * One search hit. ``score`` is a similarity, so higher is nearer -- the
 * direction every caller already reads.
 */
export interface KnowledgeHit {
  score: number;
  document_id: string;
  text: string;
}
/**
 * Just enough of one step to draw the graph: which step it is, and what it
 * waits for. The library page draws a concept diagram per card, and shipping
 * the prompts and per-node config the detail view needs would be the whole
 * library on page open.
 */
export interface PlaybookNodeShape {
  id: string;
  depends_on: string[];
}
/**
 * One playbook as the library list needs it. ``error`` is empty unless the
 * file would not parse, in which case it carries the reason and ``nodes`` is
 * empty -- one unreadable file in a directory of user-edited text must not
 * take the page down with it. ``disabled`` lives in config rather than in the
 * file, because the file is the distribution unit and the switch is local to
 * this machine.
 */
export interface PlaybookRow {
  name: string;
  description: string;
  task_summary: string;
  mode: 'dag' | 'prompt';
  confirm: boolean;
  origin: string;
  disabled: boolean;
  nodes: PlaybookNodeShape[];
  error: string;
}
/**
 * One runtime input. ``description`` is the sentence the caller is asked when
 * the value is missing, so a form built from this uses it as the label.
 */
export interface PlaybookParam {
  type: string;
  required: boolean;
  default?: JsonValue;
  enum?: string[];
  description: string;
}
/**
 * One step, whole. ``subagent`` / ``node_summary`` / ``prompt_template`` may
 * be empty: those three are the fields an author may leave blank for the
 * caller to fill at run time. ``skills`` / ``mcps`` are three-state -- null
 * means the author said nothing, ``[]`` means the author wrote an empty list,
 * and a list names what to consider.
 */
export interface PlaybookNode {
  id: string;
  subagent: string;
  node_summary: string;
  prompt_template: string;
  depends_on: string[];
  skills?: string[];
  mcps?: string[];
  instance: string;
  inputs: {
    [k: string]: JsonValue;
  };
}
/**
 * One whole playbook: its identity, its runtime inputs, and either the graph
 * (``mode: dag``) or the assembly guidance a model turns into one
 * (``mode: prompt``). ``path`` is the file this was read from.
 *
 * ``version`` is the spec format version the file declares, not a revision of
 * the playbook's content.
 */
export interface PlaybookDetail {
  name: string;
  description: string;
  task_summary: string;
  version: number;
  mode: 'dag' | 'prompt';
  confirm: boolean;
  origin: string;
  disabled: boolean;
  path: string;
  keywords: string[];
  params: {
    [k: string]: PlaybookParam;
  };
  nodes: PlaybookNode[];
  prompts: string;
  mcp_servers?: {
    [k: string]: PlaybookMcpServer;
  };
}
/**
 * One MCP server the playbook itself carries, as the file declares it. Carries every field the runtime reads to decide what the server is and whether it runs. `env` and `headers` are declarations rather than resolved values: a carried server references a credential through `{{ params.X }}` and the run supplies it, so nothing here is ever a secret's value, and `has_oauth_config` says only whether the file declares OAuth endpoints, never what they are.
 */
export interface PlaybookMcpServer {
  type?: 'stdio' | 'sse' | 'streamableHttp';
  command?: string;
  args?: string[];
  url?: string;
  env?: {
    [k: string]: string;
  };
  headers?: {
    [k: string]: string;
  };
  tool_timeout?: number;
  enabled?: boolean;
  auth?: 'none' | 'apikey' | 'oauth';
  has_oauth_config?: boolean;
}
/**
 * One `secret` param of a playbook and whether this machine holds a value for it. Never the value.
 */
export interface PlaybookCredentialParam {
  name: string;
  set: boolean;
  description: string;
}
/**
 * One server the playbook carries, as the credentials tab needs it: its auth kind, whether this machine holds OAuth tokens for it under the playbook's scope, and whether the same name exists among the host's own servers (the carried definition wins for this playbook's runs).
 */
export interface PlaybookCredentialServer {
  name: string;
  auth: 'none' | 'apikey' | 'oauth';
  enabled: boolean;
  authorized: boolean;
  shadows_host: boolean;
}
export interface OkResult {
  ok: boolean;
}
export interface SessionListParams {
  /**
   * Max sessions to return.
   */
  limit?: number;
  /**
   * Session channels to include; defaults to tui.
   */
  channels?: string[];
}
export interface SessionListResult {
  sessions: SessionListItem[];
}
export interface SessionGetParams {
  session_key: string;
}
export interface SessionGetResult {
  session: SessionInfo;
}
export interface SessionCreateParams {
  /**
   * Terminal width the client is drawing at.
   */
  cols?: number;
  /**
   * Accepted and ignored; clients set titles via session.title.
   */
  title?: string;
  /**
   * Absolute directory this session's turns run in, persisted as the session's workdir override. How a client attached to a shared gateway keeps its launch directory.
   */
  workdir?: string;
}
/**
 * The key is minted lazily -- no file is written until the first save.
 */
export interface SessionCreateResult {
  session_id: string;
  info: SessionInitInfo;
}
export interface SessionResumeParams {
  /**
   * An unknown key falls back to a freshly minted one.
   */
  session_id?: string;
  cols?: number;
}
export interface SessionResumeResult {
  session_id: string;
  info: SessionInitInfo;
  /**
   * Every stored message, not a sliced history: N stored is N on the wire.
   */
  messages: TranscriptMessage[];
}
export interface SessionDeleteParams {
  /**
   * Full session_key as sent by the UI.
   */
  session_id: string;
}
export interface SessionDeleteResult {
  /**
   * The session_id that was deleted (matches the request param); null when no such session file existed.
   */
  deleted?: string;
}
export interface SessionMostRecentParams {}
export interface SessionMostRecentResult {
  /**
   * Full tui:<chat_id> key; absent/null when no sessions exist.
   */
  session_id?: string;
  source?: string;
  started_at?: number;
  title?: string;
}
export interface SessionTitleParams {
  /**
   * Full session_key.
   */
  session_id: string;
  /**
   * When present, set as the new title; when absent, return the current title.
   */
  title?: string;
}
export interface SessionTitleResult {
  title?: string;
  session_key: string;
  /**
   * True when the title is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
export interface SessionPinParams {
  /**
   * Full session_key.
   */
  session_id: string;
  /**
   * True pins the session to the top of the picker; False unpins.
   */
  pinned: boolean;
}
export interface SessionPinResult {
  pinned: boolean;
  session_key: string;
  /**
   * True when the flag is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
export interface SessionArchiveParams {
  /**
   * Full session_key.
   */
  session_id: string;
  /**
   * True hides the session from session.list; False restores it.
   */
  archived: boolean;
}
export interface SessionArchiveResult {
  archived: boolean;
  session_key: string;
  /**
   * True when the flag is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
export interface SessionClearParams {
  /**
   * Full session_key to clear.
   */
  session_id: string;
}
export interface SessionClearResult {
  /**
   * The same session_key (no new id minted).
   */
  session_id: string;
  /**
   * True when the in-place wipe ran.
   */
  cleared: boolean;
}
export interface SessionUndoParams {
  /**
   * Full session_key to undo.
   */
  session_id: string;
  /**
   * Trailing turns to drop (role==user boundary).
   */
  n?: number;
}
export interface SessionUndoResult {
  /**
   * Messages dropped (0 = nothing to undo).
   */
  removed: number;
}
export interface SessionExportParams {
  /**
   * Session id / prefix / full key to export; current session when omitted.
   */
  session_id?: string;
}
export interface SessionExportResult {
  /**
   * True when a Markdown file was written.
   */
  exported: boolean;
  /**
   * Absolute path of the written file, or null on failure.
   */
  path?: string;
  /**
   * Failure reason when not exported: not_found | ambiguous | write_failed.
   */
  reason?: string;
  /**
   * Candidate full keys when reason is ambiguous.
   */
  candidates?: string[];
}
export interface SessionHistoryParams {
  session_key: string;
  /**
   * Maximum number of messages to return; default 500 to match Session.get_history.
   */
  max_messages?: number;
  /**
   * Return messages with index < before_index. Used for pagination.
   */
  before_index?: number;
}
export interface SessionHistoryResult {
  messages: SessionMessage[];
  total: number;
}
export interface TurnSendParams {
  session_key: string;
  content: string;
  channel?: string;
  chat_id?: string;
  sender_id?: string;
  /**
   * @maxItems 64
   */
  media?: string[];
  target?: DirectTarget;
  busy?: 'inject';
}
export interface TurnSendResult {
  turn_id: string;
  accepted: boolean;
  /**
   * Whether a session-naming call was started for this turn. A client holding a placeholder for the name can settle it on false rather than wait out its grace period. Not quite the same as 'no session.titled is coming': a send that arrives while an earlier namer for the same session is still running is also declined, and that one's title may still land -- it overwrites, which is why the weaker guarantee is enough.
   */
  naming: boolean;
}
export interface TurnSubscribeParams {
  session_key: string;
}
export interface TurnSubscribeResult {
  subscription_id: string;
}
export interface TurnUnsubscribeParams {
  subscription_id: string;
}
export interface TurnUnsubscribeResult {
  unsubscribed: boolean;
}
export interface TurnCancelParams {
  session_key: string;
  target?: DirectTarget;
}
export interface TurnCancelResult {
  cancelled: boolean;
}
export interface McpListParams {}
export interface McpListResult {
  servers: McpServerInfo[];
}
export interface McpTestParams {
  server_name: string;
}
export interface McpTestResult {
  ok: boolean;
  latency_ms: number;
  error?: string;
}
export interface McpToolsParams {
  server_name: string;
}
export interface McpToolsResult {
  tools: McpToolInfo[];
}
export interface SkillListParams {
  /**
   * Filter by skill source; default 'all'.
   */
  source?: 'local' | 'remote' | 'all';
}
export interface SkillListResult {
  skills: SkillInfo[];
}
export interface SkillPinParams {
  skill_name: string;
}
export interface SkillPinResult {
  pinned: boolean;
}
export interface SkillUnpinParams {
  skill_name: string;
}
export interface SkillUnpinResult {
  unpinned: boolean;
}
export interface ModelOptionsParams {
  session_id?: string;
}
export interface ModelOptionsResult {
  model: string;
  provider: string;
  providers: ModelOptionProvider[];
}
export interface ModelSetProtocolParams {
  slug: string;
  model: string;
  protocol: 'auto' | 'chat' | 'responses' | 'anthropic';
}
export interface ModelSetProtocolResult {
  provider: ModelOptionProvider;
}
export interface ModelSaveKeyParams {
  slug: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}
export interface ModelSaveKeyResult {
  provider: ModelOptionProvider;
}
export interface ModelDisconnectParams {
  slug: string;
  session_id?: string;
}
export interface ModelDisconnectResult {
  disconnected: boolean;
}
export interface ModelFetchModelsParams {
  slug: string;
}
export interface ModelFetchModelsResult {
  models: ModelCandidate[];
  /**
   * `ok` when the vendor answered, otherwise why it did not (`not_configured`, `unauthorized`, `network_error`, `no_probe_endpoint`, `http_NNN`). The models are the bundled catalogue unioned with whatever the vendor named, so a failure to reach it costs currency, not the list.
   */
  status: string;
  error?: string;
}
export interface ModelAddModelParams {
  slug: string;
  model: string;
  label?: string;
  capabilities?: string[];
  input_modalities?: string[];
  output_modalities?: string[];
  session_id?: string;
}
export interface ModelAddModelResult {
  provider: ModelOptionProvider;
}
export interface ModelRemoveModelParams {
  slug: string;
  model: string;
  session_id?: string;
}
export interface ModelRemoveModelResult {
  provider: ModelOptionProvider;
}
export interface ModelEndpointsParams {
  slug: string;
  session_id?: string;
}
export interface ModelEndpointsResult {
  endpoints: ProviderEndpointInfo[];
}
export interface ModelAddEndpointParams {
  slug: string;
  label: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}
export interface ModelAddEndpointResult {
  endpoints: ProviderEndpointInfo[];
}
export interface ModelRemoveEndpointParams {
  slug: string;
  label: string;
  session_id?: string;
}
export interface ModelRemoveEndpointResult {
  endpoints: ProviderEndpointInfo[];
}
export interface ConfigGetParams {
  /**
   * If omitted, return all whitelisted fields. Unknown keys are silently dropped.
   */
  keys?: string[];
  session_id?: string;
}
export interface ConfigGetResult {
  config: {
    [k: string]: JsonValue;
  };
}
export interface ConfigSetParams {
  key: string;
  value: JsonValue;
  session_id?: string;
  provider?: string;
  scope?: 'session' | 'default';
}
export interface ConfigSetResult {
  applied: boolean;
  previous: JsonValue | null;
  value?: string;
  scope?: 'session' | 'default';
  session_id?: string;
  applies_to_session?: boolean;
}
export interface ConfigUnsetParams {
  key: string;
}
export interface ConfigUnsetResult {
  removed: boolean;
  previous: JsonValue | null;
  default: JsonValue | null;
}
export interface SubagentListParams {
  /**
   * Absent or unknown is an empty list, not an error.
   */
  session_id?: string;
}
export interface SubagentListResult {
  items: SubagentCall[];
}
export interface SubagentContextParams {
  id: string;
  /**
   * A call is addressed by conversation and call, never by call alone.
   */
  session_id: string;
}
export interface SubagentContextResult {
  id: string;
  messages: TranscriptMessage[];
  status?: string;
  label?: string;
  agent?: string;
  started_at?: string;
  ended_at?: string;
  /**
   * What it did between the question and the answer, in order. Empty where the transport that ran it has no per-step visibility.
   */
  tool_calls?: string[];
  /**
   * Input plus output; null when usage was not reported.
   */
  tokens?: number;
  tokens_in?: number;
  tokens_out?: number;
  /**
   * How much reasoning it emitted, where the transport reports it separately from the answer.
   */
  thought_chars?: number;
}
export interface SubagentsListParams {
  probe?: boolean;
}
export interface SubagentsListResult {
  rows: SubagentRow[];
}
export interface SubagentsAddParams {
  preset: string;
  name?: string;
  description?: string;
  api_key?: string;
  mcps?: string[];
  allow_mcp_secrets?: boolean;
  force?: boolean;
}
export interface SubagentsAddResult {
  added: boolean;
  name: string;
}
export interface SubagentsUpdateParams {
  name: string;
  new_name?: string;
  description?: string;
  api_key?: string;
  mcps?: string[];
  allow_mcp_secrets?: boolean;
}
export interface SubagentsUpdateResult {
  updated: boolean;
  name: string;
}
export interface SubagentsRemoveParams {
  name: string;
}
export interface SubagentsRemoveResult {
  removed: boolean;
}
export interface SubagentsBuildParams {
  name: string;
}
export interface SubagentsBuildResult {
  /**
   * True once the build is under way, including when one was already running.
   */
  building: boolean;
  /**
   * Why nothing new was started, or "" when this call started it.
   */
  detail: string;
}
export interface SubagentsToggleParams {
  name: string;
  enabled: boolean;
  force?: boolean;
}
export interface SubagentsToggleResult {
  enabled: boolean;
}
export interface SubagentsProbeParams {}
export interface SubagentsProbeResult {
  rows: SubagentRow[];
}
export interface SubagentsTestParams {
  name: string;
  source?: 'config' | 'preset';
}
export interface SubagentsTestResult {
  ok: boolean;
  detail: string;
  elapsed_ms: number;
  reply?: string;
  cancelled?: boolean;
}
export interface SubagentsTestCancelParams {
  name: string;
}
export interface SubagentsTestCancelResult {
  cancelled: boolean;
}
export interface SubagentsInstancesParams {
  session_key: string;
}
export interface SubagentsInstancesResult {
  instances: InstanceRow[];
  /**
   * Direct-chat turns, and instances the user created, not yet reported to the main agent. Display only.
   */
  pending_handoff_count: number;
}
export interface SubagentsInstanceCreateParams {
  session_key: string;
  /**
   * Which sub-agent to instantiate. Must be enabled and stateful: a direct chat is a continuation, and against a stateless agent every turn would start over.
   */
  agent: string;
}
export interface SubagentsInstanceCreateResult {
  instance: InstanceRow;
}
export interface SubagentsInstanceHistoryParams {
  session_key: string;
  agent: string;
  handle: string;
}
export interface SubagentsInstanceHistoryResult {
  turns: DirectTurn[];
}
export interface SubagentsInstanceForgetParams {
  session_key: string;
  agent: string;
  handle: string;
}
export interface SubagentsInstanceForgetResult {
  removed: boolean;
}
export interface SubagentsInstanceSteerParams {
  session_key: string;
  agent: string;
  handle: string;
  text: string;
}
export interface SubagentsInstanceSteerResult {
  status: 'injected' | 'no_turn' | 'unsupported';
}
export interface SubagentsInstanceSetModeParams {
  session_key: string;
  agent: string;
  handle: string;
  mode?: string;
  /**
   * Drop this instance's override, after which the session's tier is what the next dispatch runs at. mode wins when both are given: naming one is a statement, clearing is the absence of one. session.set_mode resolves the pair the other way, because there clear selects the configured default.
   */
  clear?: boolean;
}
export interface SubagentsInstanceSetModeResult {
  /**
   * This instance's own override, or null when it has none. Null is the ordinary answer to a read with no override set and to every clear, not an error.
   */
  mode?: string | null;
  /**
   * What a dispatch runs at when there is no override: this session's tier, clamped to what the agent offers. Null when nothing is inherited and the agent's own default is what runs.
   */
  inherited?: string | null;
  availableModes?: {
    id: string;
    name?: string;
    description?: string;
  }[];
}
export interface SessionSetModeParams {
  session_key: string;
  mode?: string;
  /**
   * Drop the session override and go back to the configured default. Wins over mode when both are given, the opposite of subagents.instance.set_mode -- these are different operations: this one selects a tier, that one removes an override.
   */
  clear?: boolean;
}
export interface SessionSetModeResult {
  /**
   * The tier now in force.
   */
  mode?: string | null;
  availableModes?: {
    id: string;
    name?: string;
    description?: string;
  }[];
}
export interface SystemHelloParams {
  client_version: string;
  client_capabilities?: string[];
  /**
   * Which front end this connection is ('tui', 'page', 'shell'). Recorded per connection for tracing only; session keys and channels are unaffected.
   */
  surface?: string;
}
export interface SystemHelloResult {
  server_version: string;
  server_capabilities: string[];
  session: {
    /**
     * The channel this dispatcher's turns run on; the terminal and the served page share one.
     */
    default_channel: string;
    default_session_key: string;
  };
  /**
   * The gateway host's OS family, so a client can word host-side actions (Finder vs Explorer).
   */
  platform?: 'mac' | 'windows' | 'linux';
}
export interface SystemPingParams {}
export interface SystemPingResult {
  pong: true;
  server_time_ms: number;
}
export interface SystemVersionParams {
  /**
   * Fetch the latest release now instead of answering from the daily cache.
   */
  check?: boolean;
}
export interface SystemVersionResult {
  server_version: string;
  /**
   * OpenRPC info.version mirrored back to client.
   */
  schema_version: string;
  raven_version: string;
}
export interface CliDispatchParams {
  /**
   * Pre-tokenized argv (TUI side has already shlex-split).
   */
  argv: string[];
  /**
   * Ink container width in cells; required for Rich Console wrapping.
   */
  width: number;
  /**
   * Override the default 30s timeout for long-running commands.
   */
  timeout_s?: number;
}
export interface CliDispatchResult {
  /**
   * Rich-rendered output with ANSI SGR sequences.
   */
  stdout: string;
  /**
   * Error / warning output with ANSI SGR sequences.
   */
  stderr: string;
  /**
   * CLI command exit code; 0 = success.
   */
  exit_code: number;
  /**
   * Only present for timeout / not-dispatch-compatible cases (mirrors a JSON-RPC error code).
   */
  error_code?: number;
}
export interface SetupStatusParams {}
export interface SetupStatusResult {
  provider_configured: boolean;
}
export interface ReloadMcpParams {
  session_id?: string;
  confirm?: boolean;
}
export interface ReloadMcpResult {
  ok: boolean;
  status: 'reloaded' | 'noop' | 'confirm_required';
  message: string;
  reloaded: number;
  tools_changed: boolean;
}
export interface CommandsCatalogParams {}
export interface CommandsCatalogResult {
  /**
   * alias (with leading /) -> canonical mapping. v0.1: alias = canonical 1:1 (no short aliases generated; TS-side prefix-1-match handles partials). Group + subcommand are space-separated (e.g. '/channels status'), not slash- or dot-separated.
   */
  canon: {
    [k: string]: string;
  };
  /**
   * Ordered (alias, canonical) tuples. TS-side createGatewayEventHandler.ts:198 gates on non-empty pairs; empty pairs degrades to slash.exec direct dispatch.
   */
  pairs: [unknown, unknown][];
  /**
   * group -> [subcommand]. Blacklisted entries (e.g. channels login) and agent-REPL are filtered out.
   */
  sub: {
    [k: string]: string[];
  };
  /**
   * Ordered category list: '(top-level)' first then alphabetical group names. Fully filtered groups (e.g. tui) do not appear.
   */
  categories: string[];
  /**
   * Total skill count from skill_forge store SQL count; 0 if DB missing (warning field then populated).
   */
  skill_count: number;
  /**
   * Optional warning pushed to TUI activity strip (e.g. 'skill store not initialized').
   */
  warning?: string;
}
export interface VoiceToggleParams {
  action?: string;
}
export interface VoiceToggleResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface BrowserManageParams {
  action?: string;
  url?: string;
}
export interface BrowserManageResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SpawnTreeSaveParams {
  name?: string;
}
export interface SpawnTreeSaveResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SpawnTreeListParams {}
export interface SpawnTreeListResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SpawnTreeLoadParams {
  name?: string;
}
export interface SpawnTreeLoadResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface ProcessStopParams {}
export interface ProcessStopResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface RollbackListParams {}
export interface RollbackListResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface RollbackDiffParams {
  id?: string;
}
export interface RollbackDiffResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface RollbackRestoreParams {
  id?: string;
}
export interface RollbackRestoreResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface ToolsConfigureParams {}
export interface ToolsConfigureResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface DagGetParams {
  run_id: string;
  session_key?: string;
}
export interface DagGetResult {
  run: DagRunSnapshot;
}
export interface DagNodeParams {
  run_id: string;
  node: string;
  max_output_chars?: number;
  session_key?: string;
}
export interface DagNodeResult {
  node: DagNodeDetail;
}
export interface PlughubSearchParams {
  q?: string;
  category?: string;
}
export interface PlughubSearchResult {
  items: PlughubCatalogItem[];
  categories: string[];
}
export interface PlughubDetailParams {
  id: string;
}
export interface PlughubDetailResult {
  item: {
    [k: string]: JsonValue;
  };
  installed: boolean;
}
export interface PlugInstallParams {
  id: string;
  /**
   * Values for the entry's auth.fields, by key.
   */
  form?: {
    [k: string]: string;
  };
}
export interface PlugInstallResult {
  /**
   * False while an auth flow is still open; see `pending`.
   */
  installed: boolean;
  /**
   * True when the browser round-trip has not settled inside the connect window.
   */
  pending: boolean;
  ledger: PlugLedger;
  mcp?: McpSnapshot1;
}
/**
 * One server's live connection state, as `MCPConnectionManager` reports it.
 *
 * Every mutating `plug.*` call answers with this, and the gateway broadcasts the
 * same shape as an `mcp.status` notification, so a client renders one state
 * machine rather than two.
 */
export interface McpSnapshot1 {
  name: string;
  /**
   * stdio | sse | streamableHttp, or 'unknown'.
   */
  transport: string;
  state: 'disconnected' | 'connecting' | 'connected' | 'auth_required' | 'error';
  connected: boolean;
  tool_count: number;
  error?: string;
  enabled: boolean;
  /**
   * The authorization URL this server is parked on, when it is. Carried on the pull because the `oauth.pending` notification that also carries it is dropped when no client is attached, which is every connect started at assembly time.
   */
  auth_url?: string;
}
export interface PlugRemoveParams {
  name: string;
}
export interface PlugRemoveResult {
  removed: boolean;
  /**
   * 'market' when a ledger drove the removal, 'manual' for a hand-written server.
   */
  origin: 'market' | 'manual';
}
export interface PlugToggleParams {
  name: string;
  enabled: boolean;
}
export interface PlugToggleResult {
  name: string;
  enabled: boolean;
  mcp?: McpSnapshot;
}
export interface PlugAuthParams {
  name: string;
}
export interface PlugAuthResult {
  name: string;
  mcp?: McpSnapshot;
}
export interface SkillhubSearchParams {
  /**
   * Natural-language search; empty browses the hub.
   */
  query?: string;
  /**
   * Category enum, e.g. DEV / TESTING / DOC-PROC.
   */
  category?: string;
  /**
   * Comma-separated tags, intersected.
   */
  tags?: string;
  min_score?: number;
  page?: number;
  limit?: number;
}
export interface SkillhubSearchResult {
  items: SkillhubItem[];
  total: number;
  page: number;
  limit: number;
  base_url: string;
}
export interface SkillhubDetailParams {
  /**
   * Hub UUID or dataset skill_id.
   */
  id: string;
}
export interface SkillhubDetailResult {
  id: string;
  skill_id: string;
  name: string;
  description: string;
  source: string;
  source_url: string;
  category: string;
  quality_score: number;
  install_count: number;
  github_star: number;
  license: string;
  tags: string[];
  installed: boolean;
  /**
   * The local directory name when installed, else empty.
   */
  installed_name: string;
  files: string[];
  skill_md: string;
  body_tokens: number;
  subscores: SkillhubSubscores;
}
export interface SkillhubInstallParams {
  id: string;
}
export interface SkillhubInstallResult {
  name: string;
  path: string;
  files: string[];
  /**
   * Members the suffix/size policy refused, so the gap is visible.
   */
  skipped: string[];
  replaced: boolean;
  size_bytes: number;
  install_count: number;
}
export interface SkillhubRemoveParams {
  /**
   * Installed skill directory name.
   */
  name: string;
}
export interface SkillhubRemoveResult {
  removed: boolean;
  name: string;
}
export interface SessionCloseParams {
  /**
   * Absent or unknown is a no-op.
   */
  session_id?: string;
}
export interface SessionCloseResult {
  ok: boolean;
}
export interface SessionBranchParams {
  session_id?: string;
  /**
   * Title for the child session.
   */
  name?: string;
}
/**
 * ``session_id`` is null when the source was unknown or empty, which the
 * caller treats as a no-op rather than an error.
 */
export interface SessionBranchResult {
  session_id?: string;
  title?: string;
  message_count?: number;
}
export interface SessionCompressParams {
  session_id: string;
  focus_topic?: string;
}
/**
 * The three redraw fields ride along only when something was archived: a
 * caller that just dropped half the transcript is looking at messages that no
 * longer exist.
 */
export interface SessionCompressResult {
  before_messages: number;
  after_messages: number;
  before_tokens: number;
  after_tokens: number;
  removed: number;
  summary: SessionCompressSummary;
  info?: SessionInitInfo;
  messages?: TranscriptMessage[];
  usage?: SessionUsage;
}
export interface SessionStatusParams {
  session_id?: string;
}
export interface SessionStatusResult {
  /**
   * Rich-rendered `raven status` output with ANSI SGR sequences.
   */
  output: string;
}
export interface ExtListParams {}
export interface ExtListResult {
  skills: ExtSkillRow[];
  plugins: ExtPluginRow[];
  tools: ExtToolRow[];
  /**
   * Live connections, plus configured servers not yet connected, reported as disconnected.
   */
  mcp: McpSnapshot[];
}
export interface CronListParams {}
export interface CronListResult {
  /**
   * Disabled jobs included.
   */
  jobs: CronJobInfo[];
}
export interface CronSaveParams {
  kind: 'at' | 'every' | 'cron';
  name: string;
  message: string;
  expr?: string;
  every_seconds?: number;
  at_iso?: string;
  tz?: string;
  /**
   * Editing an existing job: the id is kept so its run history does not orphan.
   */
  id?: string;
}
export interface CronSaveResult {
  job: CronJobInfo;
}
export interface CronDeleteParams {
  id: string;
}
export interface CronDeleteResult {
  deleted: boolean;
}
export interface CronSetEnabledParams {
  id: string;
  enabled: boolean;
}
export interface CronSetEnabledResult {
  enabled: boolean;
}
export interface CronRunNowParams {
  id: string;
}
export interface CronRunNowResult {
  ok: boolean;
}
export interface CronRunsParams {
  id: string;
}
export interface CronRunsResult {
  /**
   * Newest first, capped at 50.
   */
  runs: CronRun[];
  /**
   * The cron:<id> session the history is derived from.
   */
  session_id: string;
}
export interface SettingsGetParams {}
export interface SettingsGetResult {
  /**
   * Raw config.json with secret-looking values masked.
   */
  settings: {
    [k: string]: JsonValue;
  };
  config_path: string;
  raven_version: string;
}
export interface SettingsSetParams {
  /**
   * Dotted path; only whitelisted keys are writable through this method.
   */
  key: string;
  value: JsonValue;
}
export interface SettingsSetResult {
  applied: boolean;
  previous: JsonValue;
}
export interface SettingsUsageParams {
  /**
   * Window to scan; 30 by default, capped at 90.
   */
  days?: number;
  session_key?: string | null;
}
export interface SettingsUsageResult {
  days: number;
  llm: LlmUsage;
  tools: ToolUsage;
  session_key?: string | null;
  sessions?: string[];
  session_titles?: {
    [k: string]: string;
  };
}
export interface SettingsEverosParams {}
export interface SettingsEverosResult {
  sections: {
    [k: string]: EverosSection;
  };
  config_path: string;
}
export interface SettingsEverosSetParams {
  section: string;
  /**
   * Merged into the section; ignored when clearing.
   */
  fields?: {
    [k: string]: string;
  };
  /**
   * Drop the section; refused for llm and embedding.
   */
  clear?: boolean;
  /**
   * Take api_key and base_url from this connected provider, copied not referenced. Wins over the same keys in `fields`, which cannot carry a real key: the page only ever sees a redacted one.
   */
  borrow_from?: string;
}
export interface SettingsEverosSetResult {
  applied: boolean;
}
export interface ChannelsStatusParams {}
export interface ChannelsStatusResult {
  channels: ChannelStatusRow[];
  gateway_running: boolean;
}
export interface ChannelsConfigureParams {
  name: string;
  /**
   * Field-path -> value patch; blank strings are skipped, never written.
   */
  fields?: {
    [k: string]: JsonValue;
  };
  /**
   * Connect or disconnect the channel; omitted leaves its current state alone.
   */
  enabled?: boolean;
}
export interface ChannelsConfigureResult {
  applied: boolean;
}
export interface ChannelsQrParams {
  name: string;
}
export interface ChannelsQrResult {
  /**
   * The scan code as a PNG data URI, or null when there is none.
   */
  qr?: string;
  /**
   * The raw scan payload, sent only when the server could not rasterise it.
   */
  qr_text?: string;
  /**
   * Whether the account is paired; true ends the client's polling.
   */
  connected: boolean;
  running: boolean;
}
export interface FsListParams {
  /**
   * Root-relative; the root when omitted.
   */
  path?: string;
  /**
   * Session key whose working directory roots the listing; the policy default when omitted.
   */
  session?: string;
}
export interface FsListResult {
  /**
   * Absolute working directory the listing is rooted at.
   */
  root: string;
  path: string;
  /**
   * Directories first, dotfiles omitted, capped at 500.
   */
  entries: FsEntry[];
}
export interface FsReadParams {
  path: string;
  max_bytes?: number;
  session?: string;
}
export interface FsReadResult {
  /**
   * Decoded as UTF-8 with replacement, so binary never fails the call.
   */
  content: string;
  truncated: boolean;
  /**
   * Size on disk, which exceeds len(content) when truncated.
   */
  size: number;
}
export interface FsUploadParams {
  name: string;
  content_b64: string;
  session?: string;
}
export interface FsUploadResult {
  /**
   * Workspace-relative path to hand the agent; uploads never return bytes.
   */
  path: string;
  abs_path: string;
  size: number;
}
export interface FsRevealParams {
  /**
   * Absolute, or relative to the session's working directory.
   */
  path: string;
  session?: string;
}
export interface FsRevealResult {
  ok: true;
}
export interface FsOpenParams {
  /**
   * Absolute, or relative to the session's working directory.
   */
  path: string;
  /**
   * Which installed application to hand the file to. Absent means the host's own default. A NAME, not a path or a command line: the server rejects anything with a separator, a shell character or a leading dash, and never runs it through a shell.
   */
  app?: string;
  session?: string;
}
export interface FsOpenResult {
  ok: true;
  /**
   * The application asked for, echoed back; absent when the host default was used.
   */
  app?: string;
}
export interface DeliverablesListParams {
  /**
   * Full session_key. An empty or unknown key answers with an empty list.
   */
  session_key: string;
}
export interface DeliverablesListResult {
  /**
   * Oldest first, one entry per delivered path.
   */
  files: {
    path: string;
    name: string;
    /**
     * What the agent called the file; empty when it named none.
     */
    title?: string;
    description?: string;
    size: number;
    media_type: string;
    /**
     * Token URL on the gateway; never a path.
     */
    download_path: string;
    /**
     * ISO-8601, when the file was first delivered.
     */
    created_at: string;
    /**
     * The registry has it, the filesystem no longer does.
     */
    missing: boolean;
  }[];
}
export interface MemoryStatsParams {}
/**
 * ``ok`` is false when a kind could not be counted; the counts stay zero
 * rather than the call failing, so the page opens with EverOS down.
 */
export interface MemoryStatsResult {
  ok: boolean;
  base_url: string;
  episodes: number;
  profiles: number;
  agent_cases: number;
  agent_skills: number;
}
export interface MemoryListParams {
  kind: 'episode' | 'profile' | 'agent_case' | 'agent_skill';
  page?: number;
  /**
   * Capped at 100.
   */
  page_size?: number;
  /**
   * Non-empty switches to search, which returns one page.
   */
  q?: string;
}
export interface MemoryListResult {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
}
export interface MemoryDeleteParams {
  kind: 'episode' | 'profile' | 'agent_case' | 'agent_skill';
  id: string;
}
export interface MemoryDeleteResult {
  ok: boolean;
  /**
   * Deleting an episode also drops its derived facts and foresight.
   */
  removed: number;
}
export interface PlaybooksListParams {}
export interface PlaybooksListResult {
  playbooks: PlaybookRow[];
}
export interface PlaybooksGetParams {
  name: string;
}
export interface PlaybooksGetResult {
  playbook: PlaybookDetail;
}
export interface PlaybooksCredentialsGetParams {
  name: string;
}
export interface PlaybooksCredentialsGetResult {
  params: PlaybookCredentialParam[];
  servers: PlaybookCredentialServer[];
}
export interface PlaybooksCredentialsSetParams {
  name: string;
  param: string;
  value: string;
}
export interface PlaybooksCredentialsSetResult {
  ok: boolean;
}
export interface PlaybooksCredentialsClearParams {
  name: string;
  param: string;
}
export interface PlaybooksCredentialsClearResult {
  ok: boolean;
}
export interface PlaybooksOauthAuthorizeParams {
  name: string;
  server: string;
}
export interface PlaybooksOauthAuthorizeResult {
  server: string;
  state: string;
  auth_url?: string | null;
  error?: string | null;
}
export interface PlaybooksOauthClearParams {
  name: string;
  server: string;
}
export interface PlaybooksOauthClearResult {
  ok: boolean;
}
export interface ApprovalRespondParams {
  approval_id: string;
  /**
   * allow | deny | deny_stop.
   */
  choice: string;
  /**
   * Optional sentence attached to a refusal, relayed to the model.
   */
  feedback?: string;
  session_id?: string;
  /**
   * Compatibility spelling of session_id.
   */
  conversation_id?: string;
}
export interface ApprovalRespondResult {
  /**
   * False for an unknown, expired or mis-bound request; the caller fails closed.
   */
  ok: boolean;
}
export interface ClarifyRespondParams {
  answer: string;
  request_id?: string;
  conversation_id?: string;
}
export interface ClarifyRespondResult {
  ok: boolean;
}
export interface ConfirmRespondParams {
  request_id: string;
  answer: boolean;
}
export interface ConfirmRespondResult {
  ok: boolean;
}
export interface SlashExecParams {
  /**
   * The slash text without its leading slash; shlex-split into argv.
   */
  command: string;
  session_id?: string;
}
/**
 * Never an error frame: an unknown verb, a blacklisted one, a timeout and a
 * non-zero exit all arrive here, because the client's error branch falls
 * through to a method that does not exist.
 */
export interface SlashExecResult {
  output: string;
  warning?: string;
}
export interface CompleteSlashParams {
  word?: string;
  session_id?: string;
}
/**
 * The provider is a no-op that exists to stop the client's
 * completion-unavailable toast, so ``items`` is always empty and its element
 * type is whatever a real provider would later return.
 */
export interface CompleteSlashResult {
  items: JsonValue[];
  replace_from: number;
}
export interface CompletePathParams {
  word?: string;
}
export interface CompletePathResult {
  items: JsonValue[];
}
export interface TerminalResizeParams {
  cols?: number;
  rows?: number;
  /**
   * Sent by the client; the handler does not read it.
   */
  session_id?: string;
}
export interface TerminalResizeResult {
  ok: boolean;
}
export interface SystemUpgradeParams {}
/**
 * Returned once the detached helper owns the install. The shutdown is
 * scheduled a beat later so this reply reaches the client first.
 */
export interface SystemUpgradeResult {
  status: string;
  from_version: string;
  to_version: string;
  /**
   * Whether the helper will start `raven serve` again on the same port.
   */
  relaunch: boolean;
}
export interface VoiceRecordParams {
  action?: string;
  session_id?: string;
}
export interface VoiceRecordResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SessionSaveParams {
  session_id?: string;
}
export interface SessionSaveResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SessionSteerParams {
  session_id?: string;
  text?: string;
}
export interface SessionSteerResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SessionUsageParams {
  session_id?: string;
}
export interface SessionUsageResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SkillsReloadParams {}
export interface SkillsReloadResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface ReloadEnvParams {}
export interface ReloadEnvResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SudoRespondParams {
  request_id?: string;
  password?: string;
}
export interface SudoRespondResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface SecretRespondParams {
  request_id?: string;
  value?: string;
}
export interface SecretRespondResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface ImageAttachParams {
  path?: string;
  session_id?: string;
}
export interface ImageAttachResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface PromptSubmitParams {
  session_id?: string;
  text?: string;
}
export interface PromptSubmitResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface PromptBackgroundParams {
  session_id?: string;
  text?: string;
}
export interface PromptBackgroundResult {
  /**
   * Human-readable explanation of why this method is not supported in v0.1.
   */
  error: string;
  /**
   * Optional hint to the user (e.g., 'Press Ctrl+C').
   */
  hint?: string;
}
export interface BrowserStateParams {}
export interface BrowserStateResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
}
export interface BrowserOpenParams {
  /**
   * http or https only; a bare host is completed to https. Other schemes are refused.
   */
  url?: string;
  /**
   * back | forward | reload | stop, instead of a url.
   */
  action?: string;
}
export interface BrowserOpenResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
}
export interface BrowserTabsParams {
  /**
   * list (default) | new | activate | close.
   */
  action?: string;
  index?: number;
  /**
   * Navigate a freshly opened tab in the same call.
   */
  url?: string;
}
export interface BrowserTabsResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
  tabs?: BrowserTab[];
}
export interface BrowserFrameParams {
  /**
   * JPEG quality; 55 by default.
   */
  quality?: number;
}
export interface BrowserFrameResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
  /**
   * Base64 JPEG of the page, absent when nothing is started.
   */
  jpeg?: string;
}
export interface BrowserReadParams {}
export interface BrowserReadResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
  /**
   * Page text, capped.
   */
  text?: string;
  refs?: BrowserRef[];
  console?: BrowserConsoleLine[];
}
export interface BrowserInputParams {
  /**
   * click | text | key | scroll, or the raw stream kinds move/down/up/wheel/keydown/keyup.
   */
  kind: string;
  ref?: string;
  x?: number;
  y?: number;
  dx?: number;
  dy?: number;
  button?: string;
  count?: number;
  key?: string;
  text?: string;
}
export interface BrowserInputResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
}
export interface BrowserModeParams {
  /**
   * True pops the page into a real window; false folds it back.
   */
  headful: boolean;
}
export interface BrowserModeResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
}
export interface BrowserCloseParams {}
export interface BrowserCloseResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
}
export interface BrowserWatchParams {
  on: boolean;
  width?: number;
  height?: number;
  quality?: number;
}
export interface BrowserWatchResult {
  ok: boolean;
  url?: string;
  title?: string;
  started?: boolean;
  headful?: boolean;
  /**
   * False when playwright or its Chromium is missing.
   */
  available?: boolean;
  /**
   * Why a browser cannot be started, when it cannot.
   */
  reason?: string;
  loading?: boolean;
  can_back?: boolean;
  can_forward?: boolean;
  tab_count?: number;
  /**
   * A refused navigation or a failed action; the call still answers rather than raising.
   */
  error?: string;
  watching?: boolean;
  vw?: number;
  vh?: number;
}
export interface KnowledgeStatusParams {}
/**
 * Whether a base can be created, and with which model.
 *
 * ``model`` is empty exactly when ``configured`` is false. No credential is
 * reported: the key's presence *is* the flag.
 */
export interface KnowledgeStatusResult {
  configured: boolean;
  model: string;
}
export interface KnowledgeBasesListParams {}
export interface KnowledgeBasesListResult {
  bases: KnowledgeBase[];
}
export interface KnowledgeBasesCreateParams {
  name: string;
  description?: string;
}
export interface KnowledgeBasesCreateResult {
  base: KnowledgeBase;
}
export interface KnowledgeBasesRenameParams {
  base_id: string;
  name?: string;
  description?: string;
}
export interface KnowledgeBasesRenameResult {
  base: KnowledgeBase;
}
export interface KnowledgeBasesDeleteParams {
  base_id: string;
}
/**
 * False for a base that was not there: a second delete from a stale page
 * reached the outcome its caller wanted.
 */
export interface KnowledgeBasesDeleteResult {
  removed: boolean;
}
export interface KnowledgeDocumentsListParams {
  base_id: string;
}
export interface KnowledgeDocumentsListResult {
  documents: KnowledgeDocument[];
}
export interface KnowledgeDocumentsAddParams {
  base_id: string;
  path: string;
}
export interface KnowledgeDocumentsAddResult {
  document: KnowledgeDocument;
}
export interface KnowledgeDocumentsIndexParams {
  document_id: string;
}
export interface KnowledgeDocumentsIndexResult {
  document: KnowledgeDocument;
}
export interface KnowledgeDocumentsDeleteParams {
  document_id: string;
}
export interface KnowledgeDocumentsDeleteResult {
  /**
   * False when there was no such document, which is not an error: two clicks on one row answer the same way.
   */
  removed: boolean;
}
export interface KnowledgeSearchParams {
  base_ids: string[];
  query: string;
  top_k?: number;
}
export interface KnowledgeSearchResult {
  hits: KnowledgeHit[];
}
export interface ClipboardPasteParams {}
export interface ClipboardPasteResult {
  attached: boolean;
  message?: string;
  width?: number;
  height?: number;
  token_estimate?: number;
}
export interface CommandDispatchParams {
  name: string;
  arg?: string;
}
export interface CommandDispatchResult {
  /**
   * `exec` (a shell-style command ran) or `skill` (the name resolved to a skill).
   */
  type: string;
  output?: string;
  name?: string;
  message?: string;
}
export interface DelegationStatusParams {}
export interface DelegationStatusResult {
  max_concurrent_children: number;
  max_spawn_depth: number;
  paused: boolean;
}
export interface DelegationPauseParams {
  paused: boolean;
}
export interface DelegationPauseResult {
  paused: boolean;
}
export interface InputDetectDropParams {
  text: string;
}
export interface InputDetectDropResult {
  matched: boolean;
  name?: string;
  /**
   * The resolved absolute path when matched.
   */
  text?: string;
  is_image?: boolean;
  width?: number;
  height?: number;
  token_estimate?: number;
}
export interface SessionInterruptParams {
  session_id: string;
}
export interface SessionInterruptResult {
  ok: boolean;
}
export interface ShellExecParams {
  command: string;
}
export interface ShellExecResult {
  code: number;
  stdout: string;
  stderr: string;
}
export interface SkillsManageParams {
  /**
   * One of list, inspect, search, browse, install.
   */
  action: string;
  query?: string;
  page?: number;
}
export interface SkillsManageResult {
  /**
   * `list`: names grouped by source.
   */
  skills?: {
    [k: string]: string[];
  };
  /**
   * `inspect`: one skill's metadata, {} when unknown.
   */
  info?: {
    [k: string]: JsonValue;
  };
  /**
   * `search`: matches.
   */
  results?: {
    [k: string]: JsonValue;
  }[];
  /**
   * `browse`: one page of the hub.
   */
  items?: {
    [k: string]: JsonValue;
  }[];
  page?: number;
  total?: number;
  total_pages?: number;
  /**
   * `install`.
   */
  installed?: boolean;
  name?: string;
}
export interface SubagentInterruptParams {
  subagent_id: string;
}
export interface SubagentInterruptResult {
  found: boolean;
  subagent_id: string;
}
export interface SubagentCancelSessionParams {
  session_key: string;
}
export interface SubagentCancelSessionResult {
  cancelled: number;
  session_key: string;
}
export interface SubagentCancelInstanceParams {
  session_key?: string;
  agent: string;
  handle: string;
}
export interface SubagentCancelInstanceResult {
  found: boolean;
  session_key: string;
  agent: string;
  handle: string;
}

// ---------------------------------------------------------------------------
// Method map -- generated from the contract's method list.
// ---------------------------------------------------------------------------

/** Every method the contract declares, mapped to its params and result. */
export interface RpcMethods {
  'session.list': { params: SessionListParams; result: SessionListResult };
  'session.get': { params: SessionGetParams; result: SessionGetResult };
  'session.create': { params: SessionCreateParams; result: SessionCreateResult };
  'session.resume': { params: SessionResumeParams; result: SessionResumeResult };
  'session.delete': { params: SessionDeleteParams; result: SessionDeleteResult };
  'session.most_recent': { params: SessionMostRecentParams; result: SessionMostRecentResult };
  'session.title': { params: SessionTitleParams; result: SessionTitleResult };
  'session.pin': { params: SessionPinParams; result: SessionPinResult };
  'session.archive': { params: SessionArchiveParams; result: SessionArchiveResult };
  'session.clear': { params: SessionClearParams; result: SessionClearResult };
  'session.undo': { params: SessionUndoParams; result: SessionUndoResult };
  'session.export': { params: SessionExportParams; result: SessionExportResult };
  'session.history': { params: SessionHistoryParams; result: SessionHistoryResult };
  'turn.send': { params: TurnSendParams; result: TurnSendResult };
  'turn.subscribe': { params: TurnSubscribeParams; result: TurnSubscribeResult };
  'turn.unsubscribe': { params: TurnUnsubscribeParams; result: TurnUnsubscribeResult };
  'turn.cancel': { params: TurnCancelParams; result: TurnCancelResult };
  'mcp.list': { params: McpListParams; result: McpListResult };
  'mcp.test': { params: McpTestParams; result: McpTestResult };
  'mcp.tools': { params: McpToolsParams; result: McpToolsResult };
  'skill.list': { params: SkillListParams; result: SkillListResult };
  'skill.pin': { params: SkillPinParams; result: SkillPinResult };
  'skill.unpin': { params: SkillUnpinParams; result: SkillUnpinResult };
  'model.options': { params: ModelOptionsParams; result: ModelOptionsResult };
  'model.set_protocol': { params: ModelSetProtocolParams; result: ModelSetProtocolResult };
  'model.save_key': { params: ModelSaveKeyParams; result: ModelSaveKeyResult };
  'model.disconnect': { params: ModelDisconnectParams; result: ModelDisconnectResult };
  'model.fetch_models': { params: ModelFetchModelsParams; result: ModelFetchModelsResult };
  'model.add_model': { params: ModelAddModelParams; result: ModelAddModelResult };
  'model.remove_model': { params: ModelRemoveModelParams; result: ModelRemoveModelResult };
  'model.endpoints': { params: ModelEndpointsParams; result: ModelEndpointsResult };
  'model.add_endpoint': { params: ModelAddEndpointParams; result: ModelAddEndpointResult };
  'model.remove_endpoint': { params: ModelRemoveEndpointParams; result: ModelRemoveEndpointResult };
  'config.get': { params: ConfigGetParams; result: ConfigGetResult };
  'config.set': { params: ConfigSetParams; result: ConfigSetResult };
  'config.unset': { params: ConfigUnsetParams; result: ConfigUnsetResult };
  'subagent.list': { params: SubagentListParams; result: SubagentListResult };
  'subagent.context': { params: SubagentContextParams; result: SubagentContextResult };
  'subagents.list': { params: SubagentsListParams; result: SubagentsListResult };
  'subagents.add': { params: SubagentsAddParams; result: SubagentsAddResult };
  'subagents.update': { params: SubagentsUpdateParams; result: SubagentsUpdateResult };
  'subagents.remove': { params: SubagentsRemoveParams; result: SubagentsRemoveResult };
  'subagents.build': { params: SubagentsBuildParams; result: SubagentsBuildResult };
  'subagents.toggle': { params: SubagentsToggleParams; result: SubagentsToggleResult };
  'subagents.probe': { params: SubagentsProbeParams; result: SubagentsProbeResult };
  'subagents.test': { params: SubagentsTestParams; result: SubagentsTestResult };
  'subagents.test_cancel': { params: SubagentsTestCancelParams; result: SubagentsTestCancelResult };
  'subagents.instances': { params: SubagentsInstancesParams; result: SubagentsInstancesResult };
  'subagents.instance.create': { params: SubagentsInstanceCreateParams; result: SubagentsInstanceCreateResult };
  'subagents.instance.history': { params: SubagentsInstanceHistoryParams; result: SubagentsInstanceHistoryResult };
  'subagents.instance.forget': { params: SubagentsInstanceForgetParams; result: SubagentsInstanceForgetResult };
  'subagents.instance.steer': { params: SubagentsInstanceSteerParams; result: SubagentsInstanceSteerResult };
  'subagents.instance.set_mode': { params: SubagentsInstanceSetModeParams; result: SubagentsInstanceSetModeResult };
  'session.set_mode': { params: SessionSetModeParams; result: SessionSetModeResult };
  'system.hello': { params: SystemHelloParams; result: SystemHelloResult };
  'system.ping': { params: SystemPingParams; result: SystemPingResult };
  'system.version': { params: SystemVersionParams; result: SystemVersionResult };
  'cli.dispatch': { params: CliDispatchParams; result: CliDispatchResult };
  'setup.status': { params: SetupStatusParams; result: SetupStatusResult };
  'reload.mcp': { params: ReloadMcpParams; result: ReloadMcpResult };
  'commands.catalog': { params: CommandsCatalogParams; result: CommandsCatalogResult };
  'voice.toggle': { params: VoiceToggleParams; result: VoiceToggleResult };
  'browser.manage': { params: BrowserManageParams; result: BrowserManageResult };
  'spawn_tree.save': { params: SpawnTreeSaveParams; result: SpawnTreeSaveResult };
  'spawn_tree.list': { params: SpawnTreeListParams; result: SpawnTreeListResult };
  'spawn_tree.load': { params: SpawnTreeLoadParams; result: SpawnTreeLoadResult };
  'process.stop': { params: ProcessStopParams; result: ProcessStopResult };
  'rollback.list': { params: RollbackListParams; result: RollbackListResult };
  'rollback.diff': { params: RollbackDiffParams; result: RollbackDiffResult };
  'rollback.restore': { params: RollbackRestoreParams; result: RollbackRestoreResult };
  'tools.configure': { params: ToolsConfigureParams; result: ToolsConfigureResult };
  'dag.get': { params: DagGetParams; result: DagGetResult };
  'dag.node': { params: DagNodeParams; result: DagNodeResult };
  'plughub.search': { params: PlughubSearchParams; result: PlughubSearchResult };
  'plughub.detail': { params: PlughubDetailParams; result: PlughubDetailResult };
  'plug.install': { params: PlugInstallParams; result: PlugInstallResult };
  'plug.remove': { params: PlugRemoveParams; result: PlugRemoveResult };
  'plug.toggle': { params: PlugToggleParams; result: PlugToggleResult };
  'plug.auth': { params: PlugAuthParams; result: PlugAuthResult };
  'skillhub.search': { params: SkillhubSearchParams; result: SkillhubSearchResult };
  'skillhub.detail': { params: SkillhubDetailParams; result: SkillhubDetailResult };
  'skillhub.install': { params: SkillhubInstallParams; result: SkillhubInstallResult };
  'skillhub.remove': { params: SkillhubRemoveParams; result: SkillhubRemoveResult };
  'session.close': { params: SessionCloseParams; result: SessionCloseResult };
  'session.branch': { params: SessionBranchParams; result: SessionBranchResult };
  'session.compress': { params: SessionCompressParams; result: SessionCompressResult };
  'session.status': { params: SessionStatusParams; result: SessionStatusResult };
  'ext.list': { params: ExtListParams; result: ExtListResult };
  'cron.list': { params: CronListParams; result: CronListResult };
  'cron.save': { params: CronSaveParams; result: CronSaveResult };
  'cron.delete': { params: CronDeleteParams; result: CronDeleteResult };
  'cron.set_enabled': { params: CronSetEnabledParams; result: CronSetEnabledResult };
  'cron.run_now': { params: CronRunNowParams; result: CronRunNowResult };
  'cron.runs': { params: CronRunsParams; result: CronRunsResult };
  'settings.get': { params: SettingsGetParams; result: SettingsGetResult };
  'settings.set': { params: SettingsSetParams; result: SettingsSetResult };
  'settings.usage': { params: SettingsUsageParams; result: SettingsUsageResult };
  'settings.everos': { params: SettingsEverosParams; result: SettingsEverosResult };
  'settings.everosSet': { params: SettingsEverosSetParams; result: SettingsEverosSetResult };
  'settings.everos_set': { params: SettingsEverosSetParams; result: SettingsEverosSetResult };
  'channels.status': { params: ChannelsStatusParams; result: ChannelsStatusResult };
  'channels.configure': { params: ChannelsConfigureParams; result: ChannelsConfigureResult };
  'channels.qr': { params: ChannelsQrParams; result: ChannelsQrResult };
  'fs.list': { params: FsListParams; result: FsListResult };
  'fs.read': { params: FsReadParams; result: FsReadResult };
  'fs.upload': { params: FsUploadParams; result: FsUploadResult };
  'fs.reveal': { params: FsRevealParams; result: FsRevealResult };
  'fs.open': { params: FsOpenParams; result: FsOpenResult };
  'deliverables.list': { params: DeliverablesListParams; result: DeliverablesListResult };
  'memory.stats': { params: MemoryStatsParams; result: MemoryStatsResult };
  'memory.list': { params: MemoryListParams; result: MemoryListResult };
  'memory.delete': { params: MemoryDeleteParams; result: MemoryDeleteResult };
  'playbooks.list': { params: PlaybooksListParams; result: PlaybooksListResult };
  'playbooks.get': { params: PlaybooksGetParams; result: PlaybooksGetResult };
  'playbooks.credentials.get': { params: PlaybooksCredentialsGetParams; result: PlaybooksCredentialsGetResult };
  'playbooks.credentials.set': { params: PlaybooksCredentialsSetParams; result: PlaybooksCredentialsSetResult };
  'playbooks.credentials.clear': { params: PlaybooksCredentialsClearParams; result: PlaybooksCredentialsClearResult };
  'playbooks.oauth.authorize': { params: PlaybooksOauthAuthorizeParams; result: PlaybooksOauthAuthorizeResult };
  'playbooks.oauth.clear': { params: PlaybooksOauthClearParams; result: PlaybooksOauthClearResult };
  'approval.respond': { params: ApprovalRespondParams; result: ApprovalRespondResult };
  'clarify.respond': { params: ClarifyRespondParams; result: ClarifyRespondResult };
  'confirm.respond': { params: ConfirmRespondParams; result: ConfirmRespondResult };
  'slash.exec': { params: SlashExecParams; result: SlashExecResult };
  'complete.slash': { params: CompleteSlashParams; result: CompleteSlashResult };
  'complete.path': { params: CompletePathParams; result: CompletePathResult };
  'terminal.resize': { params: TerminalResizeParams; result: TerminalResizeResult };
  'system.upgrade': { params: SystemUpgradeParams; result: SystemUpgradeResult };
  'voice.record': { params: VoiceRecordParams; result: VoiceRecordResult };
  'session.save': { params: SessionSaveParams; result: SessionSaveResult };
  'session.steer': { params: SessionSteerParams; result: SessionSteerResult };
  'session.usage': { params: SessionUsageParams; result: SessionUsageResult };
  'skills.reload': { params: SkillsReloadParams; result: SkillsReloadResult };
  'reload.env': { params: ReloadEnvParams; result: ReloadEnvResult };
  'sudo.respond': { params: SudoRespondParams; result: SudoRespondResult };
  'secret.respond': { params: SecretRespondParams; result: SecretRespondResult };
  'image.attach': { params: ImageAttachParams; result: ImageAttachResult };
  'prompt.submit': { params: PromptSubmitParams; result: PromptSubmitResult };
  'prompt.background': { params: PromptBackgroundParams; result: PromptBackgroundResult };
  'browser.state': { params: BrowserStateParams; result: BrowserStateResult };
  'browser.open': { params: BrowserOpenParams; result: BrowserOpenResult };
  'browser.tabs': { params: BrowserTabsParams; result: BrowserTabsResult };
  'browser.frame': { params: BrowserFrameParams; result: BrowserFrameResult };
  'browser.read': { params: BrowserReadParams; result: BrowserReadResult };
  'browser.input': { params: BrowserInputParams; result: BrowserInputResult };
  'browser.mode': { params: BrowserModeParams; result: BrowserModeResult };
  'browser.close': { params: BrowserCloseParams; result: BrowserCloseResult };
  'browser.watch': { params: BrowserWatchParams; result: BrowserWatchResult };
  'knowledge.status': { params: KnowledgeStatusParams; result: KnowledgeStatusResult };
  'knowledge.bases.list': { params: KnowledgeBasesListParams; result: KnowledgeBasesListResult };
  'knowledge.bases.create': { params: KnowledgeBasesCreateParams; result: KnowledgeBasesCreateResult };
  'knowledge.bases.rename': { params: KnowledgeBasesRenameParams; result: KnowledgeBasesRenameResult };
  'knowledge.bases.delete': { params: KnowledgeBasesDeleteParams; result: KnowledgeBasesDeleteResult };
  'knowledge.documents.list': { params: KnowledgeDocumentsListParams; result: KnowledgeDocumentsListResult };
  'knowledge.documents.add': { params: KnowledgeDocumentsAddParams; result: KnowledgeDocumentsAddResult };
  'knowledge.documents.index': { params: KnowledgeDocumentsIndexParams; result: KnowledgeDocumentsIndexResult };
  'knowledge.documents.delete': { params: KnowledgeDocumentsDeleteParams; result: KnowledgeDocumentsDeleteResult };
  'knowledge.search': { params: KnowledgeSearchParams; result: KnowledgeSearchResult };
  'clipboard.paste': { params: ClipboardPasteParams; result: ClipboardPasteResult };
  'command.dispatch': { params: CommandDispatchParams; result: CommandDispatchResult };
  'delegation.status': { params: DelegationStatusParams; result: DelegationStatusResult };
  'delegation.pause': { params: DelegationPauseParams; result: DelegationPauseResult };
  'input.detect_drop': { params: InputDetectDropParams; result: InputDetectDropResult };
  'session.interrupt': { params: SessionInterruptParams; result: SessionInterruptResult };
  'shell.exec': { params: ShellExecParams; result: ShellExecResult };
  'skills.manage': { params: SkillsManageParams; result: SkillsManageResult };
  'subagent.interrupt': { params: SubagentInterruptParams; result: SubagentInterruptResult };
  'subagent.cancel_session': { params: SubagentCancelSessionParams; result: SubagentCancelSessionResult };
  'subagent.cancel_instance': { params: SubagentCancelInstanceParams; result: SubagentCancelInstanceResult };
}

/** The literal union of callable method names. */
export type RpcMethod = keyof RpcMethods;

export type ParamsOf<M extends RpcMethod> = RpcMethods[M]['params'];
export type ResultOf<M extends RpcMethod> = RpcMethods[M]['result'];

/** Method names present in the contract, for a runtime guard at the edges. */
export const RPC_METHODS = [
  "approval.respond",
  "browser.close",
  "browser.frame",
  "browser.input",
  "browser.manage",
  "browser.mode",
  "browser.open",
  "browser.read",
  "browser.state",
  "browser.tabs",
  "browser.watch",
  "channels.configure",
  "channels.qr",
  "channels.status",
  "clarify.respond",
  "cli.dispatch",
  "clipboard.paste",
  "command.dispatch",
  "commands.catalog",
  "complete.path",
  "complete.slash",
  "config.get",
  "config.set",
  "config.unset",
  "confirm.respond",
  "cron.delete",
  "cron.list",
  "cron.run_now",
  "cron.runs",
  "cron.save",
  "cron.set_enabled",
  "dag.get",
  "dag.node",
  "delegation.pause",
  "delegation.status",
  "deliverables.list",
  "ext.list",
  "fs.list",
  "fs.open",
  "fs.read",
  "fs.reveal",
  "fs.upload",
  "image.attach",
  "input.detect_drop",
  "knowledge.bases.create",
  "knowledge.bases.delete",
  "knowledge.bases.list",
  "knowledge.bases.rename",
  "knowledge.documents.add",
  "knowledge.documents.delete",
  "knowledge.documents.index",
  "knowledge.documents.list",
  "knowledge.search",
  "knowledge.status",
  "mcp.list",
  "mcp.test",
  "mcp.tools",
  "memory.delete",
  "memory.list",
  "memory.stats",
  "model.add_endpoint",
  "model.add_model",
  "model.disconnect",
  "model.endpoints",
  "model.fetch_models",
  "model.options",
  "model.remove_endpoint",
  "model.remove_model",
  "model.save_key",
  "model.set_protocol",
  "playbooks.credentials.clear",
  "playbooks.credentials.get",
  "playbooks.credentials.set",
  "playbooks.get",
  "playbooks.list",
  "playbooks.oauth.authorize",
  "playbooks.oauth.clear",
  "plug.auth",
  "plug.install",
  "plug.remove",
  "plug.toggle",
  "plughub.detail",
  "plughub.search",
  "process.stop",
  "prompt.background",
  "prompt.submit",
  "reload.env",
  "reload.mcp",
  "rollback.diff",
  "rollback.list",
  "rollback.restore",
  "secret.respond",
  "session.archive",
  "session.branch",
  "session.clear",
  "session.close",
  "session.compress",
  "session.create",
  "session.delete",
  "session.export",
  "session.get",
  "session.history",
  "session.interrupt",
  "session.list",
  "session.most_recent",
  "session.pin",
  "session.resume",
  "session.save",
  "session.set_mode",
  "session.status",
  "session.steer",
  "session.title",
  "session.undo",
  "session.usage",
  "settings.everos",
  "settings.everosSet",
  "settings.everos_set",
  "settings.get",
  "settings.set",
  "settings.usage",
  "setup.status",
  "shell.exec",
  "skill.list",
  "skill.pin",
  "skill.unpin",
  "skillhub.detail",
  "skillhub.install",
  "skillhub.remove",
  "skillhub.search",
  "skills.manage",
  "skills.reload",
  "slash.exec",
  "spawn_tree.list",
  "spawn_tree.load",
  "spawn_tree.save",
  "subagent.cancel_instance",
  "subagent.cancel_session",
  "subagent.context",
  "subagent.interrupt",
  "subagent.list",
  "subagents.add",
  "subagents.build",
  "subagents.instance.create",
  "subagents.instance.forget",
  "subagents.instance.history",
  "subagents.instance.set_mode",
  "subagents.instance.steer",
  "subagents.instances",
  "subagents.list",
  "subagents.probe",
  "subagents.remove",
  "subagents.test",
  "subagents.test_cancel",
  "subagents.toggle",
  "subagents.update",
  "sudo.respond",
  "system.hello",
  "system.ping",
  "system.upgrade",
  "system.version",
  "terminal.resize",
  "tools.configure",
  "turn.cancel",
  "turn.send",
  "turn.subscribe",
  "turn.unsubscribe",
  "voice.record",
  "voice.toggle"
] as const;

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope -- protocol level, not part of the method schema.
// ---------------------------------------------------------------------------

export interface JsonRpcRequest<M extends RpcMethod = RpcMethod> {
  jsonrpc: '2.0';
  id: number;
  method: M;
  params: ParamsOf<M>;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse<R = unknown> {
  jsonrpc: '2.0';
  id: number;
  result?: R;
  error?: JsonRpcError;
}

/** A server-initiated frame: no id, and the method is outside the call map. */
export interface JsonRpcNotification<P = unknown> {
  jsonrpc?: '2.0';
  method: string;
  params?: P;
}
