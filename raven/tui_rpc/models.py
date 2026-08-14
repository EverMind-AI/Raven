"""Pydantic v2 models for the tui-ipc-bridge JSON-RPC contract.

These models are the Python-side mirror of ``ui-tui/rpc-schema/openrpc.json``.
Each public type defined in ``specs/tui-ipc.md`` §3.12 has a corresponding
:class:`pydantic.BaseModel`, and each RPC method has a ``<Method>Params`` and
``<Method>Result`` model.

Drift between this module and the OpenRPC schema is caught in CI by
``tests/test_rpc_schema_match.py``.  Any change here MUST be mirrored in the
schema (or vice versa) within the same commit.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Re-usable model config.  ``extra="forbid"`` makes Pydantic emit
# ``additionalProperties: false`` in the generated JSON Schema, matching the
# OpenRPC schema's explicit ``additionalProperties: false`` on every object.
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Base class for all RPC models — forbids extra fields by default."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Public types (specs/tui-ipc.md §3.12)
# ---------------------------------------------------------------------------


# ``JsonValue`` in JSON Schema is the union of all primitive + container types.
# We use ``Any`` here because the schema declares ``JsonValue`` as a permissive
# any-of-primitives type and the schema-match test pins JsonValue to its OpenRPC
# spec rather than to its Pydantic schema (see ``components/schemas/JsonValue``).
JsonValue = Any


class SessionInfo(_Strict):
    """A single session record as exposed by the RPC layer."""

    session_key: str = Field(..., description="<channel>:<chat_id> composite key.")
    channel: str
    chat_id: str
    created_at: str = Field(..., description="ISO-8601 timestamp.")
    updated_at: str = Field(..., description="ISO-8601 timestamp.")
    message_count: int
    metadata: dict[str, JsonValue]
    has_pending_clarification: bool


class SessionMessage(_Strict):
    """A single message inside a session's history."""

    index: int = Field(..., description="0-based position within session.messages.")
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    timestamp: str = Field(..., description="ISO-8601 timestamp.")
    metadata: dict[str, JsonValue] | None = None


class McpServerInfo(_Strict):
    """Metadata about a configured MCP server."""

    name: str
    transport: Literal["stdio", "sse", "streamableHttp"]
    connected: bool
    tool_count: int


class McpToolInfo(_Strict):
    """Metadata about a single tool exposed by an MCP server."""

    name: str = Field(..., description="Raw tool name (without mcp_<server>_ prefix).")
    description: str
    parameters: dict[str, JsonValue] = Field(..., description="JSON Schema for the tool's input arguments.")


class SkillInfo(_Strict):
    """Metadata about a skill (local or remote)."""

    name: str
    source: Literal["local", "remote"]
    pinned: bool
    description: str
    tags: list[str]


class SubagentRow(_Strict):
    """One row of the /subagents overlay.

    `api_key` is deliberately absent: `has_api_key` is the only thing the UI
    needs, and returning the value - even masked - would put a secret on the
    wire for a screen that never displays it.
    """

    name: str
    preset: str | None
    kind: Literal["cli", "openai"]
    description: str
    enabled: bool
    configured: bool
    group: Literal["installed", "uninstalled"]
    probe_status: Literal["ready", "attention", "missing", "unknown"]
    probe_detail: str
    has_api_key: bool
    last_test_ok: bool | None = None
    last_test_detail: str | None = None
    last_test_at_ms: int | None = None
    test_running: bool


