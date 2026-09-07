// AUTO-GENERATED — DO NOT EDIT — run `npm run gen:rpc`
//
// Source of truth: rpc-schema/openrpc.json (OpenRPC 1.2.6).
// Regenerate via: cd ui-tui && npm run gen:rpc
// Lint (drift check) via: cd ui-tui && npm run lint:rpc
//
// 74 method-scoped types (37 RPC methods × {Params, Result}) + all
// components/schemas + JSON-RPC 2.0 envelope types.

/* eslint-disable */
/* tslint:disable */

/**
 * Recursive JSON value type. Implemented as an unconstrained object in JSON Schema; downstream Pydantic uses typing.Any.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "JsonValue".
 */
export type JsonValue = string | number | boolean | null | unknown[] | {};
/**
 * Per-node status reported by the DAG runner. 'interrupted' never comes off the wire -- it is what a client infers for a node still called running when the run stopped reporting. 'exception' is a node that finished without accomplishing its task and is waiting on a decision; it is not terminal.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagNodeStatus".
 */
export type DagNodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'cancelled' | 'exception';
/**
 * Per-node status in a run read back off disk. Unlike the event vocabulary this includes 'interrupted', which the server infers for a node the registry still calls running on a run nothing is executing. 'exception' is a node that finished without accomplishing its task and is waiting on a decision; it is not terminal.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagSnapshotNodeStatus".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnEvent".
 */
export type TurnEvent =
  | MessageStartEvent
  | TurnStartedEvent
  | EpisodeStartEvent
  | NoticeEvent
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
  | DagRunCompletedEvent
  | CronMissedEvent
  | MediaEvent
  | SessionTitledEvent
  | SessionNamingEndedEvent;

/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserTab".
 */
