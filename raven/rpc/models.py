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


class FileChange(_Strict):
    """One file a tool call wrote, as contents rather than as a rendering of them.

    Beside ``ToolCompletePayload.diff`` rather than instead of it. A client that
    draws its own diff needs the text: a unified diff cannot be turned back into
    the file, its context is limited, and an oversized rewrite is dropped from it
    entirely.
    """

    path: str = Field(description="Absolute path of the file that was written.")
    after: str = Field(description="The file's full contents after the write.")
    before: str | None = Field(
        default=None,
        description=(
            "The contents the write replaced. Absent when the file did not exist, so a client "
            "renders a creation differently from a rewrite; an empty string means the file "
            "existed and was empty. This is why the field cannot use a falsy-means-absent "
            "shortcut -- an empty file has the same emptiness."
        ),
    )


class MediaItem(_Strict):
    """One file the agent produced as part of its reply, by local path."""

    path: str = Field(description="Absolute path of the file on the machine the agent runs on.")
    mime: str = Field(
        description=(
            "MIME type as declared by the emit site. Every producer declares "
            "application/octet-stream today, so a client that needs the real type should sniff "
            "the extension rather than trust this."
        )
    )
    kind: str = Field(description='Coarse media class; "file" is the only value emitted today.')


class MediaPayload(_Strict):
    items: list[MediaItem] = Field(
        description=(
            "The files, in the order the turn produced them. Never empty: an event with nothing "
            "to deliver is not emitted."
        )
    )


class MediaEvent(_Strict):
    """Files the reply carried, as paths rather than bytes.

    A separate event rather than a field on ``message.complete``: media is emitted
    before the reply text (the loop's own order) and a turn can produce it without
    producing text at all, so hanging it off the completion would reorder it and
    lose the text-free case.

    Paths and not contents because both ends of this wire are on one machine.
    """

    type: Literal["media"]
    payload: MediaPayload


class NoticePayload(_Strict):
    kind: str = Field(..., description="Which runtime decision this reports; `action_blocked` today.")
    detail: str = Field("", description="The blocking tool's own first line, when it gave one.")


class NoticeEvent(_Strict):
    """Prose the runtime wrote, not the model.

    It must not arrive as `token.delta`: that buffer is the model's voice, so
    the text would render as the answer -- glued to whatever the model narrated
    just before it, carrying the answer's copy and branch actions, and stuck in
    English whatever language the turn was in.
    """

    type: Literal["notice"]
    payload: NoticePayload


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
    blocking: bool = Field(
        default=False,
        description=(
            "The call is a blocking interaction, so it has no automatic deadline and may emit "
            "nothing for as long as it runs. A client that clocks the event stream for liveness "
            "must suspend that clock while it is in flight, or it declares a sub-agent run dead."
        ),
    )


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
    metadata: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "Opt-in structured payload a tool chose to publish (a file manifest, for example), "
            "straight off ToolResult.metadata. A client that does not understand a key ignores it."
        ),
    )
    diff: str | None = Field(
        default=None,
        description=(
            "Unified diff of what the call changed on disk, when the tool could produce one. "
            "The only record of what a whole-file write replaced: the arguments carry the new "
            "content and nothing else, so a client without this draws an overwrite as all additions."
        ),
    )
    file_change: FileChange | None = None


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
    turn_id: str = Field(
        "",
        description=(
            "The turn this failure belongs to. Empty when the emitter did not know it; "
            "a consumer that correlates a request to a turn must not treat an empty value as its own."
        ),
    )
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


class CronMissedItem(_Strict):
    name: str
    scheduled_at: str
    message: str


class CronMissedPayload(_Strict):
    count: int
    items: list[CronMissedItem]


class CronMissedEvent(_Strict):
    type: Literal["cron.missed"]
    payload: CronMissedPayload


TurnEvent = Annotated[
    Union[
        MessageStartEvent,
        EpisodeStartEvent,
        NoticeEvent,
        MediaEvent,
        TokenDeltaEvent,
        ThinkingDeltaEvent,
        ToolStartEvent,
        ToolProgressEvent,
        ToolCompleteEvent,
        MessageCompleteEvent,
        ErrorEvent,
        CronDeliveredEvent,
        CronMissedEvent,
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
    channel: str
    chat_id: str
    metadata: dict[str, JsonValue] | None = None


class SessionCreateResult(_Strict):
    session: SessionInfo


class SessionResumeParams(_Strict):
    session_key: str


class SessionResumeResult(_Strict):
    session: SessionInfo
    last_messages: list[SessionMessage]


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
    # Model-switch extras. ``scope`` decides the reach of a ``key="model"``
    # switch: this conversation, or the default a new one starts on.
    session_id: str | None = None
    provider: str | None = None
    scope: Literal["session", "default"] | None = None


class ConfigSetResult(_Strict):
    applied: bool
    # ``previous`` is a *required* field whose value may legitimately be
    # ``null``.  We type it as ``JsonValue`` (``Any``) because ``JsonValue``
    # already includes ``null``; the schema's redundant ``oneOf: [JsonValue,
    # null]`` collapses to the same canonical "any" form.
    previous: JsonValue = Field(...)
    # Present on a model switch: what was applied, and where it reached.
    value: str | None = None
    scope: Literal["session", "default"] | None = None
    session_id: str | None = None
    # Does the asking conversation now run this model? A default-scoped switch
    # moves the sessions that never chose one, so scope alone cannot answer it
    # and a client that guesses shows a model the conversation is not on.
    applies_to_session: bool | None = None


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
# Method registry — used by tests/test_rpc_schema_match.py to walk every
# method and compare its Pydantic Params/Result models against the OpenRPC
# schema.  Keys MUST match the ``method.name`` strings in openrpc.json.
# ---------------------------------------------------------------------------

METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
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
    # system.*
    "system.hello": (SystemHelloParams, SystemHelloResult),
    "system.ping": (SystemPingParams, SystemPingResult),
    "system.version": (SystemVersionParams, SystemVersionResult),
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
}

__all__ = [
    # public types
    "SessionInfo",
    "SessionListItem",
    "SessionMessage",
    "McpServerInfo",
    "McpToolInfo",
    "SkillInfo",
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
    "NoticeEvent",
    "FileChange",
    "MediaEvent",
    "MediaItem",
    "MediaPayload",
    "NoticePayload",
    "TokenDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolStartEvent",
    "ToolProgressEvent",
    "ToolCompleteEvent",
    "MessageCompleteEvent",
    "ErrorEvent",
    "CronDeliveredEvent",
    "CronDeliveredPayload",
    "CronMissedEvent",
    "CronMissedItem",
    "CronMissedPayload",
    # registry
    "METHOD_MODELS",
]