class UsageSnapshot(_Strict):
    """Token / cost usage reported at the end of a turn."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None = None
    context_used: int | None = None
    context_max: int | None = None
    context_percent: int | None = None


class CliResult(_Strict):
    """The result envelope returned by ``cli.dispatch``."""

    stdout: str = Field(..., description="Rich-rendered output with ANSI SGR sequences.")
    stderr: str = Field(..., description="Error / warning output with ANSI SGR sequences.")
    exit_code: int = Field(..., description="CLI command exit code; 0 = success.")
    error_code: int | None = Field(
        default=None,
        description=("Only present for timeout / not-dispatch-compatible cases (mirrors a JSON-RPC error code)."),
    )


class StubResult(_Strict):
    """Shared shape for all hermes-only stub method results (-32012)."""

    error: str = Field(..., description="Human-readable explanation of why this method is not supported in v0.1.")
    hint: str | None = Field(
        default=None,
        description="Optional hint to the user (e.g., 'Press Ctrl+C').",
    )


# ---------------------------------------------------------------------------
# TurnEvent — discriminated union over the 8 streaming event variants.
# ---------------------------------------------------------------------------


class MessageStartPayload(_Strict):
    turn_id: str


class MessageStartEvent(_Strict):
    type: Literal["message.start"]
    payload: MessageStartPayload


class EpisodeStartPayload(_Strict):
    index: int


class EpisodeStartEvent(_Strict):
    type: Literal["episode.start"]
    payload: EpisodeStartPayload


class TokenDeltaPayload(_Strict):
    text: str


class TokenDeltaEvent(_Strict):
    type: Literal["token.delta"]
    payload: TokenDeltaPayload


class ThinkingDeltaPayload(_Strict):
    text: str


class ThinkingDeltaEvent(_Strict):
    type: Literal["thinking.delta"]
    payload: ThinkingDeltaPayload


class ToolStartPayload(_Strict):
    tool_call_id: str
    name: str
    arguments: dict[str, JsonValue]
    display: str | None = None


class ToolStartEvent(_Strict):
    type: Literal["tool.start"]
    payload: ToolStartPayload


class ToolProgressPayload(_Strict):
    tool_call_id: str
    preview: str


class ToolProgressEvent(_Strict):
    type: Literal["tool.progress"]
    payload: ToolProgressPayload


class ToolCompletePayload(_Strict):
    tool_call_id: str
    result_preview: str
    truncated: bool
    metadata: dict[str, JsonValue] | None = None


class ToolCompleteEvent(_Strict):
    type: Literal["tool.complete"]
    payload: ToolCompletePayload


class MessageCompletePayload(_Strict):
    turn_id: str
    usage: UsageSnapshot


class MessageCompleteEvent(_Strict):
    type: Literal["message.complete"]
    payload: MessageCompletePayload


class ErrorEventPayload(_Strict):
    code: int
    message: str
    reason: Literal["cancelled_by_client", "internal"] | None = None
    detail: str | None = None


class ErrorEvent(_Strict):
    type: Literal["error"]
    payload: ErrorEventPayload


class CronDeliveredPayload(_Strict):
    job_id: str
    name: str
    text: str
    fired_at: str


class CronDeliveredEvent(_Strict):
    type: Literal["cron.delivered"]
    payload: CronDeliveredPayload


DagNodeStatus = Literal["pending", "running", "completed", "failed", "skipped"]


class DagRunStartedNode(_Strict):
    id: str
    subagent: str
    depends_on: list[str]
    instance: str | None = None


class DagRunStartedPayload(_Strict):
    run_id: str
    tool_call_id: str | None = None
    nodes: list[DagRunStartedNode]


class DagRunStartedEvent(_Strict):
    type: Literal["dag.run_started"]
    payload: DagRunStartedPayload


class DagNodeUpdatedPayload(_Strict):
    run_id: str
    tool_call_id: str | None = None
    node: str
    status: DagNodeStatus
    started_at: int | None = None
    ended_at: int | None = None


class DagNodeUpdatedEvent(_Strict):
    type: Literal["dag.node_updated"]
    payload: DagNodeUpdatedPayload


class DagRunSummary(_Strict):
    total: int | None = None
    completed: int | None = None
    failed: int | None = None
    skipped: int | None = None


class DagRunFile(_Strict):
    node: str
    status: DagNodeStatus
    output_file: str | None = None
    error: str | None = None


class DagRunCompletedPayload(_Strict):
    run_id: str
    tool_call_id: str | None = None
    dir: str
    summary: DagRunSummary
    files: list[DagRunFile]


class DagRunCompletedEvent(_Strict):
    type: Literal["dag.run_completed"]
    payload: DagRunCompletedPayload


# ---------------------------------------------------------------------------
# dag.* methods -- a run read back off disk (the events are never replayed)
# ---------------------------------------------------------------------------

# Wider than DagNodeStatus: a snapshot can report ``interrupted``, which the
# server infers for a node the registry still calls running on a run nothing is
# executing. Nothing on the event wire may claim that.
DagSnapshotNodeStatus = Literal["pending", "running", "completed", "failed", "skipped", "interrupted"]


class DagSnapshotNode(_Strict):
    node: str
    subagent: str | None = None
    depends_on: list[str] | None = None
    instance: str | None = None
    status: DagSnapshotNodeStatus
    started_at: int | None = None
    ended_at: int | None = None
    prompt_file: str | None = None
    output_file: str | None = None
    error: str | None = None
    prompt_template: str | None = None


class DagTerminalOutput(_Strict):
    node: str
    text: str


class DagRunSnapshot(_Strict):
    run_id: str
    dir: str
    finalized: bool
    files: list[DagSnapshotNode]
    terminal_outputs: list[DagTerminalOutput] | None = None
    summary: DagRunSummary


class DagNodeDetail(_Strict):
    run_id: str
    node: str
    prompt: str | None = None
    prompt_file: str | None = None
    output: str | None = None
    output_file: str | None = None
    output_chars: int
    output_truncated: bool


class DagGetParams(_Strict):
    run_id: str
    session_key: str | None = None


class DagGetResult(_Strict):
    run: DagRunSnapshot


class DagNodeParams(_Strict):
    run_id: str
    node: str
    max_output_chars: int | None = None


class DagNodeResult(_Strict):
    node: DagNodeDetail


TurnEvent = Annotated[
    Union[
        MessageStartEvent,
        EpisodeStartEvent,
        TokenDeltaEvent,
        ThinkingDeltaEvent,
        ToolStartEvent,
        ToolProgressEvent,
        ToolCompleteEvent,
        MessageCompleteEvent,
        ErrorEvent,
        CronDeliveredEvent,
        DagRunStartedEvent,
        DagNodeUpdatedEvent,
        DagRunCompletedEvent,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# session.* methods
# ---------------------------------------------------------------------------


class SessionListItem(_Strict):
    """One row in the session picker (gatewayTypes.ts:130 SessionListItem)."""

    id: str = Field(..., description="Full session_key: <channel>:<chat_id>.")
    message_count: int
    preview: str
    source: str | None = None
    started_at: float = Field(..., description="Unix timestamp from created_at.")
    title: str


class SessionListParams(_Strict):
    limit: int | None = Field(default=None, description="Max sessions to return.")


class SessionListResult(_Strict):
    sessions: list[SessionListItem]


class SessionGetParams(_Strict):
    session_key: str


class SessionGetResult(_Strict):
    session: SessionInfo


class SessionCreateParams(_Strict):
    cols: int | None = Field(default=None, description="Terminal width the client is drawing at.")
    title: str | None = Field(default=None, description="Accepted and ignored; clients set titles via session.title.")


class SessionCreateResult(_Strict):
    """The key is minted lazily -- no file is written until the first save."""

    session_id: str
    info: "SessionInitInfo"


class SessionResumeParams(_Strict):
    session_id: str | None = Field(default=None, description="An unknown key falls back to a freshly minted one.")
    cols: int | None = None


class SessionResumeResult(_Strict):
    session_id: str
    info: "SessionInitInfo"
    messages: list["TranscriptMessage"] = Field(
        ...,
        description="Every stored message, not a sliced history: N stored is N on the wire.",
    )


class SessionDeleteParams(_Strict):
    session_id: str = Field(..., description="Full session_key as sent by the UI.")


class SessionDeleteResult(_Strict):
    deleted: str | None = Field(
        default=None,
        description=(
            "The session_id that was deleted (matches the request param); null when no such session file existed."
        ),
    )


class SessionMostRecentParams(_Strict):
    pass


class SessionMostRecentResult(_Strict):
    """Response shape per gatewayTypes.ts:147 SessionMostRecentResponse."""

    session_id: str | None = Field(
        default=None,
        description="Full tui:<chat_id> key, or null when no sessions exist.",
    )
    source: str | None = None
    started_at: float | None = None
    title: str | None = None


class SessionTitleParams(_Strict):
    """Params per slash/commands/core.ts:201,218 — session_id + optional title."""

    session_id: str = Field(..., description="Full session_key.")
    title: str | None = None


class SessionTitleResult(_Strict):
    """Response per gatewayTypes.ts:154 SessionTitleResponse.

    pending=True means the title is held in memory for a lazy (never-saved)
    session and lands with the session's first save.
    """

    title: str | None = None
    session_key: str
    pending: bool


class SessionClearParams(_Strict):
    """Params for session.clear — wipe messages in place, keep the sid."""

    session_id: str = Field(..., description="Full session_key to clear.")


class SessionClearResult(_Strict):
    session_id: str = Field(..., description="The same session_key (no new id minted).")
    cleared: bool = Field(..., description="True when the in-place wipe ran.")


class SessionUndoParams(_Strict):
    """Params for session.undo — drop the last n turns (default 1)."""

    session_id: str = Field(..., description="Full session_key to undo.")
    n: int = Field(1, description="Trailing turns to drop (role==user boundary).")


class SessionUndoResult(_Strict):
    removed: int = Field(..., description="Messages dropped (0 = nothing to undo).")


class SessionExportParams(_Strict):
    """Params for session.export — render a transcript to a Markdown file."""

    session_id: str | None = Field(
        default=None,
        description="Session id / prefix / full key to export; current session when omitted.",
    )


class SessionExportResult(_Strict):
    exported: bool = Field(..., description="True when a Markdown file was written.")
    path: str | None = Field(..., description="Absolute path of the written file, or null on failure.")
    reason: str | None = Field(
        default=None,
        description="Failure reason when not exported: not_found | ambiguous | write_failed.",
    )
    candidates: list[str] | None = Field(
        default=None,
        description="Candidate full keys when reason is ambiguous.",
    )


class SessionHistoryParams(_Strict):
    session_key: str
    max_messages: int | None = Field(
        default=None,
        description="Maximum number of messages to return; default 500 to match Session.get_history.",
    )
    before_index: int | None = Field(
        default=None,
        description="Return messages with index < before_index. Used for pagination.",
    )


class SessionHistoryResult(_Strict):
    messages: list[SessionMessage]
    total: int


# ---------------------------------------------------------------------------
# turn.* methods
# ---------------------------------------------------------------------------


class TurnSendParams(_Strict):
    session_key: str
    content: str
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    # Attachment paths, workspace-relative or absolute. The same lane channels
    # already use (``TurnRequest.media``): a vision-capable model gets the
    # picture inlined in the user message, anything else gets a note naming it.
    # Paths rather than bytes -- the caller has already put the file in the
    # workspace, and every file tool is workspace-scoped. Bounded here so a
    # malformed caller is refused at the schema rather than resolving thousands
    # of paths; the renderer caps how many are inlined regardless.
    media: list[str] | None = Field(default=None, max_length=64)


class TurnSendResult(_Strict):
    turn_id: str
    accepted: bool


class TurnSubscribeParams(_Strict):
    session_key: str


class TurnSubscribeResult(_Strict):
    subscription_id: str


class TurnUnsubscribeParams(_Strict):
    subscription_id: str


class TurnUnsubscribeResult(_Strict):
    unsubscribed: bool


class TurnCancelParams(_Strict):
    session_key: str


class TurnCancelResult(_Strict):
    cancelled: bool


# ---------------------------------------------------------------------------
# mcp.* methods
# ---------------------------------------------------------------------------


class McpListParams(_Strict):
    pass


class McpListResult(_Strict):
    servers: list[McpServerInfo]


class McpTestParams(_Strict):
    server_name: str


class McpTestResult(_Strict):
    ok: bool
    latency_ms: float
    error: str | None = None


class McpToolsParams(_Strict):
    server_name: str


class McpToolsResult(_Strict):
    tools: list[McpToolInfo]


# ---------------------------------------------------------------------------
# skill.* methods
# ---------------------------------------------------------------------------


class SkillListParams(_Strict):
    source: Literal["local", "remote", "all"] | None = Field(
        default=None,
        description="Filter by skill source; default 'all'.",
    )


class SkillListResult(_Strict):
    skills: list[SkillInfo]


class SkillPinParams(_Strict):
    skill_name: str


class SkillPinResult(_Strict):
    pinned: bool


class SkillUnpinParams(_Strict):
    skill_name: str


class SkillUnpinResult(_Strict):
    unpinned: bool


# ---------------------------------------------------------------------------
# model.* methods
# ---------------------------------------------------------------------------


class ModelLabel(_Strict):
    """How a model reads to a person, for the ids in ``models``.

    Present only for models a catalogue describes; one released since the
    bundled snapshot, or served by a local deployment, has no entry and the
    picker shows its id.
    """

    label: str
    description: str | None = None


class ModelOptionProvider(_Strict):
    """One provider row in the ``/model`` picker."""

    slug: str
    name: str
    authenticated: bool
    is_current: bool
    auth_type: str
    key_env: str | None = None
    models: list[str]
    model_labels: dict[str, ModelLabel] | None = None
    total_models: int
    needs_api_base: bool
    warning: str


class ModelOptionsParams(_Strict):
    session_id: str | None = None


class ModelOptionsResult(_Strict):
    model: str
    provider: str
    providers: list[ModelOptionProvider]


class ModelSaveKeyParams(_Strict):
    slug: str
    # Empty for a local deployment, which is reached by address and has no key.
    # The handler rejects an empty one for every other credential shape.
    api_key: str = ""
    api_base: str | None = None
    session_id: str | None = None


class ModelSaveKeyResult(_Strict):
    provider: ModelOptionProvider


class ModelDisconnectParams(_Strict):
    slug: str
    session_id: str | None = None


class ModelDisconnectResult(_Strict):
    disconnected: bool


class ModelAddModelParams(_Strict):
    slug: str
    model: str
    session_id: str | None = None


class ModelAddModelResult(_Strict):
    provider: ModelOptionProvider


class ModelRemoveModelParams(_Strict):
    slug: str
    model: str
    session_id: str | None = None


class ModelRemoveModelResult(_Strict):
    provider: ModelOptionProvider


class ProviderEndpointInfo(_Strict):
    """One of a provider section's endpoints, as the picker shows it."""

    label: str
    api_key: str = Field(..., description="Redacted for display: `****set****` or `(empty)`.")
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None