export interface BrowserTab {
  index: number;
  url: string;
  title: string;
  active: boolean;
  loading?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserConsoleLine".
 */
export interface BrowserConsoleLine {
  type: string;
  text: string;
}
/**
 * One actionable element, with the id a click can be aimed at.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserRef".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCompressSummary".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionInitInfo".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionUsage".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TranscriptMessage".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TranscriptToolCall".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TranscriptTurnEnded".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TranscriptNotice".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TranscriptDelegated".
 */
export interface TranscriptDelegated {
  kind: 'spawn' | 'dag';
  label: string;
  status: 'ok' | 'error' | 'exception';
  /**
   * Set for kind=dag, so a client can open the run.
   */
  run_id?: string;
  /**
   * Set for a dag node's own message, so a client can place it against that row.
   */
  node_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ExtPluginRow".
 */
export interface ExtPluginRow {
  id: string;
  display_name: string;
  version: string;
  enabled: boolean;
  bundled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ExtSkillRow".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ExtToolRow".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolSetupNeed".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronJobInfo".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronRun".
 */
export interface CronRun {
  at_ms?: number;
  ok: boolean;
  preview: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ApiUsageModel".
 */
export interface ApiUsageModel {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
  model: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ApiUsageTotals".
 */
export interface ApiUsageTotals {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "LlmUsage".
 */
export interface LlmUsage {
  total: ApiUsageTotals;
  /**
   * Most expensive first.
   */
  models: ApiUsageModel[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolUsage".
 */
export interface ToolUsage {
  total: number;
  /**
   * Most called first.
   */
  counts: ToolUsageCount[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolUsageCount".
 */
export interface ToolUsageCount {
  name: string;
  count: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "EverosSection".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelField".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelStatusRow".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsEntry".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryItem".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlughubCatalogItem".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpSnapshot".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugLedger".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubItem".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubSubscores".
 */
export interface SkillhubSubscores {
  utility: number;
  robustness: number;
  safety: number;
  flags: string[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionInfo".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionListItem".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionMessage".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpServerInfo".
 */
export interface McpServerInfo {
  name: string;
  transport: 'stdio' | 'sse' | 'streamableHttp';
  connected: boolean;
  tool_count: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpToolInfo".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillInfo".
 */
export interface SkillInfo {
  name: string;
  source: 'local' | 'remote';
  pinned: boolean;
  description: string;
  tags: string[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentCall".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentRow".
 */
export interface SubagentRow {
  name: string;
  preset?: string;
  kind: 'builtin' | 'cli' | 'openai' | 'acp';
  description: string;
  enabled: boolean;
  configured: boolean;
  /**
   * Discovered under `subagents/` rather than written into config: one of the raven builds that ship beside this one, materialized as a row on every table build. Like `builtin` it leaves `configured` false -- there is no config entry to delete, and removing it means removing its folder -- but unlike `builtin` it is a real subprocess with a command, so it is probed and it can be unready (an unbuilt venv leaves it listed and disabled). Absent from a server that predates discovery.
   */
  vendored?: boolean;
  /**
   * A vendored folder's venv is being built right now (`subagents.build`). Its own flag rather than `test_running`: a build and a test are different verbs on the same row, and one must not read as the other. Absent from a server that predates discovery.
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelOptionProvider".
 */
export interface ModelOptionProvider {
  slug: string;
  name: string;
  authenticated: boolean;
  is_current: boolean;
  auth_type: string;
  key_env?: string;
  models: string[];
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
 * How a model reads to a person. Absent for a model no catalogue knows -- one released since the bundled snapshot, or served by a local deployment -- in which case the id is all there is to show.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelLabel".
 */
export interface ModelLabel {
  label: string;
  description?: string;
}
/**
 * One of a provider section's several url/key groups. `label` is the idempotency key the write methods address an entry by.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ProviderEndpointInfo".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DirectTarget".
 */
export interface DirectTarget {
  agent: string;
  handle: string;
}
/**
 * One sub-agent instance this session has used, verbatim from the instance registry apart from `resumable`. camelCase because the web RPC serves the same record unchanged. `runId` / `nodeId` name the DAG node an instance belongs to: set on a dag-node row, and on the ordinary row of a stateful node, which is what lets one invocation's two rows be reported as one.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "InstanceRow".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DirectTurn".
 */
export interface DirectTurn {
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnUsage".
 */
export interface TurnUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd?: number;
  context_used?: number;
  context_max?: number;
  context_percent?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CliResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CliDispatchResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "StubResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "VoiceToggleResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserManageResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeSaveResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeListResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeLoadResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ProcessStopResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackListResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackDiffResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackRestoreResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolsConfigureResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "VoiceRecordResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSaveResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSteerResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionUsageResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillsReloadResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ReloadEnvResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SudoRespondResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SecretRespondResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ImageAttachResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PromptSubmitResult".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PromptBackgroundResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CommandsCatalogResponse".
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CommandsCatalogResult".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnStartedEvent".
 */
export interface TurnStartedEvent {
  type: 'turn.started';
  payload: {
    turn_id: string;
    delegated?: {
      kind: 'spawn' | 'dag';
      label: string;
      status: 'ok' | 'error' | 'exception';
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MessageStartEvent".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "EpisodeStartEvent".
 */
export interface EpisodeStartEvent {
  type: 'episode.start';
  payload: {
    index: number;
  };
}
/**
 * Prose the runtime wrote, not the model.
 *
 * It must not arrive as `token.delta`: that buffer is the model's voice, so the text would render as the answer -- glued to whatever the model narrated just before it, carrying the answer's copy and branch actions, and stuck in English whatever language the turn was in.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "NoticeEvent".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TokenDeltaEvent".
 */
export interface TokenDeltaEvent {
  type: 'token.delta';
  payload: {
    text: string;
    target?: DirectTarget;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ThinkingDeltaEvent".
 */
export interface ThinkingDeltaEvent {
  type: 'thinking.delta';
  payload: {
    text: string;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolStartEvent".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolProgressEvent".
 */
export interface ToolProgressEvent {
  type: 'tool.progress';
  payload: {
    tool_call_id: string;
    preview: string;
  };
}
/**
 * One file a tool call wrote, as contents rather than as a rendering of them. Beside ToolCompleteEvent.diff rather than instead of it: a client that draws its own diff needs the text, and a unified diff cannot be turned back into the file.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FileChange".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolCompleteEvent".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MessageCompleteEvent".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ErrorEvent".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronDeliveredEvent".
 */
export interface CronDeliveredEvent {
  type: 'cron.delivered';
  payload: {
    job_id: string;
    name: string;
    text: string;
    fired_at: string;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronMissedEvent".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionTitledEvent".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionNamingEndedEvent".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentDeliveredEvent".
 */
export interface SubagentDeliveredEvent {
  type: 'subagent.delivered';
  payload: {
    kind: 'spawn' | 'dag';
    /**
     * The spawn's display label, or the dag's run_id.
     */
    label: string;
    status: 'ok' | 'error' | 'exception';
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentStatusEvent".
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
     * The spawn record id subagent.context reads. Known from 'running' onward; a 'pending' run has not opened its record yet, so there is nothing to read.
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagSnapshotNode".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagRunSnapshot".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagNodeDetail".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagRunStartedEvent".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagNodeUpdatedEvent".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagRunReplannedEvent".
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
 * The run finished. Terminal node output text is deliberately absent -- it is already in the tool result.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagRunCompletedEvent".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MediaItem".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MediaEvent".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBase".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocument".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeHit".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookNodeShape".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookRow".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookParam".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookNode".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookDetail".
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybookMcpServer".
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
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionListParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionListResult".
 */
export interface SessionListResult {
  sessions: SessionListItem[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionGetParams".
 */
export interface SessionGetParams {
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionGetResult".
 */
export interface SessionGetResult {
  session: SessionInfo;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCreateParams".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCreateResult".
 */
export interface SessionCreateResult {
  session_id: string;
  info: SessionInitInfo;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionResumeParams".
 */
export interface SessionResumeParams {
  /**
   * An unknown key falls back to a freshly minted one.
   */
  session_id?: string;
  cols?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionResumeResult".
 */
export interface SessionResumeResult {
  session_id: string;
  info: SessionInitInfo;
  /**
   * Every stored message, not a sliced history: N stored is N on the wire.
   */
  messages: TranscriptMessage[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionDeleteParams".
 */
export interface SessionDeleteParams {
  /**
   * Full session_key as sent by the UI.
   */
  session_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionDeleteResult".
 */
export interface SessionDeleteResult {
  /**
   * The session_id that was deleted (matches the request param); null when no such session file existed.
   */
  deleted?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionMostRecentParams".
 */
export interface SessionMostRecentParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionMostRecentResult".
 */
export interface SessionMostRecentResult {
  /**
   * Full tui:<chat_id> key; absent/null when no sessions exist.
   */
  session_id?: string;
  source?: string;
  started_at?: number;
  title?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionTitleParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionTitleResult".
 */
export interface SessionTitleResult {
  title?: string;
  session_key: string;
  /**
   * True when the title is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionPinParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionPinResult".
 */
export interface SessionPinResult {
  pinned: boolean;
  session_key: string;
  /**
   * True when the flag is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionArchiveParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionArchiveResult".
 */
export interface SessionArchiveResult {
  archived: boolean;
  session_key: string;
  /**
   * True when the flag is held in memory for a lazy (never-saved) session and lands with the session's first save.
   */
  pending: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionClearParams".
 */
export interface SessionClearParams {
  /**
   * Full session_key to clear.
   */
  session_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionClearResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionUndoParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionUndoResult".
 */
export interface SessionUndoResult {
  /**
   * Messages dropped (0 = nothing to undo).
   */
  removed: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionExportParams".
 */
export interface SessionExportParams {
  /**
   * Session id / prefix / full key to export; current session when omitted.
   */
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionExportResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionHistoryParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionHistoryResult".
 */
export interface SessionHistoryResult {
  messages: SessionMessage[];
  total: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnSendParams".
 */
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
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnSendResult".
 */
export interface TurnSendResult {
  turn_id: string;
  accepted: boolean;
  /**
   * Whether a session-naming call was started for this turn. A client holding a placeholder for the name can settle it on false rather than wait out its grace period. Not quite the same as 'no session.titled is coming': a send that arrives while an earlier namer for the same session is still running is also declined, and that one's title may still land -- it overwrites, which is why the weaker guarantee is enough.
   */
  naming: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnSubscribeParams".
 */
export interface TurnSubscribeParams {
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnSubscribeResult".
 */
export interface TurnSubscribeResult {
  subscription_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnUnsubscribeParams".
 */
export interface TurnUnsubscribeParams {
  subscription_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnUnsubscribeResult".
 */
export interface TurnUnsubscribeResult {
  unsubscribed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnCancelParams".
 */
export interface TurnCancelParams {
  session_key: string;
  target?: DirectTarget;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnCancelResult".
 */
export interface TurnCancelResult {
  cancelled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpListParams".
 */
export interface McpListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpListResult".
 */
export interface McpListResult {
  servers: McpServerInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpTestParams".
 */
export interface McpTestParams {
  server_name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpTestResult".
 */
export interface McpTestResult {
  ok: boolean;
  latency_ms: number;
  error?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpToolsParams".
 */
export interface McpToolsParams {
  server_name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "McpToolsResult".
 */
export interface McpToolsResult {
  tools: McpToolInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillListParams".
 */
export interface SkillListParams {
  /**
   * Filter by skill source; default 'all'.
   */
  source?: 'local' | 'remote' | 'all';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillListResult".
 */
export interface SkillListResult {
  skills: SkillInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillPinParams".
 */
export interface SkillPinParams {
  skill_name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillPinResult".
 */
export interface SkillPinResult {
  pinned: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillUnpinParams".
 */
export interface SkillUnpinParams {
  skill_name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillUnpinResult".
 */
export interface SkillUnpinResult {
  unpinned: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelOptionsParams".
 */
export interface ModelOptionsParams {
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelOptionsResult".
 */
export interface ModelOptionsResult {
  model: string;
  provider: string;
  providers: ModelOptionProvider[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelSetProtocolParams".
 */
export interface ModelSetProtocolParams {
  slug: string;
  model: string;
  protocol: 'auto' | 'chat' | 'responses' | 'anthropic';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelSetProtocolResult".
 */
export interface ModelSetProtocolResult {
  provider: ModelOptionProvider;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelSaveKeyParams".
 */
export interface ModelSaveKeyParams {
  slug: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelSaveKeyResult".
 */
export interface ModelSaveKeyResult {
  provider: ModelOptionProvider;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelDisconnectParams".
 */
export interface ModelDisconnectParams {
  slug: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelDisconnectResult".
 */
export interface ModelDisconnectResult {
  disconnected: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelAddModelParams".
 */
export interface ModelAddModelParams {
  slug: string;
  model: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelAddModelResult".
 */
export interface ModelAddModelResult {
  provider: ModelOptionProvider;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelRemoveModelParams".
 */
export interface ModelRemoveModelParams {
  slug: string;
  model: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelRemoveModelResult".
 */
export interface ModelRemoveModelResult {
  provider: ModelOptionProvider;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelEndpointsParams".
 */
export interface ModelEndpointsParams {
  slug: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelEndpointsResult".
 */
export interface ModelEndpointsResult {
  endpoints: ProviderEndpointInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelAddEndpointParams".
 */
export interface ModelAddEndpointParams {
  slug: string;
  label: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelAddEndpointResult".
 */
export interface ModelAddEndpointResult {
  endpoints: ProviderEndpointInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelRemoveEndpointParams".
 */
export interface ModelRemoveEndpointParams {
  slug: string;
  label: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ModelRemoveEndpointResult".
 */
export interface ModelRemoveEndpointResult {
  endpoints: ProviderEndpointInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigGetParams".
 */
export interface ConfigGetParams {
  /**
   * If omitted, return all whitelisted fields. Unknown keys are silently dropped.
   */
  keys?: string[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigGetResult".
 */
export interface ConfigGetResult {
  config: {
    [k: string]: JsonValue;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigSetParams".
 */
export interface ConfigSetParams {
  key: string;
  value: JsonValue;
  session_id?: string;
  provider?: string;
  scope?: 'session' | 'default';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigSetResult".
 */
export interface ConfigSetResult {
  applied: boolean;
  previous: JsonValue | null;
  value?: string;
  scope?: 'session' | 'default';
  session_id?: string;
  applies_to_session?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigUnsetParams".
 */
export interface ConfigUnsetParams {
  key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigUnsetResult".
 */
export interface ConfigUnsetResult {
  removed: boolean;
  previous: JsonValue | null;
  default: JsonValue | null;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentListParams".
 */
export interface SubagentListParams {
  /**
   * Absent or unknown is an empty list, not an error.
   */
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentListResult".
 */
export interface SubagentListResult {
  items: SubagentCall[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentContextParams".
 */
export interface SubagentContextParams {
  id: string;
  /**
   * A call is addressed by conversation and call, never by call alone.
   */
  session_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentContextResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsListParams".
 */
export interface SubagentsListParams {
  probe?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsListResult".
 */
export interface SubagentsListResult {
  rows: SubagentRow[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsAddParams".
 */
export interface SubagentsAddParams {
  preset: string;
  name?: string;
  description?: string;
  api_key?: string;
  mcps?: string[];
  allow_mcp_secrets?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsAddResult".
 */
export interface SubagentsAddResult {
  added: boolean;
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsUpdateParams".
 */
export interface SubagentsUpdateParams {
  name: string;
  new_name?: string;
  description?: string;
  api_key?: string;
  mcps?: string[];
  allow_mcp_secrets?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsUpdateResult".
 */
export interface SubagentsUpdateResult {
  updated: boolean;
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsRemoveParams".
 */
export interface SubagentsRemoveParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsRemoveResult".
 */
export interface SubagentsRemoveResult {
  removed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsBuildParams".
 */
export interface SubagentsBuildParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsBuildResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsToggleParams".
 */
export interface SubagentsToggleParams {
  name: string;
  enabled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsToggleResult".
 */
export interface SubagentsToggleResult {
  enabled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsProbeParams".
 */
export interface SubagentsProbeParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsProbeResult".
 */
export interface SubagentsProbeResult {
  rows: SubagentRow[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsTestParams".
 */
export interface SubagentsTestParams {
  name: string;
  source?: 'config' | 'preset';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsTestResult".
 */
export interface SubagentsTestResult {
  ok: boolean;
  detail: string;
  elapsed_ms: number;
  reply?: string;
  cancelled?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsTestCancelParams".
 */
export interface SubagentsTestCancelParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsTestCancelResult".
 */
export interface SubagentsTestCancelResult {
  cancelled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstancesParams".
 */
export interface SubagentsInstancesParams {
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstancesResult".
 */
export interface SubagentsInstancesResult {
  instances: InstanceRow[];
  /**
   * Direct-chat turns, and instances the user created, not yet reported to the main agent. Display only.
   */
  pending_handoff_count: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceCreateParams".
 */
export interface SubagentsInstanceCreateParams {
  session_key: string;
  /**
   * Which sub-agent to instantiate. Must be enabled and stateful: a direct chat is a continuation, and against a stateless agent every turn would start over.
   */
  agent: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceCreateResult".
 */
export interface SubagentsInstanceCreateResult {
  instance: InstanceRow;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceHistoryParams".
 */
export interface SubagentsInstanceHistoryParams {
  session_key: string;
  agent: string;
  handle: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceHistoryResult".
 */
export interface SubagentsInstanceHistoryResult {
  turns: DirectTurn[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceForgetParams".
 */
export interface SubagentsInstanceForgetParams {
  session_key: string;
  agent: string;
  handle: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceForgetResult".
 */
export interface SubagentsInstanceForgetResult {
  removed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceSteerParams".
 */
export interface SubagentsInstanceSteerParams {
  session_key: string;
  agent: string;
  handle: string;
  text: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceSteerResult".
 */
export interface SubagentsInstanceSteerResult {
  status: 'injected' | 'no_turn' | 'unsupported';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceSetModeParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentsInstanceSetModeResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSetModeParams".
 */
export interface SessionSetModeParams {
  session_key: string;
  mode?: string;
  /**
   * Drop the session override and go back to the configured default. Wins over mode when both are given, the opposite of subagents.instance.set_mode -- these are different operations: this one selects a tier, that one removes an override.
   */
  clear?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSetModeResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemHelloParams".
 */
export interface SystemHelloParams {
  client_version: string;
  client_capabilities?: string[];
  /**
   * Which front end this connection is ('tui', 'page', 'shell'). Recorded per connection for tracing only; session keys and channels are unaffected.
   */
  surface?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemHelloResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemPingParams".
 */
export interface SystemPingParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemPingResult".
 */
export interface SystemPingResult {
  pong: true;
  server_time_ms: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemVersionParams".
 */
export interface SystemVersionParams {
  /**
   * Fetch the latest release now instead of answering from the daily cache.
   */
  check?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemVersionResult".
 */
export interface SystemVersionResult {
  server_version: string;
  /**
   * OpenRPC info.version mirrored back to client.
   */
  schema_version: string;
  raven_version: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CliDispatchParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SetupStatusParams".
 */
export interface SetupStatusParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SetupStatusResult".
 */
export interface SetupStatusResult {
  provider_configured: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ReloadMcpParams".
 */
export interface ReloadMcpParams {
  session_id?: string;
  confirm?: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ReloadMcpResult".
 */
export interface ReloadMcpResult {
  ok: boolean;
  status: 'reloaded' | 'noop' | 'confirm_required';
  message: string;
  reloaded: number;
  tools_changed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CommandsCatalogParams".
 */
export interface CommandsCatalogParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "VoiceToggleParams".
 */
export interface VoiceToggleParams {
  action?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserManageParams".
 */
export interface BrowserManageParams {
  action?: string;
  url?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeSaveParams".
 */
export interface SpawnTreeSaveParams {
  name?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeListParams".
 */
export interface SpawnTreeListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SpawnTreeLoadParams".
 */
export interface SpawnTreeLoadParams {
  name?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ProcessStopParams".
 */
export interface ProcessStopParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackListParams".
 */
export interface RollbackListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackDiffParams".
 */
export interface RollbackDiffParams {
  id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "RollbackRestoreParams".
 */
export interface RollbackRestoreParams {
  id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolsConfigureParams".
 */
export interface ToolsConfigureParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagGetParams".
 */
export interface DagGetParams {
  run_id: string;
  session_key?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagGetResult".
 */
export interface DagGetResult {
  run: DagRunSnapshot;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagNodeParams".
 */
export interface DagNodeParams {
  run_id: string;
  node: string;
  max_output_chars?: number;
  session_key?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DagNodeResult".
 */
export interface DagNodeResult {
  node: DagNodeDetail;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlughubSearchParams".
 */
export interface PlughubSearchParams {
  q?: string;
  category?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlughubSearchResult".
 */
export interface PlughubSearchResult {
  items: PlughubCatalogItem[];
  categories: string[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlughubDetailParams".
 */
export interface PlughubDetailParams {
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlughubDetailResult".
 */
export interface PlughubDetailResult {
  item: {
    [k: string]: JsonValue;
  };
  installed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugInstallParams".
 */
export interface PlugInstallParams {
  id: string;
  /**
   * Values for the entry's auth.fields, by key.
   */
  form?: {
    [k: string]: string;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugInstallResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugRemoveParams".
 */
export interface PlugRemoveParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugRemoveResult".
 */
export interface PlugRemoveResult {
  removed: boolean;
  /**
   * 'market' when a ledger drove the removal, 'manual' for a hand-written server.
   */
  origin: 'market' | 'manual';
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugToggleParams".
 */
export interface PlugToggleParams {
  name: string;
  enabled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugToggleResult".
 */
export interface PlugToggleResult {
  name: string;
  enabled: boolean;
  mcp?: McpSnapshot;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugAuthParams".
 */
export interface PlugAuthParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlugAuthResult".
 */
export interface PlugAuthResult {
  name: string;
  mcp?: McpSnapshot;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubSearchParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubSearchResult".
 */
export interface SkillhubSearchResult {
  items: SkillhubItem[];
  total: number;
  page: number;
  limit: number;
  base_url: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubDetailParams".
 */
export interface SkillhubDetailParams {
  /**
   * Hub UUID or dataset skill_id.
   */
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubDetailResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubInstallParams".
 */
export interface SkillhubInstallParams {
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubInstallResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubRemoveParams".
 */
export interface SkillhubRemoveParams {
  /**
   * Installed skill directory name.
   */
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillhubRemoveResult".
 */
export interface SkillhubRemoveResult {
  removed: boolean;
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCloseParams".
 */
export interface SessionCloseParams {
  /**
   * Absent or unknown is a no-op.
   */
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCloseResult".
 */
export interface SessionCloseResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionBranchParams".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionBranchResult".
 */
export interface SessionBranchResult {
  session_id?: string;
  title?: string;
  message_count?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCompressParams".
 */
export interface SessionCompressParams {
  session_id: string;
  focus_topic?: string;
}
/**
 * The three redraw fields ride along only when something was archived: a
 * caller that just dropped half the transcript is looking at messages that no
 * longer exist.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCompressResult".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionStatusParams".
 */
export interface SessionStatusParams {
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionStatusResult".
 */
export interface SessionStatusResult {
  /**
   * Rich-rendered `raven status` output with ANSI SGR sequences.
   */
  output: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ExtListParams".
 */
export interface ExtListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ExtListResult".
 */
export interface ExtListResult {
  skills: ExtSkillRow[];
  plugins: ExtPluginRow[];
  tools: ExtToolRow[];
  /**
   * Live connections, plus configured servers not yet connected, reported as disconnected.
   */
  mcp: McpSnapshot[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronListParams".
 */
export interface CronListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronListResult".
 */
export interface CronListResult {
  /**
   * Disabled jobs included.
   */
  jobs: CronJobInfo[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronSaveParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronSaveResult".
 */
export interface CronSaveResult {
  job: CronJobInfo;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronDeleteParams".
 */
export interface CronDeleteParams {
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronDeleteResult".
 */
export interface CronDeleteResult {
  deleted: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronSetEnabledParams".
 */
export interface CronSetEnabledParams {
  id: string;
  enabled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronSetEnabledResult".
 */
export interface CronSetEnabledResult {
  enabled: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronRunNowParams".
 */
export interface CronRunNowParams {
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronRunNowResult".
 */
export interface CronRunNowResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronRunsParams".
 */
export interface CronRunsParams {
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CronRunsResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsGetParams".
 */
export interface SettingsGetParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsGetResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsSetParams".
 */
export interface SettingsSetParams {
  /**
   * Dotted path; only whitelisted keys are writable through this method.
   */
  key: string;
  value: JsonValue;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsSetResult".
 */
export interface SettingsSetResult {
  applied: boolean;
  previous: JsonValue;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsUsageParams".
 */
export interface SettingsUsageParams {
  /**
   * Window to scan; 30 by default, capped at 90.
   */
  days?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsUsageResult".
 */
export interface SettingsUsageResult {
  days: number;
  llm: LlmUsage;
  tools: ToolUsage;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsEverosParams".
 */
export interface SettingsEverosParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsEverosResult".
 */
export interface SettingsEverosResult {
  sections: {
    [k: string]: EverosSection;
  };
  config_path: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsEverosSetParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SettingsEverosSetResult".
 */
export interface SettingsEverosSetResult {
  applied: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsStatusParams".
 */
export interface ChannelsStatusParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsStatusResult".
 */
export interface ChannelsStatusResult {
  channels: ChannelStatusRow[];
  gateway_running: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsConfigureParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsConfigureResult".
 */
export interface ChannelsConfigureResult {
  applied: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsQrParams".
 */
export interface ChannelsQrParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ChannelsQrResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsListParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsListResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsReadParams".
 */
export interface FsReadParams {
  path: string;
  max_bytes?: number;
  session?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsReadResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsUploadParams".
 */
export interface FsUploadParams {
  name: string;
  content_b64: string;
  session?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsUploadResult".
 */
export interface FsUploadResult {
  /**
   * Workspace-relative path to hand the agent; uploads never return bytes.
   */
  path: string;
  abs_path: string;
  size: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsRevealParams".
 */
export interface FsRevealParams {
  /**
   * Absolute, or relative to the session's working directory.
   */
  path: string;
  session?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsRevealResult".
 */
export interface FsRevealResult {
  ok: true;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsOpenParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "FsOpenResult".
 */
export interface FsOpenResult {
  ok: true;
  /**
   * The application asked for, echoed back; absent when the host default was used.
   */
  app?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DeliverablesListParams".
 */
export interface DeliverablesListParams {
  /**
   * Full session_key. An empty or unknown key answers with an empty list.
   */
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DeliverablesListResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryStatsParams".
 */
export interface MemoryStatsParams {}
/**
 * ``ok`` is false when a kind could not be counted; the counts stay zero
 * rather than the call failing, so the page opens with EverOS down.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryStatsResult".
 */
export interface MemoryStatsResult {
  ok: boolean;
  base_url: string;
  episodes: number;
  profiles: number;
  agent_cases: number;
  agent_skills: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryListParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryListResult".
 */
export interface MemoryListResult {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryDeleteParams".
 */
export interface MemoryDeleteParams {
  kind: 'episode' | 'profile' | 'agent_case' | 'agent_skill';
  id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MemoryDeleteResult".
 */
export interface MemoryDeleteResult {
  ok: boolean;
  /**
   * Deleting an episode also drops its derived facts and foresight.
   */
  removed: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybooksListParams".
 */
export interface PlaybooksListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybooksListResult".
 */
export interface PlaybooksListResult {
  playbooks: PlaybookRow[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybooksGetParams".
 */
export interface PlaybooksGetParams {
  name: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PlaybooksGetResult".
 */
export interface PlaybooksGetResult {
  playbook: PlaybookDetail;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ApprovalRespondParams".
 */
export interface ApprovalRespondParams {
  approval_id: string;
  /**
   * allow | deny.
   */
  choice: string;
  session_id?: string;
  /**
   * Compatibility spelling of session_id.
   */
  conversation_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ApprovalRespondResult".
 */
export interface ApprovalRespondResult {
  /**
   * False for an unknown, expired or mis-bound request; the caller fails closed.
   */
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ClarifyRespondParams".
 */
export interface ClarifyRespondParams {
  answer: string;
  request_id?: string;
  conversation_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ClarifyRespondResult".
 */
export interface ClarifyRespondResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfirmRespondParams".
 */
export interface ConfirmRespondParams {
  request_id: string;
  answer: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfirmRespondResult".
 */
export interface ConfirmRespondResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SlashExecParams".
 */
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
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SlashExecResult".
 */
export interface SlashExecResult {
  output: string;
  warning?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CompleteSlashParams".
 */
export interface CompleteSlashParams {
  word?: string;
  session_id?: string;
}
/**
 * The provider is a no-op that exists to stop the client's
 * completion-unavailable toast, so ``items`` is always empty and its element
 * type is whatever a real provider would later return.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CompleteSlashResult".
 */
export interface CompleteSlashResult {
  items: JsonValue[];
  replace_from: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CompletePathParams".
 */
export interface CompletePathParams {
  word?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CompletePathResult".
 */
export interface CompletePathResult {
  items: JsonValue[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TerminalResizeParams".
 */
export interface TerminalResizeParams {
  cols?: number;
  rows?: number;
  /**
   * Sent by the client; the handler does not read it.
   */
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TerminalResizeResult".
 */
export interface TerminalResizeResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemUpgradeParams".
 */
export interface SystemUpgradeParams {}
/**
 * Returned once the detached helper owns the install. The shutdown is
 * scheduled a beat later so this reply reaches the client first.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemUpgradeResult".
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "VoiceRecordParams".
 */
export interface VoiceRecordParams {
  action?: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSaveParams".
 */
export interface SessionSaveParams {
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionSteerParams".
 */
export interface SessionSteerParams {
  session_id?: string;
  text?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionUsageParams".
 */
export interface SessionUsageParams {
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillsReloadParams".
 */
export interface SkillsReloadParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ReloadEnvParams".
 */
export interface ReloadEnvParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SudoRespondParams".
 */
export interface SudoRespondParams {
  request_id?: string;
  password?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SecretRespondParams".
 */
export interface SecretRespondParams {
  request_id?: string;
  value?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ImageAttachParams".
 */
export interface ImageAttachParams {
  path?: string;
  session_id?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PromptSubmitParams".
 */
export interface PromptSubmitParams {
  session_id?: string;
  text?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "PromptBackgroundParams".
 */
export interface PromptBackgroundParams {
  session_id?: string;
  text?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserStateParams".
 */
export interface BrowserStateParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserStateResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserOpenParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserOpenResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserTabsParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserTabsResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserFrameParams".
 */
export interface BrowserFrameParams {
  /**
   * JPEG quality; 55 by default.
   */
  quality?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserFrameResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserReadParams".
 */
export interface BrowserReadParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserReadResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserInputParams".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserInputResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserModeParams".
 */
export interface BrowserModeParams {
  /**
   * True pops the page into a real window; false folds it back.
   */
  headful: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserModeResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserCloseParams".
 */
export interface BrowserCloseParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserCloseResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserWatchParams".
 */
export interface BrowserWatchParams {
  on: boolean;
  width?: number;
  height?: number;
  quality?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "BrowserWatchResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeStatusParams".
 */
export interface KnowledgeStatusParams {}
/**
 * Whether a base can be created, and with which model.
 *
 * ``model`` is empty exactly when ``configured`` is false. No credential is
 * reported: the key's presence *is* the flag.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeStatusResult".
 */
export interface KnowledgeStatusResult {
  configured: boolean;
  model: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesListParams".
 */
export interface KnowledgeBasesListParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesListResult".
 */
export interface KnowledgeBasesListResult {
  bases: KnowledgeBase[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesCreateParams".
 */
export interface KnowledgeBasesCreateParams {
  name: string;
  description?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesCreateResult".
 */
export interface KnowledgeBasesCreateResult {
  base: KnowledgeBase;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesRenameParams".
 */
export interface KnowledgeBasesRenameParams {
  base_id: string;
  name?: string;
  description?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesRenameResult".
 */
export interface KnowledgeBasesRenameResult {
  base: KnowledgeBase;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesDeleteParams".
 */
export interface KnowledgeBasesDeleteParams {
  base_id: string;
}
/**
 * False for a base that was not there: a second delete from a stale page
 * reached the outcome its caller wanted.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeBasesDeleteResult".
 */
export interface KnowledgeBasesDeleteResult {
  removed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsListParams".
 */
export interface KnowledgeDocumentsListParams {
  base_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsListResult".
 */
export interface KnowledgeDocumentsListResult {
  documents: KnowledgeDocument[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsAddParams".
 */
export interface KnowledgeDocumentsAddParams {
  base_id: string;
  path: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsAddResult".
 */
export interface KnowledgeDocumentsAddResult {
  document: KnowledgeDocument;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsIndexParams".
 */
export interface KnowledgeDocumentsIndexParams {
  document_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsIndexResult".
 */
export interface KnowledgeDocumentsIndexResult {
  document: KnowledgeDocument;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsDeleteParams".
 */
export interface KnowledgeDocumentsDeleteParams {
  document_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeDocumentsDeleteResult".
 */
export interface KnowledgeDocumentsDeleteResult {
  /**
   * False when there was no such document, which is not an error: two clicks on one row answer the same way.
   */
  removed: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeSearchParams".
 */
export interface KnowledgeSearchParams {
  base_ids: string[];
  query: string;
  top_k?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "KnowledgeSearchResult".
 */
export interface KnowledgeSearchResult {
  hits: KnowledgeHit[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ClipboardPasteParams".
 */
export interface ClipboardPasteParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ClipboardPasteResult".
 */
export interface ClipboardPasteResult {
  attached: boolean;
  message?: string;
  width?: number;
  height?: number;
  token_estimate?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CommandDispatchParams".
 */
export interface CommandDispatchParams {
  name: string;
  arg?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "CommandDispatchResult".
 */
export interface CommandDispatchResult {
  /**
   * `exec` (a shell-style command ran) or `skill` (the name resolved to a skill).
   */
  type: string;
  output?: string;
  name?: string;
  message?: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DelegationStatusParams".
 */
export interface DelegationStatusParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DelegationStatusResult".
 */
export interface DelegationStatusResult {
  max_concurrent_children: number;
  max_spawn_depth: number;
  paused: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DelegationPauseParams".
 */
export interface DelegationPauseParams {
  paused: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "DelegationPauseResult".
 */
export interface DelegationPauseResult {
  paused: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "InputDetectDropParams".
 */
export interface InputDetectDropParams {
  text: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "InputDetectDropResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionInterruptParams".
 */
export interface SessionInterruptParams {
  session_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionInterruptResult".
 */
export interface SessionInterruptResult {
  ok: boolean;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ShellExecParams".
 */
export interface ShellExecParams {
  command: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ShellExecResult".
 */
export interface ShellExecResult {
  code: number;
  stdout: string;
  stderr: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillsManageParams".
 */
export interface SkillsManageParams {
  /**
   * One of list, inspect, search, browse, install.
   */
  action: string;
  query?: string;
  page?: number;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SkillsManageResult".
 */
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
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentInterruptParams".
 */
export interface SubagentInterruptParams {
  subagent_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentInterruptResult".
 */
export interface SubagentInterruptResult {
  found: boolean;
  subagent_id: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentCancelSessionParams".
 */
export interface SubagentCancelSessionParams {
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentCancelSessionResult".
 */
export interface SubagentCancelSessionResult {
  cancelled: number;
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentCancelInstanceParams".
 */
export interface SubagentCancelInstanceParams {
  session_key?: string;
  agent: string;
  handle: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SubagentCancelInstanceResult".
 */
export interface SubagentCancelInstanceResult {
  found: boolean;
  session_key: string;
  agent: string;
  handle: string;
}

// ---- Schema-name aliases for structurally-deduplicated types ----
export type BrowserManageResult = StubResult;
export type CliDispatchResult = CliResult;
export type CommandsCatalogResult = CommandsCatalogResponse;
export type ImageAttachResult = StubResult;
export type ProcessStopResult = StubResult;
export type PromptBackgroundResult = StubResult;
export type PromptSubmitResult = StubResult;
export type ReloadEnvResult = StubResult;
export type RollbackDiffResult = StubResult;
export type RollbackListResult = StubResult;
export type RollbackRestoreResult = StubResult;
export type SecretRespondResult = StubResult;
export type SessionSaveResult = StubResult;
export type SessionSteerResult = StubResult;
export type SessionUsageResult = StubResult;
export type SkillsReloadResult = StubResult;
export type SpawnTreeListResult = StubResult;
export type SpawnTreeLoadResult = StubResult;
export type SpawnTreeSaveResult = StubResult;
export type SudoRespondResult = StubResult;
export type ToolsConfigureResult = StubResult;
export type VoiceRecordResult = StubResult;
export type VoiceToggleResult = StubResult;

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope (specs/tui-ipc.md §2.1/2.2/2.3/2.4)
// ---------------------------------------------------------------------------

export interface JsonRpcRequest<P = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: P;
}

export interface JsonRpcSuccess<R = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  result: R;
}

export interface JsonRpcErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcErrorResponse {
  jsonrpc: '2.0';
  id: string | number;
  error: JsonRpcErrorObject;
}

export type JsonRpcResponse<R = unknown> = JsonRpcSuccess<R> | JsonRpcErrorResponse;

export interface JsonRpcNotification<P = unknown> {
  jsonrpc: '2.0';
  method: string;
  params: P;
}

export interface EventNotificationParams<E = unknown> {
  subscription_id: string;
  event: E;
}

export function isJsonRpcError<R>(
  resp: JsonRpcResponse<R>,
): resp is JsonRpcErrorResponse {
  return (resp as JsonRpcErrorResponse).error !== undefined;
}
