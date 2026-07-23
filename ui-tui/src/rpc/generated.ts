// AUTO-GENERATED — DO NOT EDIT — run `npm run gen:rpc`
//
// Source of truth: ui-tui/rpc-schema/openrpc.json (OpenRPC 1.2.6).
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
 * Discriminated union of turn streaming events. The 'type' field is the discriminator.
 *
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnEvent".
 */
export type TurnEvent =
  | MessageStartEvent
  | TokenDeltaEvent
  | ThinkingDeltaEvent
  | ToolStartEvent
  | ToolProgressEvent
  | ToolCompleteEvent
  | MessageCompleteEvent
  | ErrorEvent
  | CronDeliveredEvent;

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
  preview: string;
  source?: string;
  /**
   * Unix timestamp derived from created_at.
   */
  started_at: number;
  title: string;
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
  total_models: number;
  needs_api_base: boolean;
  warning: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "UsageSnapshot".
 */
export interface UsageSnapshot {
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
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "MessageStartEvent".
 */
export interface MessageStartEvent {
  type: 'message.start';
  payload: {
    turn_id: string;
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
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ToolCompleteEvent".
 */
export interface ToolCompleteEvent {
  type: 'tool.complete';
  payload: {
    tool_call_id: string;
    result_preview: string;
    truncated: boolean;
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
    usage: UsageSnapshot;
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
    message: string;
    reason?: 'cancelled_by_client' | 'internal';
    detail?: string;
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
 * via the `definition` "SessionListParams".
 */
export interface SessionListParams {
  /**
   * Max sessions to return.
   */
  limit?: number;
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
  channel: string;
  chat_id: string;
  metadata?: {
    [k: string]: JsonValue;
  };
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionCreateResult".
 */
export interface SessionCreateResult {
  session: SessionInfo;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionResumeParams".
 */
export interface SessionResumeParams {
  session_key: string;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SessionResumeResult".
 */
export interface SessionResumeResult {
  session: SessionInfo;
  last_messages: SessionMessage[];
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
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "TurnSendResult".
 */
export interface TurnSendResult {
  turn_id: string;
  accepted: boolean;
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
 * via the `definition` "ModelSaveKeyParams".
 */
export interface ModelSaveKeyParams {
  slug: string;
  api_key: string;
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
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ConfigSetResult".
 */
export interface ConfigSetResult {
  applied: boolean;
  previous: JsonValue | null;
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemHelloParams".
 */
export interface SystemHelloParams {
  client_version: string;
  client_capabilities?: string[];
}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "SystemHelloResult".
 */
export interface SystemHelloResult {
  server_version: string;
  server_capabilities: string[];
  session: {
    default_channel: 'tui';
    default_session_key: string;
  };
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
export interface SystemVersionParams {}
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
export interface ReloadMcpParams {}
/**
 * This interface was referenced by `RavenRpcRoot`'s JSON-Schema
 * via the `definition` "ReloadMcpResult".
 */
export interface ReloadMcpResult {
  ok: boolean;
  reloaded?: number;
  tools_changed?: boolean;
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

// ---- Schema-name aliases for structurally-deduplicated types ----
export type BrowserManageResult = StubResult;
export type CliDispatchResult = CliResult;
export type CommandsCatalogResult = CommandsCatalogResponse;
export type ProcessStopResult = StubResult;
export type RollbackDiffResult = StubResult;
export type RollbackListResult = StubResult;
export type RollbackRestoreResult = StubResult;
export type SpawnTreeListResult = StubResult;
export type SpawnTreeLoadResult = StubResult;
export type SpawnTreeSaveResult = StubResult;
export type ToolsConfigureResult = StubResult;
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