class ModelEndpointsParams(_Strict):
    slug: str
    session_id: str | None = None


class ModelEndpointsResult(_Strict):
    endpoints: list[ProviderEndpointInfo]


class ModelAddEndpointParams(_Strict):
    slug: str
    label: str = Field(..., description="Idempotency key: an existing entry with this label is replaced wholesale.")
    api_key: str = ""
    api_base: str | None = None
    session_id: str | None = None


class ModelAddEndpointResult(_Strict):
    endpoints: list[ProviderEndpointInfo]


class ModelRemoveEndpointParams(_Strict):
    slug: str
    label: str
    session_id: str | None = None


class ModelRemoveEndpointResult(_Strict):
    endpoints: list[ProviderEndpointInfo]


# ---------------------------------------------------------------------------
# config.* methods
# ---------------------------------------------------------------------------


class ConfigGetParams(_Strict):
    keys: list[str] | None = Field(
        default=None,
        description=("If omitted, return all whitelisted fields. Unknown keys are silently dropped."),
    )


class ConfigGetResult(_Strict):
    config: dict[str, JsonValue]


class ConfigSetParams(_Strict):
    key: str
    value: JsonValue


class ConfigSetResult(_Strict):
    applied: bool
    # ``previous`` is a *required* field whose value may legitimately be
    # ``null``.  We type it as ``JsonValue`` (``Any``) because ``JsonValue``
    # already includes ``null``; the schema's redundant ``oneOf: [JsonValue,
    # null]`` collapses to the same canonical "any" form.
    previous: JsonValue = Field(...)


# ---------------------------------------------------------------------------
# system.* methods
# ---------------------------------------------------------------------------


class SystemHelloParams(_Strict):
    client_version: str
    client_capabilities: list[str] | None = None


class SystemHelloSession(_Strict):
    default_channel: Literal["tui"]
    default_session_key: str


class SystemHelloResult(_Strict):
    server_version: str
    server_capabilities: list[str]
    session: SystemHelloSession


class SystemPingParams(_Strict):
    pass


class SystemPingResult(_Strict):
    pong: Literal[True]
    server_time_ms: float


class SystemVersionParams(_Strict):
    pass


class SystemVersionResult(_Strict):
    server_version: str
    schema_version: str = Field(..., description="OpenRPC info.version mirrored back to client.")
    raven_version: str


# ---------------------------------------------------------------------------
# cli.dispatch
# ---------------------------------------------------------------------------


class CliDispatchParams(_Strict):
    argv: list[str] = Field(..., description="Pre-tokenized argv (TUI side has already shlex-split).")
    width: int = Field(
        ...,
        ge=20,
        le=500,
        description="Ink container width in cells; required for Rich Console wrapping.",
    )
    timeout_s: float | None = Field(
        default=None,
        description="Override the default 30s timeout for long-running commands.",
    )


CliDispatchResult = CliResult


# ---------------------------------------------------------------------------
# setup.status / reload.mcp
# ---------------------------------------------------------------------------


class SetupStatusParams(_Strict):
    pass


class SetupStatusResult(_Strict):
    provider_configured: bool


class ReloadMcpParams(_Strict):
    pass


class ReloadMcpResult(_Strict):
    ok: bool
    reloaded: int | None = None
    tools_changed: bool | None = None


# ---------------------------------------------------------------------------
# commands.catalog (dynamic Typer-reflection slash catalog)
# ---------------------------------------------------------------------------


class CommandsCatalogParams(_Strict):
    pass


class CommandsCatalogResponse(_Strict):
    """Slash-command catalog reflected from raven.cli.commands.app.

    Shape consumed by ui-tui createSlashHandler.ts:53-79 (alias / prefix-1 /
    multi-match) and createGatewayEventHandler.ts:198 (gating on non-empty
    pairs). v0.1 emits alias=canonical 1:1; TS-side prefix-1-match handles
    partials.
    """

    canon: dict[str, str] = Field(
        ...,
        description=(
            "alias (with leading /) -> canonical mapping. Group + subcommand space-separated (e.g. '/channels status')."
        ),
    )
    pairs: list[tuple[str, str]] = Field(
        ...,
        description=("Ordered (alias, canonical) tuples. Empty pairs -> TS degrades catalog setup; gating field."),
    )
    sub: dict[str, list[str]] = Field(..., description="group -> [subcommand]; blacklisted entries filtered out.")
    categories: list[str] = Field(
        ...,
        description="'(top-level)' first then alphabetical group names.",
    )
    skill_count: int = Field(
        ...,
        ge=0,
        description="Total skill count via skill_forge.store; 0 + warning if DB missing.",
    )
    warning: str | None = Field(
        default=None,
        description="Optional warning pushed to TUI activity strip.",
    )


# ---------------------------------------------------------------------------
# hermes-only stubs (10 methods, all share StubResult)
# ---------------------------------------------------------------------------


class VoiceToggleParams(_Strict):
    action: str | None = None


VoiceToggleResult = StubResult


class BrowserManageParams(_Strict):
    action: str | None = None
    url: str | None = None


BrowserManageResult = StubResult


class SpawnTreeSaveParams(_Strict):
    name: str | None = None


SpawnTreeSaveResult = StubResult


class SpawnTreeListParams(_Strict):
    pass


SpawnTreeListResult = StubResult


class SpawnTreeLoadParams(_Strict):
    name: str | None = None


SpawnTreeLoadResult = StubResult


class ProcessStopParams(_Strict):
    pass


ProcessStopResult = StubResult


class RollbackListParams(_Strict):
    pass


RollbackListResult = StubResult


class RollbackDiffParams(_Strict):
    id: str | None = None


RollbackDiffResult = StubResult


class RollbackRestoreParams(_Strict):
    id: str | None = None


RollbackRestoreResult = StubResult


class ToolsConfigureParams(_Strict):
    pass


ToolsConfigureResult = StubResult


# ---------------------------------------------------------------------------
# subagents.* methods
# ---------------------------------------------------------------------------


class SubagentsListParams(_Strict):
    probe: bool = Field(
        default=True,
        description="False skips the network availability probe; rows report probe_status='unknown'.",
    )


class SubagentsListResult(_Strict):
    rows: list[SubagentRow]


class SubagentsAddParams(_Strict):
    preset: str
    name: str | None = None
    description: str | None = None
    api_key: str | None = None


class SubagentsAddResult(_Strict):
    added: bool
    name: str


class SubagentsUpdateParams(_Strict):
    name: str
    new_name: str | None = None
    description: str | None = None
    api_key: str | None = None


class SubagentsUpdateResult(_Strict):
    updated: bool
    name: str


class SubagentsRemoveParams(_Strict):
    name: str


class SubagentsRemoveResult(_Strict):
    removed: bool


class SubagentsToggleParams(_Strict):
    name: str
    enabled: bool


class SubagentsToggleResult(_Strict):
    enabled: bool


class SubagentsProbeParams(_Strict):
    pass


class SubagentsProbeResult(_Strict):
    rows: list[SubagentRow]


class SubagentsTestParams(_Strict):
    name: str
    source: Literal["config", "preset"] = "config"


class SubagentsTestResult(_Strict):
    ok: bool
    detail: str
    elapsed_ms: int
    reply: str | None = None
    cancelled: bool = False


class SubagentsTestCancelParams(_Strict):
    name: str


class SubagentsTestCancelResult(_Strict):
    cancelled: bool


# ---------------------------------------------------------------------------
# Method registry — used by tests/test_rpc_schema_match.py to walk every
# method and compare its Pydantic Params/Result models against the OpenRPC
# schema.  Keys MUST match the ``method.name`` strings in openrpc.json.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# skillhub.* methods
# ---------------------------------------------------------------------------


class SkillhubSearchParams(_Strict):
    query: str = Field("", description="Natural-language search; empty browses the hub.")
    category: str = Field("", description="Category enum, e.g. DEV / TESTING / DOC-PROC.")
    tags: str = Field("", description="Comma-separated tags, intersected.")
    min_score: float | None = Field(None, ge=0.0, le=1.0)
    page: int = Field(1, ge=1)
    limit: int = Field(24, ge=1, le=50)


class SkillhubDetailParams(_Strict):
    id: str = Field(..., description="Hub UUID or dataset skill_id.")


class SkillhubInstallParams(_Strict):
    id: str


class SkillhubRemoveParams(_Strict):
    name: str = Field(..., description="Installed skill directory name.")


# ---------------------------------------------------------------------------
# plughub.* / plug.* — the plugin market
# ---------------------------------------------------------------------------


class McpSnapshot(_Strict):
    """One server's live connection state, as `MCPConnectionManager` reports it.

    Every mutating `plug.*` call answers with this, and the gateway broadcasts the
    same shape as an `mcp.status` notification, so a client renders one state
    machine rather than two.
    """

    name: str
    transport: str = Field(..., description="stdio | sse | streamableHttp, or 'unknown'.")
    state: Literal["disconnected", "connecting", "connected", "auth_required", "error"]
    connected: bool
    tool_count: int
    error: str | None = None
    enabled: bool


class PlughubCatalogItem(_Strict):
    """The card-sized projection of a catalogue entry."""

    id: str
    version: str = ""
    name: str
    summary: str
    category: str
    verified: bool
    publisher: str
    risk_tier: int
    auth_mode: Literal["none", "apikey", "oauth"]
    transport: str | None = Field(default=None, description="None when the entry contributes no MCP server.")
    tool_preview_count: int
    skill_count: int
    kinds: list[str] = Field(..., description="Which contribution kinds the entry carries: mcp, skill, python.")
    installed: bool


class PlughubSearchParams(_Strict):
    q: str = ""
    category: str = ""


class PlughubSearchResult(_Strict):
    items: list[PlughubCatalogItem]
    categories: list[str]


class PlughubDetailParams(_Strict):
    id: str


class PlughubDetailResult(_Strict):
    # The raw catalogue entry, whose shape the catalogue owns (contributes[],
    # auth.fields[], tools_preview[], i18n name/summary objects). Typing it here
    # would put a second, weaker definition of the catalogue format in the
    # contract, and the first client to trust it over catalog.json would be wrong.
    item: dict[str, Any]
    installed: bool


class PlugInstallParams(_Strict):
    id: str
    form: dict[str, str] = Field(default_factory=dict, description="Values for the entry's auth.fields, by key.")


class PlugLedger(_Strict):
    """What the install actually landed, which is what uninstall replays."""

    catalog_id: str
    pieces: list[dict[str, Any]] = Field(
        ..., description="One entry per landed piece: {kind: 'mcp', server} or {kind: 'skill', name, skillhub_id}."
    )


class PlugInstallResult(_Strict):
    installed: bool = Field(..., description="False while an auth flow is still open; see `pending`.")
    pending: bool = Field(
        ..., description="True when the browser round-trip has not settled inside the connect window."
    )
    ledger: PlugLedger
    mcp: McpSnapshot | None = Field(default=None, description="None when no agent loop is running.")


class PlugRemoveParams(_Strict):
    name: str


class PlugRemoveResult(_Strict):
    removed: bool
    origin: Literal["market", "manual"] = Field(
        ..., description="'market' when a ledger drove the removal, 'manual' for a hand-written server."
    )


class PlugToggleParams(_Strict):
    name: str
    enabled: bool


class PlugToggleResult(_Strict):
    name: str
    enabled: bool
    mcp: McpSnapshot | None = None


class PlugAuthParams(_Strict):
    name: str


class PlugAuthResult(_Strict):
    name: str
    mcp: McpSnapshot | None = None


# ---------------------------------------------------------------------------
# skillhub.* results (the params models are above)
# ---------------------------------------------------------------------------


class SkillhubItem(_Strict):
    id: str
    skill_id: str
    name: str
    description: str
    source: str
    source_url: str
    category: str
    quality_score: float
    install_count: int
    github_star: int
    license: str
    tags: list[str]
    installed: bool
    installed_name: str = Field(..., description="The local directory name when installed, else empty.")


class SkillhubSearchResult(_Strict):
    items: list[SkillhubItem]
    total: int
    page: int
    limit: int
    base_url: str


class SkillhubSubscores(_Strict):
    utility: int
    robustness: int
    safety: int
    flags: list[str]


class SkillhubDetailResult(SkillhubItem):
    files: list[str]
    skill_md: str
    body_tokens: int
    subscores: SkillhubSubscores


class SkillhubInstallResult(_Strict):
    name: str
    path: str
    files: list[str]
    skipped: list[str] = Field(..., description="Members the suffix/size policy refused, so the gap is visible.")
    replaced: bool
    size_bytes: int
    install_count: int


class SkillhubRemoveResult(_Strict):
    removed: bool
    name: str


# ---------------------------------------------------------------------------
# The session init bundle.
#
# ``session.create`` / ``session.resume`` / ``session.compress`` all hand back
# the same three pieces: the id, the banner ``info``, and the transcript. The
# models live here rather than beside the session methods above because
# ``compress`` returns them too, and one definition is what makes a client able
# to redraw from any of the three.
# ---------------------------------------------------------------------------


class SessionUsage(_Strict):
    """``info.usage`` — the boot baseline, refreshed by each turn's completion.

    Distinct from :class:`UsageSnapshot`, which is the per-turn event payload:
    this one carries the context-window fill a banner draws, and its counters
    are named for the session rather than for one LLM call.
    """

    input: int
    output: int
    cost_usd: float
    calls: int
    context_max: int
    context_used: int
    context_percent: int
    context_estimated: bool | None = Field(
        default=None,
        description="True when context_used is a tiktoken estimate of a resumed transcript, not a measurement.",
    )


class SessionInitInfo(_Strict):
    """The banner bundle: which model, which tools and skills, how full."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str
    model_id: str
    provider: str
    context_window: int
    lazy: bool = Field(..., description="True when no agent loop was running, so tools/skills are empty.")
    skills: dict[str, list[str]] = Field(..., description="Skill names grouped by source.")
    tools: dict[str, list[str]] = Field(..., description="Tool names in a single 'builtin' bucket.")
    usage: SessionUsage
    version: str
    cwd: str
    mcp_servers: list[JsonValue]
    update_available: bool | None = None
    update_command: str | None = Field(default=None, description="The command that would install the newer release.")
    endpoint: str | None = Field(
        default=None,
        description="Which of a multi-endpoint provider's endpoints this session is on; null for single-endpoint ones.",
    )


class TranscriptToolCall(_Strict):
    id: str
    name: str
    arguments: str = Field(..., description="JSON-encoded arguments; re-serialized when stored as an object.")


class TranscriptMessage(_Strict):
    """One stored message in wire form: ``content`` renamed to ``text``."""

    role: str
    text: str | None = None
    context: JsonValue = None
    name: str | None = None
    tool_call_id: str | None = None
    timestamp: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[TranscriptToolCall] | None = None


class SessionCloseParams(_Strict):
    session_id: str | None = Field(default=None, description="Absent or unknown is a no-op.")


class SessionCloseResult(_Strict):
    ok: bool


class SessionBranchParams(_Strict):
    session_id: str | None = None
    name: str | None = Field(default=None, description="Title for the child session.")


class SessionBranchResult(_Strict):
    """``session_id`` is null when the source was unknown or empty, which the
    caller treats as a no-op rather than an error."""

    session_id: str | None = None
    title: str | None = None
    message_count: int | None = None


class SessionCompressSummary(_Strict):
    headline: str
    noop: bool = Field(..., description="True when nothing moved, including when the context engine owns compaction.")
    note: str | None = None
    token_line: str | None = None


class SessionCompressParams(_Strict):
    session_id: str
    focus_topic: str | None = None


class SessionCompressResult(_Strict):
    """The three redraw fields ride along only when something was archived: a
    caller that just dropped half the transcript is looking at messages that no
    longer exist."""

    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    removed: int
    summary: SessionCompressSummary
    info: SessionInitInfo | None = None
    messages: list[TranscriptMessage] | None = None
    usage: SessionUsage | None = None


class SessionStatusParams(_Strict):
    session_id: str | None = None


class SessionStatusResult(_Strict):
    output: str = Field(..., description="Rich-rendered `raven status` output with ANSI SGR sequences.")


# ---------------------------------------------------------------------------
# The console surface: ext.list / cron.* / settings.* / channels.status / fs.*
# ---------------------------------------------------------------------------


class ExtSkillRow(_Strict):
    name: str
    description: str
    source: str
    always: bool
    hub: bool = Field(..., description="Installed from the skill hub, so skillhub.remove can uninstall it.")
    hub_id: str


class ExtPluginRow(_Strict):
    id: str
    display_name: str
    version: str
    enabled: bool
    bundled: bool


class ExtToolRow(_Strict):
    name: str
    description: str
    enabled: bool
    mcp_server: str | None = Field(default=None, description="Owning MCP server, or null for a built-in tool.")


class ExtListParams(_Strict):
    pass


class ExtListResult(_Strict):
    skills: list[ExtSkillRow]
    plugins: list[ExtPluginRow]
    tools: list[ExtToolRow]
    mcp: list[McpSnapshot] = Field(
        ...,
        description="Live connections, plus configured servers not yet connected, reported as disconnected.",
    )


class CronJobInfo(_Strict):
    id: str
    name: str
    enabled: bool
    kind: Literal["at", "every", "cron"]
    expr: str | None = None
    every_ms: int | None = None
    at_ms: int | None = None
    tz: str | None = None
    message: str
    deliver: bool
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


class CronListParams(_Strict):
    pass


class CronListResult(_Strict):
    jobs: list[CronJobInfo] = Field(..., description="Disabled jobs included.")


class CronSaveParams(_Strict):
    """One shape for all three schedule kinds; which optional field is required
    follows from ``kind``."""

    kind: Literal["at", "every", "cron"]
    name: str
    message: str
    expr: str | None = None
    every_seconds: int | None = None
    at_iso: str | None = None
    tz: str | None = None
    deliver: bool | None = None
    id: str | None = Field(
        default=None,
        description="Editing an existing job: the id is kept so its run history does not orphan.",
    )


class CronSaveResult(_Strict):
    job: CronJobInfo


class CronDeleteParams(_Strict):
    id: str


class CronDeleteResult(_Strict):
    deleted: bool


class CronSetEnabledParams(_Strict):
    id: str
    enabled: bool


class CronSetEnabledResult(_Strict):
    enabled: bool


class CronRunNowParams(_Strict):
    id: str


class CronRunNowResult(_Strict):
    ok: bool


class CronRun(_Strict):
    at_ms: int | None = None
    ok: bool
    preview: str


class CronRunsParams(_Strict):
    id: str


class CronRunsResult(_Strict):
    runs: list[CronRun] = Field(..., description="Newest first, capped at 50.")
    session_id: str = Field(..., description="The cron:<id> session the history is derived from.")


class SettingsGetParams(_Strict):
    pass


class SettingsGetResult(_Strict):
    settings: dict[str, JsonValue] = Field(..., description="Raw config.json with secret-looking values masked.")
    config_path: str
    raven_version: str


class SettingsSetParams(_Strict):
    key: str = Field(..., description="Dotted path; only whitelisted keys are writable through this method.")
    value: JsonValue


class SettingsSetResult(_Strict):
    applied: bool
    previous: JsonValue


class ApiUsageTotals(_Strict):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float


class ApiUsageModel(ApiUsageTotals):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str


class LlmUsage(_Strict):
    total: ApiUsageTotals
    models: list[ApiUsageModel] = Field(..., description="Most expensive first.")


class ToolUsageCount(_Strict):
    name: str
    count: int


class ToolUsage(_Strict):
    total: int
    counts: list[ToolUsageCount] = Field(..., description="Most called first.")


class SettingsUsageParams(_Strict):
    days: int | None = Field(default=None, description="Window to scan; 30 by default, capped at 90.")


class SettingsUsageResult(_Strict):
    days: int
    llm: LlmUsage
    tools: ToolUsage


class EverosSection(_Strict):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str = Field(..., description="Empty when the shipped placeholder is still in place.")
    base_url: str
    provider: str
    api_key_set: bool = Field(..., description="Whether a key is stored; the value never goes on the wire.")


class SettingsEverosParams(_Strict):
    pass


class SettingsEverosResult(_Strict):
    sections: dict[str, EverosSection]
    config_path: str


class SettingsEverosSetParams(_Strict):
    section: str
    fields: dict[str, str] | None = Field(default=None, description="Merged into the section; ignored when clearing.")
    clear: bool | None = Field(default=None, description="Drop the section; refused for llm and embedding.")


class SettingsEverosSetResult(_Strict):
    applied: bool


class ChannelStatusRow(_Strict):
    name: str
    enabled: bool
    configured: bool
    missing: list[str] = Field(..., description="Required fields still empty.")


class ChannelsStatusParams(_Strict):
    pass


class ChannelsStatusResult(_Strict):
    channels: list[ChannelStatusRow]
    gateway_running: bool


class FsEntry(_Strict):
    name: str
    dir: bool
    size: int = Field(..., description="Zero for a directory.")


class FsListParams(_Strict):
    path: str | None = Field(default=None, description="Workspace-relative; the root when omitted.")


class FsListResult(_Strict):
    root: str
    path: str
    entries: list[FsEntry] = Field(..., description="Directories first, dotfiles omitted, capped at 500.")


class FsReadParams(_Strict):
    path: str
    max_bytes: int | None = None


class FsReadResult(_Strict):
    content: str = Field(..., description="Decoded as UTF-8 with replacement, so binary never fails the call.")
    truncated: bool
    size: int = Field(..., description="Size on disk, which exceeds len(content) when truncated.")


class FsUploadParams(_Strict):
    name: str
    content_b64: str


class FsUploadResult(_Strict):
    path: str = Field(..., description="Workspace-relative path to hand the agent; uploads never return bytes.")
    abs_path: str
    size: int


# ---------------------------------------------------------------------------
# memory.* — the EverOS long-term memory browser
# ---------------------------------------------------------------------------


MemoryKind = Literal["episode", "profile", "agent_case", "agent_skill"]


class MemoryStatsParams(_Strict):
    pass


class MemoryStatsResult(_Strict):
    """``ok`` is false when a kind could not be counted; the counts stay zero
    rather than the call failing, so the page opens with EverOS down."""

    ok: bool
    base_url: str
    episodes: int
    profiles: int
    agent_cases: int
    agent_skills: int


class MemoryItem(_Strict):
    """One row, projected card-sized. ``kind`` decides which optional fields
    carry a value: the four memory types share only ``id`` and ``kind``."""

    id: str
    kind: MemoryKind
    score: float | None = Field(default=None, description="Present on search hits only.")
    session_id: str | None = None
    timestamp: str | None = None
    subject: str | None = None
    summary: str | None = None
    body: str | None = None
    profile_data: dict[str, JsonValue] | None = None
    key_insight: str | None = None
    quality_score: float | None = None
    confidence: float | None = None
    maturity_score: float | None = None


class MemoryListParams(_Strict):
    kind: MemoryKind
    page: int | None = None
    page_size: int | None = Field(default=None, description="Capped at 100.")
    q: str | None = Field(default=None, description="Non-empty switches to search, which returns one page.")


class MemoryListResult(_Strict):
    items: list[MemoryItem]
    total: int
    page: int
    page_size: int


class MemoryDeleteParams(_Strict):
    kind: MemoryKind
    id: str


class MemoryDeleteResult(_Strict):
    ok: bool
    removed: int = Field(..., description="Deleting an episode also drops its derived facts and foresight.")


# ---------------------------------------------------------------------------
# The round-trip answer sinks, slash routing, and the rest
# ---------------------------------------------------------------------------


class ApprovalRespondParams(_Strict):
    """Both identities travel so a delayed answer cannot resolve a newer request
    that happens to show the same command."""

    approval_id: str
    choice: str = Field(..., description="allow | deny.")
    session_id: str | None = None
    conversation_id: str | None = Field(default=None, description="Compatibility spelling of session_id.")


class ApprovalRespondResult(_Strict):
    ok: bool = Field(..., description="False for an unknown, expired or mis-bound request; the caller fails closed.")


class ClarifyRespondParams(_Strict):
    answer: str
    request_id: str | None = None
    conversation_id: str | None = None


class ClarifyRespondResult(_Strict):
    ok: bool


class ConfirmRespondParams(_Strict):
    request_id: str
    answer: bool


class ConfirmRespondResult(_Strict):
    ok: bool


class CompleteSlashParams(_Strict):
    word: str | None = None
    session_id: str | None = None


class CompleteSlashResult(_Strict):
    """The provider is a no-op that exists to stop the client's
    completion-unavailable toast, so ``items`` is always empty and its element
    type is whatever a real provider would later return."""

    items: list[JsonValue]
    replace_from: int


class CompletePathParams(_Strict):
    word: str | None = None


class CompletePathResult(_Strict):
    items: list[JsonValue]


class SlashExecParams(_Strict):
    command: str = Field(..., description="The slash text without its leading slash; shlex-split into argv.")
    session_id: str | None = None


class SlashExecResult(_Strict):
    """Never an error frame: an unknown verb, a blacklisted one, a timeout and a
    non-zero exit all arrive here, because the client's error branch falls
    through to a method that does not exist."""

    output: str
    warning: str | None = None


class TerminalResizeParams(_Strict):
    cols: int | None = None
    rows: int | None = None
    session_id: str | None = Field(default=None, description="Sent by the client; the handler does not read it.")


class TerminalResizeResult(_Strict):
    ok: bool


class SystemUpgradeParams(_Strict):
    pass


class SystemUpgradeResult(_Strict):
    """Returned once the detached helper owns the install. The shutdown is
    scheduled a beat later so this reply reaches the client first."""

    status: str
    from_version: str
    to_version: str
    relaunch: bool = Field(..., description="Whether the helper will start `raven serve` again on the same port.")


# ---------------------------------------------------------------------------
# The remaining hermes-only stubs (-32012). Params are what ui-tui sends, which
# is the only reason to describe a call that cannot succeed: the client is
# typed against this contract whether the method works or not.
# ---------------------------------------------------------------------------


class VoiceRecordParams(_Strict):
    action: str | None = None
    session_id: str | None = None


class SessionSaveParams(_Strict):
    session_id: str | None = None


class SessionSteerParams(_Strict):
    session_id: str | None = None
    text: str | None = None


class SessionUsageParams(_Strict):
    session_id: str | None = None


class SkillsReloadParams(_Strict):
    pass


class ReloadEnvParams(_Strict):
    pass


class SudoRespondParams(_Strict):
    request_id: str | None = None
    password: str | None = None


class SecretRespondParams(_Strict):
    request_id: str | None = None
    value: str | None = None


class ImageAttachParams(_Strict):
    path: str | None = None
    session_id: str | None = None


class PromptSubmitParams(_Strict):
    session_id: str | None = None
    text: str | None = None


class PromptBackgroundParams(_Strict):
    session_id: str | None = None
    text: str | None = None


METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    # plughub.* / plug.* / skillhub.* — the market
    "plughub.search": (PlughubSearchParams, PlughubSearchResult),
    "plughub.detail": (PlughubDetailParams, PlughubDetailResult),
    "plug.install": (PlugInstallParams, PlugInstallResult),
    "plug.remove": (PlugRemoveParams, PlugRemoveResult),
    "plug.toggle": (PlugToggleParams, PlugToggleResult),
    "plug.auth": (PlugAuthParams, PlugAuthResult),
    "skillhub.search": (SkillhubSearchParams, SkillhubSearchResult),
    "skillhub.detail": (SkillhubDetailParams, SkillhubDetailResult),
    "skillhub.install": (SkillhubInstallParams, SkillhubInstallResult),
    "skillhub.remove": (SkillhubRemoveParams, SkillhubRemoveResult),
    # session.*
    "session.list": (SessionListParams, SessionListResult),
    "session.get": (SessionGetParams, SessionGetResult),
    "session.create": (SessionCreateParams, SessionCreateResult),
    "session.resume": (SessionResumeParams, SessionResumeResult),
    "session.delete": (SessionDeleteParams, SessionDeleteResult),
    "session.most_recent": (SessionMostRecentParams, SessionMostRecentResult),
    "session.title": (SessionTitleParams, SessionTitleResult),
    "session.clear": (SessionClearParams, SessionClearResult),
    "session.undo": (SessionUndoParams, SessionUndoResult),
    "session.export": (SessionExportParams, SessionExportResult),
    "session.history": (SessionHistoryParams, SessionHistoryResult),
    "session.close": (SessionCloseParams, SessionCloseResult),
    "session.branch": (SessionBranchParams, SessionBranchResult),
    "session.compress": (SessionCompressParams, SessionCompressResult),
    "session.status": (SessionStatusParams, SessionStatusResult),
    # ext.list / cron.* / settings.* / channels.status / fs.* -- the console
    "ext.list": (ExtListParams, ExtListResult),
    "cron.list": (CronListParams, CronListResult),
    "cron.save": (CronSaveParams, CronSaveResult),
    "cron.delete": (CronDeleteParams, CronDeleteResult),
    "cron.set_enabled": (CronSetEnabledParams, CronSetEnabledResult),
    "cron.run_now": (CronRunNowParams, CronRunNowResult),
    "cron.runs": (CronRunsParams, CronRunsResult),
    "settings.get": (SettingsGetParams, SettingsGetResult),
    "settings.set": (SettingsSetParams, SettingsSetResult),
    "settings.usage": (SettingsUsageParams, SettingsUsageResult),
    "settings.everos": (SettingsEverosParams, SettingsEverosResult),
    "settings.everosSet": (SettingsEverosSetParams, SettingsEverosSetResult),
    "channels.status": (ChannelsStatusParams, ChannelsStatusResult),
    "fs.list": (FsListParams, FsListResult),
    "fs.read": (FsReadParams, FsReadResult),
    "fs.upload": (FsUploadParams, FsUploadResult),
    # memory.*
    "memory.stats": (MemoryStatsParams, MemoryStatsResult),
    "memory.list": (MemoryListParams, MemoryListResult),
    "memory.delete": (MemoryDeleteParams, MemoryDeleteResult),
    # the round-trip answer sinks
    "approval.respond": (ApprovalRespondParams, ApprovalRespondResult),
    "clarify.respond": (ClarifyRespondParams, ClarifyRespondResult),
    "confirm.respond": (ConfirmRespondParams, ConfirmRespondResult),
    # slash routing and completion
    "slash.exec": (SlashExecParams, SlashExecResult),
    "complete.slash": (CompleteSlashParams, CompleteSlashResult),
    "complete.path": (CompletePathParams, CompletePathResult),
    "terminal.resize": (TerminalResizeParams, TerminalResizeResult),
    # turn.*
    "turn.send": (TurnSendParams, TurnSendResult),
    "turn.subscribe": (TurnSubscribeParams, TurnSubscribeResult),
    "turn.unsubscribe": (TurnUnsubscribeParams, TurnUnsubscribeResult),
    "turn.cancel": (TurnCancelParams, TurnCancelResult),
    # mcp.*
    "mcp.list": (McpListParams, McpListResult),
    "mcp.test": (McpTestParams, McpTestResult),
    "mcp.tools": (McpToolsParams, McpToolsResult),
    # skill.*
    "skill.list": (SkillListParams, SkillListResult),
    "skill.pin": (SkillPinParams, SkillPinResult),
    "skill.unpin": (SkillUnpinParams, SkillUnpinResult),
    # model.*
    "model.options": (ModelOptionsParams, ModelOptionsResult),
    "model.save_key": (ModelSaveKeyParams, ModelSaveKeyResult),
    "model.disconnect": (ModelDisconnectParams, ModelDisconnectResult),
    "model.add_model": (ModelAddModelParams, ModelAddModelResult),
    "model.remove_model": (ModelRemoveModelParams, ModelRemoveModelResult),
    "model.endpoints": (ModelEndpointsParams, ModelEndpointsResult),
    "model.add_endpoint": (ModelAddEndpointParams, ModelAddEndpointResult),
    "model.remove_endpoint": (ModelRemoveEndpointParams, ModelRemoveEndpointResult),
    # config.*
    "config.get": (ConfigGetParams, ConfigGetResult),
    "config.set": (ConfigSetParams, ConfigSetResult),
    # subagents.*
    "subagents.list": (SubagentsListParams, SubagentsListResult),
    "subagents.add": (SubagentsAddParams, SubagentsAddResult),
    "subagents.update": (SubagentsUpdateParams, SubagentsUpdateResult),
    "subagents.remove": (SubagentsRemoveParams, SubagentsRemoveResult),
    "subagents.toggle": (SubagentsToggleParams, SubagentsToggleResult),
    "subagents.probe": (SubagentsProbeParams, SubagentsProbeResult),
    "subagents.test": (SubagentsTestParams, SubagentsTestResult),
    "subagents.test_cancel": (SubagentsTestCancelParams, SubagentsTestCancelResult),
    # system.*
    "system.hello": (SystemHelloParams, SystemHelloResult),
    "system.ping": (SystemPingParams, SystemPingResult),
    "system.version": (SystemVersionParams, SystemVersionResult),
    "system.upgrade": (SystemUpgradeParams, SystemUpgradeResult),
    # cli.* / setup.* / reload.* / commands.*
    "cli.dispatch": (CliDispatchParams, CliResult),
    "setup.status": (SetupStatusParams, SetupStatusResult),
    "reload.mcp": (ReloadMcpParams, ReloadMcpResult),
    "commands.catalog": (CommandsCatalogParams, CommandsCatalogResponse),
    # hermes-only stubs
    "voice.toggle": (VoiceToggleParams, StubResult),
    "browser.manage": (BrowserManageParams, StubResult),
    "spawn_tree.save": (SpawnTreeSaveParams, StubResult),
    "spawn_tree.list": (SpawnTreeListParams, StubResult),
    "spawn_tree.load": (SpawnTreeLoadParams, StubResult),
    "process.stop": (ProcessStopParams, StubResult),
    "rollback.list": (RollbackListParams, StubResult),
    "rollback.diff": (RollbackDiffParams, StubResult),
    "rollback.restore": (RollbackRestoreParams, StubResult),
    "tools.configure": (ToolsConfigureParams, StubResult),
    "voice.record": (VoiceRecordParams, StubResult),
    "session.save": (SessionSaveParams, StubResult),
    "session.steer": (SessionSteerParams, StubResult),
    "session.usage": (SessionUsageParams, StubResult),
    "skills.reload": (SkillsReloadParams, StubResult),
    "reload.env": (ReloadEnvParams, StubResult),
    "sudo.respond": (SudoRespondParams, StubResult),
    "secret.respond": (SecretRespondParams, StubResult),
    "image.attach": (ImageAttachParams, StubResult),
    "prompt.submit": (PromptSubmitParams, StubResult),
    "prompt.background": (PromptBackgroundParams, StubResult),
    # dag.*
    "dag.get": (DagGetParams, DagGetResult),
    "dag.node": (DagNodeParams, DagNodeResult),
}

__all__ = [
    # public types
    "SessionInfo",
    "SessionListItem",
    "SessionMessage",
    "McpServerInfo",
    "McpToolInfo",
    "SkillInfo",
    "SubagentRow",
    "ModelOptionProvider",
    "ProviderEndpointInfo",
    "UsageSnapshot",
    "CliResult",
    "StubResult",
    "CommandsCatalogResponse",
    "TurnEvent",
    "SessionMostRecentParams",
    "SessionMostRecentResult",
    "SessionTitleParams",
    "SessionTitleResult",
    "SessionClearParams",
    "SessionClearResult",
    "SessionUndoParams",
    "SessionUndoResult",
    "SessionExportParams",
    "SessionExportResult",
    "MessageStartEvent",
    "EpisodeStartEvent",
    "TokenDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolStartEvent",
    "ToolProgressEvent",
    "ToolCompleteEvent",
    "MessageCompleteEvent",
    "ErrorEvent",
    "CronDeliveredEvent",
    "CronDeliveredPayload",
    "DagRunStartedEvent",
    "DagRunStartedPayload",
    "DagNodeUpdatedEvent",
    "DagNodeUpdatedPayload",
    "DagRunCompletedEvent",
    "DagRunCompletedPayload",
    "DagGetParams",
    "DagGetResult",
    "DagNodeParams",
    "DagNodeResult",
    "DagRunSnapshot",
    "DagNodeDetail",
    # market
    "McpSnapshot",
    "PlugAuthParams",
    "PlugAuthResult",
    "PlugInstallParams",
    "PlugInstallResult",
    "PlugLedger",
    "PlugRemoveParams",
    "PlugRemoveResult",
    "PlugToggleParams",
    "PlugToggleResult",
    "PlughubCatalogItem",
    "PlughubDetailParams",
    "PlughubDetailResult",
    "PlughubSearchParams",
    "PlughubSearchResult",
    # skillhub
    "SkillhubDetailParams",
    "SkillhubInstallParams",
    "SkillhubDetailResult",
    "SkillhubInstallResult",
    "SkillhubItem",
    "SkillhubRemoveParams",
    "SkillhubRemoveResult",
    "SkillhubSearchParams",
    "SkillhubSearchResult",
    "SkillhubSubscores",
    # registry
    "METHOD_MODELS",
]
