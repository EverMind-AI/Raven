"""Pydantic v2 models for the client JSON-RPC contract.

These models are the Python-side mirror of ``rpc-schema/openrpc.json``.
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
    kind: Literal["builtin", "cli", "openai", "acp"]
    description: str
    enabled: bool
    configured: bool
    vendored: bool = Field(
        default=False,
        description=(
            "Discovered under `subagents/` rather than written into config: one of the raven "
            "builds that ship beside this one, materialized as a row on every table build. Like "
            "`builtin` it leaves `configured` false -- there is no config entry to delete, and "
            "removing it means removing its folder -- but unlike `builtin` it is a real "
            "subprocess with a command, so it is probed and it can be unready (an unbuilt venv "
            "leaves it listed and disabled). Absent from a server that predates discovery."
        ),
    )
    building: bool = Field(
        default=False,
        description=(
            "A vendored folder's venv is being built right now (`subagents.build`). Its own flag "
            "rather than `test_running`: a build and a test are different verbs on the same row, "
            "and one must not read as the other. Absent from a server that predates discovery."
        ),
    )
    stateful: bool = Field(
        default=False,
        description=(
            "Reusing a handle continues this agent's conversation rather than starting a fresh "
            "one (`agent_meta`). What makes a row direct-chattable at all: `chat` refuses a "
            "stateless agent, so a picker that offers one is offering a refusal. Absent from a "
            "server that predates this, which reads as 'not offered' rather than as an error."
        ),
    )
    builtin: bool = Field(
        default=False,
        description=(
            "A built-in agent: raven's own in-process loop, on the agent table whether or not "
            "config mentions it. Distinct from `configured`, which stays false for one -- not "
            "writing a row is how 'use the package's default' is spelled, so there is nothing "
            "to delete and no transport to connect. It takes no action at all: `subagents.toggle` "
            "answers `config_field_readonly` for it, because an unnamed spawn and a dag node with "
            "no sub-agent both dispatch to this row, so it cannot leave the roster. A client must "
            "not offer a switch, a test or a delete for it."
        ),
    )
    group: Literal["builtin", "installed", "uninstalled"]
    upgrade_to: str | None = Field(
        default=None,
        description=(
            "The transport this entry's preset has since moved to, or null when it is current. "
            "A configured entry is never rewritten underneath the user, so the mismatch is shown instead."
        ),
    )
    probe_status: Literal["ready", "attention", "missing", "unknown"]
    probe_detail: str
    has_api_key: bool
    mcps: list[str]
    allow_mcp_secrets: bool
    last_test_ok: bool | None = None
    last_test_detail: str | None = None
    last_test_at_ms: int | None = None
    test_running: bool


class DirectTarget(_Strict):
    """One sub-agent instance a turn is addressed to, instead of the main agent.

    Null everywhere it appears means the main conversation. The pair is the
    instance's whole identity - the registry is keyed
    ``(session_key, agent, handle)`` and the session is already implied by the
    method that carries this - so nothing else belongs here.
    """

    agent: str
    handle: str


class InstanceRow(_Strict):
    """One sub-agent instance this session has used.

    camelCase field names, unlike every other model here, because a row is a
    registry record verbatim (``raven/agent/subagent/instances.py``) and the web
    RPC already serves it unchanged. Renaming the fields for this surface would
    make the TUI and the web UI disagree about what an instance is, which is the
    hardest class of bug to find later.

    ``runId`` / ``nodeId`` name the DAG node an instance belongs to. They are on
    a ``dag-node`` row and also on the ordinary row of a stateful node, which is
    what lets one invocation's two rows be reported as one.

    ``resumable`` is the exception to "verbatim": the registry does not store it,
    because whether a row can be talked to depends on the agent's configured
    backend rather than on anything in the record.

    The camelCase is carried by aliases rather than by the attribute names, so
    the wire keeps the registry's spelling while the Python side stays like
    every other model in this module.
    """

    session_key: str = Field(alias="sessionKey")
    agent: str
    handle: str
    kind: str
    status: str | None = None
    agent_id: str | None = Field(default=None, alias="agentId")
    run_id: str | None = Field(default=None, alias="runId")
    node_id: str | None = Field(default=None, alias="nodeId")
    created_at_ms: int | None = Field(default=None, alias="createdAtMs")
    updated_at_ms: int | None = Field(default=None, alias="updatedAtMs")
    resumable: bool | None = None
    title: str | None = Field(
        default=None,
        description=(
            "What this instance was asked, in one line: a graph node's node_summary, or a spawn's "
            "task_summary. Null for an instance nobody dispatched (one the user created by hand) and "
            "for work that ran before those fields existed; a reader falls back to the handle."
        ),
    )
    run_title: str | None = Field(
        default=None,
        alias="runTitle",
        description=(
            "What the graph this instance belongs to was dispatched for. Absent -- not empty -- for an "
            "instance that came from no orchestration, so its presence is what says the row has a source."
        ),
    )


class TranscriptToolCall(_Strict):
    id: str
    name: str
    arguments: str = Field(..., description="JSON-encoded arguments; re-serialized when stored as an object.")


class DirectTurn(_Strict):
    """One row of one instance's conversation.

    Preferred source is the instance's own log, which is written turn by turn and
    holds what the run did on the way -- hence the step fields below and the
    ``tool`` role. A conversation with no log falls back to its record
    directories, where a turn is a pair of files and flattens to two of these:
    the prompt as ``user`` and the reply as ``assistant``. A turn still running,
    or one that failed, has no reply and yields only the first.

    Every step field is optional because the lane that ran the turn decides
    whether there is one: the cli transport has no per-step visibility at all,
    and absent is not the same as none.
    """

    call_id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    at_ms: int
    prompt_path: str | None = None
    out_path: str | None = None
    reasoning_content: str | None = Field(
        default=None, description="The thought that preceded this turn, where the transport reports one."
    )
    tool_calls: list[TranscriptToolCall] = Field(
        default_factory=list,
        description="What this turn called on the way, matched to a later role='tool' row by id.",
    )
    tool_call_id: str | None = Field(default=None, description="On a role='tool' row, the call it answers.")
    live: bool | None = Field(
        default=None,
        description=(
            "Set on a row from a turn still running, which no record holds yet. Such rows are a "
            "snapshot: the next read replaces them, and the record replaces them once the turn lands."
        ),
    )
    steer: bool | None = Field(
        default=None,
        description=(
            "Set on a user row that was a steer: words merged into a turn already running, not the "
            "prompt that opened one. A reader draws it inside the turn rather than as a new one."
        ),
    )
    interrupted: bool | None = Field(
        default=None,
        description=(
            "Set on a trailing user row whose turn died with its session: nothing is running now and "
            "no reply ever landed. A reader marks the question as interrupted rather than leaving it hanging."
        ),
    )


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
    content: str = Field(
        "",
        description=(
            "The message that started the turn. Present so a client that did not send it "
            "can draw the question: the user entry reaches the transcript only at turn end."
        ),
    )
    target: DirectTarget | None = None
    """Which conversation this event belongs to; null is the main agent.

    Carried on the four events a direct-chat turn can produce
    (``message.start`` / ``token.delta`` / ``message.complete`` / ``error``) --
    it emits exactly one reply and no tool or reasoning output, so no other
    variant needs the tag. A client cannot infer this from its own state: it can
    switch instances mid-turn or reconnect, and an untagged delta painted into
    the main transcript is the failure this prevents."""


class MessageStartEvent(_Strict):
    type: Literal["message.start"]
    payload: MessageStartPayload


class TurnStartedPayload(_Strict):
    turn_id: str
    target: DirectTarget | None = None


class TurnStartedEvent(_Strict):
    """A turn the runtime opened (a delegated result re-entering the
    conversation) has begun.

    ``turn.send`` owns ``message.start``, and the spine suppresses it for these
    turns -- so this event is the client's only live signal that a new turn
    started. It advances the client's live turn bookkeeping (the workspace
    record's turn number, which the artifact bar reads) and draws nothing: a
    stored delegated user entry advances the same counter on replay, which is
    what keeps the two views in step about which turn a file belongs to.
    """

    type: Literal["turn.started"]
    payload: TurnStartedPayload


class EpisodeStartPayload(_Strict):
    index: int


class EpisodeStartEvent(_Strict):
    type: Literal["episode.start"]
    payload: EpisodeStartPayload


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
    target: DirectTarget | None = None


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


class FileChange(_Strict):
    """One file a tool call wrote, as contents rather than as a rendering of them.

    Beside ``ToolCompletePayload.diff`` rather than instead of it. A client that
    draws its own diff needs the text: a unified diff cannot be turned back into
    the file, its context is limited, and an oversized rewrite is dropped from it
    entirely. The ACP surface needs exactly this shape -- its ``Diff`` content
    block is ``{path, newText, oldText}`` -- so the rendered string cannot serve
    it at all.
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


class ToolCompletePayload(_Strict):
    tool_call_id: str
    result_preview: str
    truncated: bool
    ok: bool = True
    """Whether the tool call succeeded, by the emit site's verdict. A client
    that draws a failure row differently needs this; the preview alone cannot
    be classified, because a tool that writes a friendly error message is
    indistinguishable from one that succeeded."""
    metadata: dict[str, JsonValue] | None = None
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
    target: DirectTarget | None = None
    duration_ms: int | None = Field(
        default=None,
        description=(
            "How long the whole turn took, measured server-side from the moment the runner "
            "picked the turn up to the moment it returned. Sent so a live client does not have "
            "to time the turn with its own clock: a browser stopwatch starts when the events "
            "arrive rather than when the work did, and only exists while that page is open, so "
            "the same turn came out one number live and another after a reload. Absent means "
            "unknown, same rule as reasoning_ms -- fall back to timing it locally, never to zero."
        ),
    )


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
    target: DirectTarget | None = None


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


class SubagentDeliveredPayload(_Strict):
    kind: Literal["spawn", "dag"]
    label: str = Field(..., description="The spawn's display label, or the dag's run_id.")
    status: Literal["ok", "error", "exception"]
    run_id: str | None = Field(default=None, description="Set for kind=dag, so a client can open the run.")
    node_id: str | None = Field(
        default=None,
        description="Set for a dag node's own message, so a client can place it against that row.",
    )
    content: str = Field(
        default="",
        description=(
            "The text that re-entered the conversation, verbatim. A client shows the "
            "reader-facing part of it by keeping only what sits INSIDE the untrusted "
            "fence -- and a client replaying this turn later reads the same string from "
            "the stored entry's `text`, so one rule over one input keeps the live view "
            "and the reloaded one from disagreeing about what was delivered."
        ),
    )


class SubagentDeliveredEvent(_Strict):
    """A delegated run's result re-entered its conversation here.

    Emitted just after the result is submitted to the main loop, so a client can
    say "this reply is the sub-agent's result coming back" at the exact point in
    the flow where that is true. The turn it opens goes through the spine, which
    emits no ``message.start``, so this event is the only LIVE signal that it
    happened -- and a client replaying the session later reads the same identity
    off the stored user entry's ``delegated`` field. Two readings of one fact,
    which is what keeps a reloaded transcript from drawing the injection as a
    question the user asked.
    """

    type: Literal["subagent.delivered"]
    payload: SubagentDeliveredPayload


SubagentRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class SubagentStatusPayload(_Strict):
    task_id: str = Field(..., description="The manager's short id for this run, stable across its lifecycle.")
    agent: str
    label: str
    status: SubagentRunStatus
    call_id: str | None = Field(
        default=None,
        description=(
            "The spawn record id `subagent.context` reads. Known from `running` onward; a "
            "`pending` run has not opened its record yet, so there is nothing to read."
        ),
    )
    tool_call_id: str | None = Field(
        default=None,
        description=(
            "The spawn tool call that dispatched this run, when the host correlates the two. "
            "What lets a client pin the run onto the tool row that made it, the same way "
            "dag.run_started names its call."
        ),
    )
    instance: str | None = Field(
        default=None,
        description="The addressable handle, when the caller named or minted one.",
    )
    started_at: int | None = None
    ended_at: int | None = None


class SubagentStatusEvent(_Strict):
    """One spawned run's lifecycle transition, as it happens.

    ``subagent.delivered`` marks where a finished result re-entered the
    conversation; this event is the run itself moving -- queued, started,
    finished -- so a client can render live delegation without polling the
    disk-backed lists. Terminal transitions are not replayed: a client that
    reconnects reconciles against ``subagent.list`` instead.
    """

    type: Literal["subagent.status"]
    payload: SubagentStatusPayload


DagNodeStatus = Literal["pending", "running", "completed", "failed", "skipped", "cancelled", "exception"]


class DagRunStartedNode(_Strict):
    id: str
    subagent: str
    depends_on: list[str]
    instance: str | None = None
    node_summary: str | None = None


class DagRunStartedPayload(_Strict):
    run_id: str
    tool_call_id: str | None = None
    task_summary: str | None = Field(
        default=None,
        description=(
            "What the whole graph was dispatched for, in one line. Absent for a run started before "
            "the field existed. Per-node the equivalent is DagRunStartedNode.node_summary."
        ),
    )
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
    cancelled: int | None = None


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
DagSnapshotNodeStatus = Literal[
    "pending", "running", "completed", "failed", "skipped", "cancelled", "interrupted", "exception"
]


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
    node_summary: str | None = None
    inputs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What this node was handed, per key: a literal string, {file: path}, or {node: id}. "
            "The other half of prompt_template -- a template's {{ inputs.k }} does not say where k came from."
        ),
    )


class DagTerminalOutput(_Strict):
    node: str
    text: str


class DagRunSnapshot(_Strict):
    run_id: str
    dir: str
    finalized: bool
    task_summary: str | None = Field(
        default=None,
        description=(
            "What the whole graph was dispatched for, in one line. Null for a run written before the "
            "field existed. Per-node the equivalent is DagSnapshotNode.node_summary."
        ),
    )
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
    status: DagNodeStatus | None = Field(
        default=None,
        description="This node's own last recorded status; null when the run has written no manifest yet.",
    )
    error: str | None = Field(
        default=None,
        description="Why the node failed. A failed node has no output, so without this it reads as unanswered.",
    )
    messages: list["TranscriptMessage"] = Field(
        default_factory=list,
        description=(
            "What the node was asked, what it did on the way where the transport could see it, "
            "and what it answered -- the shape subagent.context returns, so one renderer draws both."
        ),
    )


class DagGetParams(_Strict):
    run_id: str
    session_key: str | None = None


class DagGetResult(_Strict):
    run: DagRunSnapshot


class DagNodeParams(_Strict):
    run_id: str
    node: str
    max_output_chars: int | None = None
    session_key: str | None = Field(
        default=None,
        description=(
            "Which conversation's run to read, as `dag.get` already takes. The handler has "
            "always read it; a run directory lives inside its session, so a caller that omits "
            "it is asking the server to guess."
        ),
    )


class DagNodeResult(_Strict):
    node: DagNodeDetail


class CronMissedItem(_Strict):
    name: str
    scheduled_at: str
    message: str


class CronMissedPayload(_Strict):
    count: int
    items: list[CronMissedItem]


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

    Paths and not contents because both ends of this wire are on one machine --
    the terminal is a child process, and an ACP client spawns the agent itself.
    A client that is not local cannot use this event, which is the same
    constraint every other path-carrying field on this contract already has.
    """

    type: Literal["media"]
    payload: MediaPayload


class CronMissedEvent(_Strict):
    type: Literal["cron.missed"]
    payload: CronMissedPayload


class SessionTitledPayload(_Strict):
    session_id: str = Field(..., description="Full session_key the title belongs to.")
    title: str = Field(..., description="The generated title, already cleaned and clamped.")


class SessionTitledEvent(_Strict):
    """A session was named by the model that reads its opening message.

    Conversation-scoped rather than broadcast: one session being named is not
    news to a client watching a different one. Emitted only when the title
    actually changed, so a client can treat it as "replace what you are showing"
    without comparing.

    A client is not required to have asked for this. It arrives on the same
    subscription as the turn it was generated alongside, which is what lets a
    front end park a placeholder where the title goes and fill it in on arrival
    instead of showing a truncated first line and rewriting it a second later.
    """

    type: Literal["session.titled"]
    payload: SessionTitledPayload


class SessionNamingEndedPayload(_Strict):
    session_id: str = Field(..., description="Full session_key whose naming call has finished.")
    reason: Literal["timeout", "error", "no_title", "renamed"] = Field(
        ...,
        description=(
            "Why no title was published. 'timeout' the call outran its budget; 'no_title' it came "
            "back with nothing usable -- the "
            "model answered without calling the naming tool, or the provider failed and "
            "`generate_title` swallowed it (that one logs its cause at debug); 'error' the naming "
            "code itself raised, which is a bug in this seam rather than a model that had nothing "
            "to say; 'renamed' a person named the session while the call was running."
        ),
    )


class SessionNamingEndedEvent(_Strict):
    """The naming call for a session finished without publishing a title.

    The other half of ``session.titled``, and the reason it exists is latency
    rather than information: a client parks a placeholder where the title goes
    when ``turn.send`` answers ``naming: true``, and until this event there was
    no signal for any of the ways that call can end quietly. The only way to
    find out was to wait out a grace period, so a model that answered without
    calling the tool -- which happens, and takes about a second -- cost the
    reader the full wait before the row showed anything.

    Exactly one of ``session.titled`` and this event follows a turn whose
    ``turn.send`` reported ``naming: true``, so a client can stop waiting on
    either. ``reason`` is for the log and the bug report: a reader cannot act on
    the difference, but the person diagnosing "why is there no title" can, and
    the debug lines that used to be the only record are off by default.
    """

    type: Literal["session.naming_ended"]
    payload: SessionNamingEndedPayload


TurnEvent = Annotated[
    Union[
        MessageStartEvent,
        EpisodeStartEvent,
        NoticeEvent,
        TokenDeltaEvent,
        ThinkingDeltaEvent,
        ToolStartEvent,
        ToolProgressEvent,
        ToolCompleteEvent,
        MessageCompleteEvent,
        ErrorEvent,
        CronDeliveredEvent,
        SubagentDeliveredEvent,
        SubagentStatusEvent,
        DagRunStartedEvent,
        DagNodeUpdatedEvent,
        DagRunCompletedEvent,
        CronMissedEvent,
        MediaEvent,
        SessionTitledEvent,
        SessionNamingEndedEvent,
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
    preview: str = Field(..., description="First user message, used as the untitled-session identity fallback.")
    last_message_preview: str = Field(..., description="Latest non-empty user or assistant message text.")
    source: str | None = None
    started_at: float = Field(..., description="Unix timestamp from created_at.")
    updated_at: float = Field(..., description="Unix timestamp of the latest user or assistant message.")
    title: str
    pinned: bool = Field(default=False, description="User pinned this session to the top of the picker.")


class SessionListParams(_Strict):
    limit: int | None = Field(default=None, description="Max sessions to return.")
    channels: list[str] | None = Field(default=None, description="Session channels to include; defaults to tui.")


class SessionListResult(_Strict):
    sessions: list[SessionListItem]


class SessionGetParams(_Strict):
    session_key: str


class SessionGetResult(_Strict):
    session: SessionInfo


class SessionCreateParams(_Strict):
    cols: int | None = Field(default=None, description="Terminal width the client is drawing at.")
    title: str | None = Field(default=None, description="Accepted and ignored; clients set titles via session.title.")
    workdir: str | None = Field(
        default=None,
        description=(
            "Absolute directory this session's turns run in, persisted as the session's workdir override. "
            "How a client attached to a shared gateway keeps its launch directory."
        ),
    )


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


class SessionPinParams(_Strict):
    session_id: str = Field(..., description="Full session_key.")
    pinned: bool = Field(..., description="True pins the session to the top of the picker; False unpins.")


class SessionPinResult(_Strict):
    """pending=True means the flag is held in memory for a lazy (never-saved)
    session and lands with the session's first save — same contract as
    session.title."""

    pinned: bool
    session_key: str
    pending: bool


class SessionArchiveParams(_Strict):
    session_id: str = Field(..., description="Full session_key.")
    archived: bool = Field(..., description="True hides the session from session.list; False restores it.")


class SessionArchiveResult(_Strict):
    archived: bool
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
    # Absent means the main agent, which is what every existing caller sends.
    # With it set the turn skips the model entirely and runs one direct-chat
    # turn against that instance (``TurnRequest.direct_target``).
    target: DirectTarget | None = None


class TurnSendResult(_Strict):
    turn_id: str
    accepted: bool
    # Whether a session-naming call was started for this turn. It answers a
    # question a client otherwise has no way to ask, and without it the only way
    # to find out was to hold a placeholder until a grace period ran out --
    # which made the shortest openings, the ones refused in microseconds, the
    # slowest to show a name.
    #
    # Read it as what it says, not as "no title is coming": a send arriving
    # while an earlier namer for the same session is still in flight is also
    # declined, and that earlier call's title may still land. Settling early on
    # this is safe because the event overwrites whatever the row shows.
    naming: bool


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
    # Which lane of the session to cancel. Absent means the main agent, which is
    # what every caller sent while a direct turn was uncancellable. A direct turn
    # runs on its instance's own lane (``direct_lane``), so without this the
    # handler looks up a key nothing was ever registered under and reports
    # "nothing to cancel" for a turn that is plainly running.
    target: DirectTarget | None = None


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
    surface: str | None = Field(
        default=None,
        description=(
            "Which front end this connection is ('tui', 'page', 'shell'). Recorded per connection "
            "for tracing only; session keys and channels are unaffected."
        ),
    )


class SystemHelloSession(_Strict):
    default_channel: str = Field(
        ...,
        description="The channel this dispatcher's turns run on; the terminal and the served page share one.",
    )
    default_session_key: str


class SystemHelloResult(_Strict):
    server_version: str
    server_capabilities: list[str]
    session: SystemHelloSession
    platform: Literal["mac", "windows", "linux"] = Field(
        default="linux",
        description="The gateway host's OS family, so a client can word host-side actions (Finder vs Explorer).",
    )


class SystemPingParams(_Strict):
    pass


class SystemPingResult(_Strict):
    pong: Literal[True]
    server_time_ms: float


class SystemVersionParams(_Strict):
    check: bool = Field(
        default=False,
        description=(
            "Fetch the latest release now instead of answering from the daily cache. "
            "For the explicit check button: a person who just asked deserves a live answer."
        ),
    )


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
    """``confirm`` skips the confirmation gate for this call, and only this one.

    There is deliberately no "and stop asking": storing that needs a settings
    section this config does not have, and the flag that used to claim it
    (``always``) was never persisted -- the TUI announced it had been while the
    server logged that it had not.
    """

    session_id: str | None = None
    confirm: bool | None = None


class ReloadMcpResult(_Strict):
    """Every branch fills all five fields.

    ``status`` is what the caller acts on -- hermes ``ops.ts`` picks its line
    from it -- so a branch that omitted it would leave that client reading
    undefined and printing the fall-through text for a reload that failed. That
    was the shipped behaviour while this method was a stub: it never sent a
    status at all.
    """

    ok: bool
    status: Literal["reloaded", "noop", "confirm_required"]
    message: str
    reloaded: int
    tools_changed: bool


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
# subagent.* -- the background calls one conversation handed off
#
# Singular, and directly above the plural `subagents.*` that configures which
# external agents exist, because the two names are one keystroke apart and mean
# different things. These two read the on-disk record the delegation paths leave
# in the session's own directory -- a `spawn` call, and every node of a graph run.
# The graph is still read through `dag.*`, which has the structure this flat list
# does not; the list's job is "which agents worked on this conversation", and
# answering it with only half of them was worse than a little duplication.
# ---------------------------------------------------------------------------


class SubagentCall(_Strict):
    """One handed-off unit of work, as a row in the panel that lists them."""

    id: str = Field(
        ...,
        description=(
            "A spawn's call id, or '<run_id>/<node>' for a graph node. Opaque to a client: a spawn row is "
            "opened through subagent.context and a dag row through dag.node, which is what `kind` is for."
        ),
    )
    kind: str = Field(
        default="spawn",
        description="spawn | dag. Which of the two delegation paths made this, and so how it is opened.",
    )
    run_id: str | None = Field(default=None, description="dag rows only: the graph run this node belongs to.")
    node: str | None = Field(default=None, description="dag rows only: the node's id inside that run.")
    label: str = Field(..., description="What it was asked, in short. The prompt's first line when unlabelled.")
    status: str = Field(
        ...,
        description=(
            "run | ok | error | cancelled | queued | skipped. An aborted call reads as error: it answered, "
            "but not with the work. A spawn record with no readable status is derived from its files -- an "
            "answer on disk means it ended. `queued` and `skipped` only occur for graph nodes."
        ),
    )
    agent: str | None = Field(default=None, description="The external agent that ran it, or null for a raven subagent.")
    instance: str | None = Field(default=None, description="The handle sharing one stateful agent session, if any.")
    started_at: str | None = Field(default=None, description="ISO 8601; the record stores epoch millis.")
    ended_at: str | None = Field(default=None, description="Absent while it is still running.")
    message_count: int = Field(..., description="What was asked, plus the answer once there is one.")
    tokens: int | None = Field(
        default=None,
        description=(
            "Input plus output tokens this run spent. Null, not zero, when the transport that ran it cannot "
            "report usage -- the cli lane never can, and a zero there would be a claim about the agent."
        ),
    )
    tool_call_count: int | None = Field(
        default=None,
        description="How many tools it called. Null where the transport has no per-step visibility.",
    )


class SubagentListParams(_Strict):
    session_id: str | None = Field(
        default=None,
        description=(
            "Whose calls to list. Absent or unknown is an empty list, not an error: the record lives inside "
            "one conversation's directory, so there is no cross-session listing to give."
        ),
    )


class SubagentListResult(_Strict):
    items: list[SubagentCall] = Field(..., description="Newest first.")


class SubagentContextParams(_Strict):
    id: str = Field(..., description="The call id from subagent.list.")
    session_id: str = Field(
        ...,
        description="Required with the id: a call is addressed by conversation and call, never by call alone.",
    )


class SubagentContextResult(_Strict):
    """The same wire shape ``session.resume`` returns, so one renderer draws both."""

    id: str
    messages: list["TranscriptMessage"] = Field(
        ...,
        description=(
            "What was asked, the run's own recorded turns where the transport could see them "
            "(acp records thoughts, tool calls and results; cli cannot), and what came back."
        ),
    )
    status: str | None = None
    label: str | None = None
    agent: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    tool_calls: list[str] = Field(
        default_factory=list,
        description=(
            "What it did between the question and the answer, in order. Empty where the transport that ran it "
            "has no per-step visibility -- which is a property of the transport, not a quiet run."
        ),
    )
    tokens: int | None = Field(default=None, description="Input plus output; null when usage was not reported.")
    tokens_in: int | None = None
    tokens_out: int | None = None
    thought_chars: int = Field(
        default=0,
        description="How much reasoning it emitted, where the transport reports it separately from the answer.",
    )


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
    mcps: list[str] | None = None
    allow_mcp_secrets: bool | None = None


class SubagentsAddResult(_Strict):
    added: bool
    name: str


class SubagentsUpdateParams(_Strict):
    name: str
    new_name: str | None = None
    description: str | None = None
    api_key: str | None = None
    mcps: list[str] | None = None
    allow_mcp_secrets: bool | None = None


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


class SubagentsBuildParams(_Strict):
    name: str


class SubagentsBuildResult(_Strict):
    building: bool
    """True once the build is under way, including when one was already running."""
    detail: str
    """Why nothing new was started, or "" when this call started it."""


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
# subagents.instance* methods
#
# A different noun from the ``subagents.*`` group above: that one is about which
# sub-agents are configured, this one about the instances a session has actually
# talked to -- which is what the direct-chat surface addresses.
# ---------------------------------------------------------------------------


class SubagentsInstancesParams(_Strict):
    session_key: str


class SubagentsInstancesResult(_Strict):
    instances: list[InstanceRow]
    pending_handoff_count: int = Field(
        description=(
            "Direct-chat turns, and instances the user created, that this session has not yet "
            "reported to its main agent. Display only; the runtime owns the list and clears it "
            "on the next main-agent turn."
        ),
    )


class SubagentsInstanceCreateParams(_Strict):
    session_key: str
    agent: str = Field(
        description=(
            "Which sub-agent to instantiate. Must be enabled and stateful: a direct chat is a "
            "continuation, and against a stateless agent every turn would start over."
        ),
    )


class SubagentsInstanceCreateResult(_Strict):
    instance: InstanceRow


class SubagentsInstanceHistoryParams(_Strict):
    session_key: str
    agent: str
    handle: str


class SubagentsInstanceHistoryResult(_Strict):
    turns: list[DirectTurn]


class SubagentsInstanceForgetParams(_Strict):
    session_key: str
    agent: str
    handle: str


class SubagentsInstanceForgetResult(_Strict):
    removed: bool


class SubagentsInstanceSteerParams(_Strict):
    session_key: str
    agent: str
    handle: str
    text: str


class SubagentsInstanceSteerResult(_Strict):
    # ``injected``: merged into the turn the instance is running. ``no_turn``:
    # nothing is running, nothing was started, the caller keeps the text.
    # ``unsupported``: the run's transport cannot take text mid-turn.
    status: Literal["injected", "no_turn", "unsupported"]


class SubagentMode(_Strict):
    """One operating profile an agent offers, as its probe measured it."""

    id: str
    name: str = ""
    description: str = ""


class SubagentsInstanceSetModeParams(_Strict):
    session_key: str
    agent: str
    handle: str
    mode: str | None = Field(None, description="The mode id to switch to. Omit it to report without changing.")
    clear: bool = Field(
        False, description="Drop the override and go back to the agent's own default. Ignored when mode is given."
    )


class SubagentsInstanceSetModeResult(_Strict):
    mode: str | None = Field(None, description="The mode now in force, or null when the agent's default is.")
    available_modes: list[SubagentMode] = Field(
        default_factory=list,
        alias="availableModes",
        description="Everything this agent offers, so one reply is enough to draw the control.",
    )


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
    config_notices: list[str] | None = Field(
        default=None,
        description=(
            "What a config migration changed on the user's behalf during this boot. "
            "Drained, so only the first session of a launch carries them."
        ),
    )
    endpoint: str | None = Field(
        default=None,
        description="Which of a multi-endpoint provider's endpoints this session is on; null for single-endpoint ones.",
    )
    title: str | None = Field(
        default=None,
        description=(
            "The resumed session's name, when it has one. Absent on a fresh session, which has "
            "nothing to name yet. Carried on the bundle rather than fetched separately because a "
            "client resuming a session is already being told what it is resuming."
        ),
    )


class TranscriptTurnEnded(_Strict):
    """Why a turn's transcript stops where it does."""

    status: str = Field(..., description="'cancelled' (a person stopped it) or 'failed'.")
    reason: str | None = None


class TranscriptNotice(_Strict):
    """Marks an assistant entry the runtime wrote rather than the model."""

    kind: str = Field(..., description="Which runtime decision this reports; `action_blocked` today.")
    detail: str | None = None


class TranscriptDelegated(_Strict):
    """Which delegated run re-entered the conversation at this entry.

    The same identity ``subagent.delivered`` carries, so a replayed transcript
    and a live stream draw the same row from the same fields.
    """

    kind: Literal["spawn", "dag"]
    label: str
    status: Literal["ok", "error", "exception"]
    run_id: str | None = Field(default=None, description="Set for kind=dag, so a client can open the run.")
    node_id: str | None = Field(
        default=None,
        description="Set for a dag node's own message, so a client can place it against that row.",
    )


class TranscriptMessage(_Strict):
    """One stored message in wire form: ``content`` renamed to ``text``."""

    role: str
    text: str | None = None
    context: JsonValue = None
    name: str | None = None
    tool_call_id: str | None = None
    dag_run_id: str | None = Field(
        default=None,
        description=(
            "The run a run_subagent_dag call started, so a resumed transcript can fetch its graph "
            "through dag.get. Absent on every other tool, and on a graph that was rejected before "
            "it ran."
        ),
    )
    spawn_task_id: str | None = Field(
        default=None,
        description=(
            "The task id a spawn call's result names, so a resumed transcript can find the run's "
            "record through subagent.list. Absent on every other tool, and on a spawn that was "
            "refused before it ran."
        ),
    )
    timestamp: str | None = None
    reasoning_content: str | None = None
    reasoning_ms: int | None = Field(
        default=None,
        description=(
            "How long the thought on this assistant entry took, measured server-side from the "
            "first reasoning delta to the first answer token or tool call. Absent means unknown "
            "(an unstreamed call, or a session written before it was recorded) -- render the bare "
            "header, never a zero."
        ),
    )
    tool_calls: list[TranscriptToolCall] | None = None
    duration_ms: int | None = Field(
        default=None,
        description=(
            "How long the call this role='tool' entry answers ran, dispatch to result. "
            "Absent means unknown, same rule as reasoning_ms."
        ),
    )
    diff: str | None = Field(
        default=None,
        description="A file tool's unified diff of the change it made, on its role='tool' entry.",
    )
    turn_ended: TranscriptTurnEnded | None = Field(
        default=None,
        description="Present on the closing entry of a turn that was cancelled or died: why the transcript stops.",
    )
    notice: TranscriptNotice | None = Field(
        default=None,
        description=(
            "Present on an assistant entry the runtime wrote. The model reads `text`, "
            "but a reader must not: render the notice, or a reload attributes runtime "
            "prose to the model that a live view showed as its own row."
        ),
    )
    origin: str | None = Field(
        default=None,
        description=(
            "Present on the user entry of a turn the runtime opened, naming what opened "
            "it (`subagent`, `cron`, `sentinel`, `heartbeat`). Absent means a person "
            "typed it. Same rule as `notice`, one role over: the model reads `text`, a "
            "reader must not -- a sub-agent's announce carries an untrusted fence, an "
            "instance handle and an instruction not to repeat either to the user, and a "
            "cron reminder carries how to word the reply."
        ),
    )
    delegated: TranscriptDelegated | None = Field(
        default=None,
        description=(
            "Present on a USER entry the runtime wrote: a delegated run's result "
            "re-entering the conversation. Same rule as `notice` and for the same "
            "reason -- the model reads `text`, a reader must not. Render the delivery "
            "row a live client draws from `subagent.delivered`, and take the readable "
            "body from inside the untrusted fence in `text`; drawing `text` as prose "
            "attributes to the user a question they never asked, fence markers "
            "included. `origin` names who opened the turn; this says which run came back."
        ),
    )


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


class ToolSetupNeed(_Strict):
    setting: str = Field(..., description="Dotted config key that unlocks the tool.")
    env: str | None = Field(default=None, description="Environment variable accepted instead.")


class ExtToolRow(_Strict):
    name: str
    description: str
    enabled: bool
    mcp_server: str | None = Field(default=None, description="Owning MCP server, or null for a built-in tool.")
    needs: ToolSetupNeed | None = Field(
        default=None,
        description=(
            "Set when the tool exists but is withheld for want of a key. The model cannot call it; "
            "the row is here so the page can offer the field instead of the tool simply being absent."
        ),
    )


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
    borrow_from: str | None = Field(
        default=None,
        description=(
            "Take api_key and base_url from this connected provider, copied not "
            "referenced. Wins over the same keys in `fields`, which cannot carry a "
            "real key: the page only ever sees a redacted one."
        ),
    )


class SettingsEverosSetResult(_Strict):
    applied: bool


class ChannelField(_Strict):
    key: str
    label: str
    required: bool
    secret: bool
    set: bool = Field(..., description="Whether a value is stored; the value itself never rides the wire.")


class ChannelStatusRow(_Strict):
    name: str
    enabled: bool
    configured: bool
    missing: list[str] = Field(..., description="Required fields still empty.")
    fields: list[ChannelField] = Field(
        default_factory=list,
        description="Every field the channel takes, so a client can render its configure form.",
    )
    running: bool | None = Field(
        default=None,
        description="Whether the adapter is up, read from the live gateway. Null when no gateway answered.",
    )
    connected: bool | None = Field(
        default=None,
        description=(
            "Whether the account is paired. Only the QR-login channels report this; null means the "
            "channel does not report a pairing and must not be drawn as disconnected."
        ),
    )
    qr_login: bool | None = Field(
        default=None,
        description="Whether this channel signs in by scanning a code.",
    )


class ChannelsStatusParams(_Strict):
    pass


class ChannelsStatusResult(_Strict):
    channels: list[ChannelStatusRow]
    gateway_running: bool


class ChannelsQrParams(_Strict):
    name: str


class ChannelsQrResult(_Strict):
    qr: str | None = Field(default=None, description="The scan code as a PNG data URI, or null when there is none.")
    qr_text: str | None = Field(
        default=None,
        description="The raw scan payload, sent only when the server could not rasterise it.",
    )
    connected: bool = Field(..., description="Whether the account is paired; true ends the client's polling.")
    running: bool


class ChannelsConfigureParams(_Strict):
    name: str
    fields: dict[str, JsonValue] = Field(
        default_factory=dict, description="Field-path -> value patch; blank strings are skipped, never written."
    )
    enabled: bool | None = Field(
        default=None,
        description="Connect or disconnect the channel; omitted leaves its current state alone.",
    )


class ChannelsConfigureResult(_Strict):
    applied: bool


class FsEntry(_Strict):
    name: str
    dir: bool
    size: int = Field(..., description="Zero for a directory.")


class FsListParams(_Strict):
    path: str | None = Field(default=None, description="Root-relative; the root when omitted.")
    session: str | None = Field(
        default=None,
        description="Session key whose working directory roots the listing; the policy default when omitted.",
    )


class FsListResult(_Strict):
    root: str = Field(..., description="Absolute working directory the listing is rooted at.")
    path: str
    entries: list[FsEntry] = Field(..., description="Directories first, dotfiles omitted, capped at 500.")


class FsReadParams(_Strict):
    path: str
    max_bytes: int | None = None
    session: str | None = None


class FsReadResult(_Strict):
    content: str = Field(..., description="Decoded as UTF-8 with replacement, so binary never fails the call.")
    truncated: bool
    size: int = Field(..., description="Size on disk, which exceeds len(content) when truncated.")


class FsUploadParams(_Strict):
    name: str
    content_b64: str
    session: str | None = None


class FsUploadResult(_Strict):
    path: str = Field(..., description="Workspace-relative path to hand the agent; uploads never return bytes.")
    abs_path: str
    size: int


class FsRevealParams(_Strict):
    path: str = Field(..., description="Absolute, or relative to the session's working directory.")
    session: str | None = None


class FsRevealResult(_Strict):
    ok: Literal[True]


class DeliverablesListParams(_Strict):
    session_key: str = Field(..., description="Full session_key. An empty or unknown key answers with an empty list.")


class DeliverableEntry(_Strict):
    path: str
    name: str
    title: str | None = Field(None, description="What the agent called the file; empty when it named none.")
    description: str | None = None
    size: int
    media_type: str
    download_path: str = Field(..., description="Token URL on the gateway; never a path.")
    created_at: str = Field(..., description="ISO-8601, when the file was first delivered.")
    missing: bool = Field(..., description="The registry has it, the filesystem no longer does.")


class DeliverablesListResult(_Strict):
    files: list[DeliverableEntry] = Field(..., description="Oldest first, one entry per delivered path.")


class FsOpenParams(_Strict):
    path: str = Field(..., description="Absolute, or relative to the session's working directory.")
    app: str | None = Field(
        default=None,
        description=(
            "Which installed application to hand the file to. Absent means the host's own default. "
            "A NAME, not a path or a command line: the server rejects anything with a separator, a "
            "shell character or a leading dash, and never runs it through a shell."
        ),
    )
    session: str | None = None


class FsOpenResult(_Strict):
    ok: Literal[True]
    app: str | None = Field(
        default=None,
        description="The application asked for, echoed back; absent when the host default was used.",
    )


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


# ---------------------------------------------------------------------------
# browser.* -- the shared page
#
# Every one of these answers `ok` plus some subset of the driver's page state,
# because the handlers spread it in rather than projecting. The subset differs
# by how far the call got: a browser that was never started reports whether it
# *could* be (`available` / `reason`) and nothing about a page, and a call that
# failed reports `error` instead of a new state. So one optional-heavy base is
# the honest description; a model per call would claim distinctions the handlers
# do not make.
# ---------------------------------------------------------------------------


class BrowserPageState(_Strict):
    ok: bool
    url: str | None = None
    title: str | None = None
    started: bool | None = None
    headful: bool | None = None
    available: bool | None = Field(default=None, description="False when playwright or its Chromium is missing.")
    reason: str | None = Field(default=None, description="Why a browser cannot be started, when it cannot.")
    loading: bool | None = None
    can_back: bool | None = None
    can_forward: bool | None = None
    tab_count: int | None = None
    error: str | None = Field(
        default=None,
        description="A refused navigation or a failed action; the call still answers rather than raising.",
    )


class BrowserStateParams(_Strict):
    pass


class BrowserStateResult(BrowserPageState):
    pass


class BrowserOpenParams(_Strict):
    url: str | None = Field(
        default=None,
        description="http or https only; a bare host is completed to https. Other schemes are refused.",
    )
    action: str | None = Field(default=None, description="back | forward | reload | stop, instead of a url.")


class BrowserOpenResult(BrowserPageState):
    pass


class BrowserTab(_Strict):
    index: int
    url: str
    title: str
    active: bool
    # Per tab, because the page-level flag can only speak for the active one --
    # and a background tab still loading is exactly what the tab strip is for.
    loading: bool | None = None


class BrowserTabsParams(_Strict):
    action: str | None = Field(default=None, description="list (default) | new | activate | close.")
    index: int | None = None
    url: str | None = Field(default=None, description="Navigate a freshly opened tab in the same call.")


class BrowserTabsResult(BrowserPageState):
    tabs: list[BrowserTab] | None = None


class BrowserFrameParams(_Strict):
    quality: int | None = Field(default=None, description="JPEG quality; 55 by default.")


class BrowserFrameResult(BrowserPageState):
    jpeg: str | None = Field(default=None, description="Base64 JPEG of the page, absent when nothing is started.")


class BrowserReadParams(_Strict):
    pass


class BrowserRef(_Strict):
    """One actionable element, with the id a click can be aimed at."""

    ref: str
    role: str
    name: str
    x: int
    y: int
    href: str | None = None
    value: str | None = Field(default=None, description="Never present for a password field.")
    disabled: bool | None = None


class BrowserConsoleLine(_Strict):
    type: str
    text: str


class BrowserReadResult(BrowserPageState):
    text: str | None = Field(default=None, description="Page text, capped.")
    refs: list[BrowserRef] | None = None
    console: list[BrowserConsoleLine] | None = None


class BrowserInputParams(_Strict):
    kind: str = Field(
        ...,
        description="click | text | key | scroll, or the raw stream kinds move/down/up/wheel/keydown/keyup.",
    )
    ref: str | None = None
    x: float | None = None
    y: float | None = None
    dx: float | None = None
    dy: float | None = None
    button: str | None = None
    count: int | None = None
    key: str | None = None
    text: str | None = None


class BrowserInputResult(BrowserPageState):
    pass


class BrowserModeParams(_Strict):
    headful: bool = Field(..., description="True pops the page into a real window; false folds it back.")


class BrowserModeResult(BrowserPageState):
    pass


class BrowserCloseParams(_Strict):
    pass


class BrowserCloseResult(BrowserPageState):
    pass


class BrowserWatchParams(_Strict):
    on: bool
    width: int | None = None
    height: int | None = None
    quality: int | None = None


class BrowserWatchResult(BrowserPageState):
    watching: bool | None = None
    vw: int | None = None
    vh: int | None = None


class KnowledgeStatusParams(_Strict):
    pass


class KnowledgeStatusResult(_Strict):
    """Whether a base can be created, and with which model.

    ``model`` is empty exactly when ``configured`` is false. No credential is
    reported: the key's presence *is* the flag."""

    configured: bool
    model: str


class KnowledgeBase(_Strict):
    """One base as the list view needs it.

    ``embedding_model`` and ``dimensions`` are the base's own, recorded when it
    was created rather than read from today's config -- a base outlives a change
    to what the operator has configured, and the page has to be able to show the
    mismatch."""

    id: str
    name: str
    description: str
    embedding_model: str
    dimensions: int
    created_at: str
    updated_at: str
    documents: int


class KnowledgeBasesListParams(_Strict):
    pass


class KnowledgeBasesListResult(_Strict):
    bases: list[KnowledgeBase]


class KnowledgeDocument(_Strict):
    """One uploaded document and where its indexing got to.

    ``error`` is empty unless ``status`` is ``failed``; a row carries the reason
    with it so a reader does not have to go looking for why nothing is
    searchable."""

    id: str
    base_id: str
    source: str
    media_type: str
    size: int
    status: str
    chunk_count: int
    error: str
    created_at: str
    updated_at: str


class KnowledgeBasesCreateParams(_Strict):
    name: str
    description: str | None = None


class KnowledgeBasesCreateResult(_Strict):
    base: KnowledgeBase


class KnowledgeBasesRenameParams(_Strict):
    """Either field may be omitted; the one left out is untouched."""

    base_id: str
    name: str | None = None
    description: str | None = None


class KnowledgeBasesRenameResult(_Strict):
    base: KnowledgeBase


class KnowledgeBasesDeleteParams(_Strict):
    base_id: str


class KnowledgeBasesDeleteResult(_Strict):
    """False for a base that was not there: a second delete from a stale page
    reached the outcome its caller wanted."""

    removed: bool


class KnowledgeDocumentsListParams(_Strict):
    base_id: str


class KnowledgeDocumentsListResult(_Strict):
    documents: list[KnowledgeDocument]


class KnowledgeHit(_Strict):
    """One search hit. ``score`` is a similarity, so higher is nearer -- the
    direction every caller already reads."""

    score: float
    document_id: str
    text: str


class KnowledgeDocumentsAddParams(_Strict):
    """``path`` is what ``fs.upload`` answered -- a workspace path such as
    ``uploads/handbook.md``, resolved through the filesystem tools' own policy
    rather than opened as given."""

    base_id: str
    path: str


class KnowledgeDocumentsAddResult(_Strict):
    document: KnowledgeDocument


class KnowledgeDocumentsIndexParams(_Strict):
    document_id: str


class KnowledgeDocumentsIndexResult(_Strict):
    document: KnowledgeDocument


class KnowledgeDocumentsDeleteParams(_Strict):
    document_id: str


class KnowledgeDocumentsDeleteResult(_Strict):
    removed: bool = Field(
        description=(
            "False when there was no such document, which is not an error: two clicks on one row answer the same way."
        )
    )


class KnowledgeSearchParams(_Strict):
    base_ids: list[str]
    query: str
    top_k: int | None = None


class KnowledgeSearchResult(_Strict):
    hits: list[KnowledgeHit]


# ── playbooks.* — the stored library, read-only ────────────────────────────


class PlaybookNodeShape(_Strict):
    """Just enough of one step to draw the graph: which step it is, and what it
    waits for.

    The library page draws a concept diagram per card, so the list has to carry
    the shape; carrying the prompts and per-node config the detail view needs
    would be the whole library shipped on page open."""

    id: str
    depends_on: list[str]


class PlaybookRow(_Strict):
    """One playbook as the library list needs it.

    ``error`` is empty unless the file would not parse, in which case it carries
    the reason and ``nodes`` is empty -- one unreadable file in a directory of
    user-edited text must not take the page down with it. ``disabled`` lives in
    config rather than in the file, because the file is the distribution unit and
    the switch is local to this machine."""

    name: str
    description: str
    task_summary: str
    mode: Literal["dag", "prompt"]
    confirm: bool
    origin: str
    disabled: bool
    nodes: list[PlaybookNodeShape]
    error: str


class PlaybookParam(_Strict):
    """One runtime input. ``description`` is the sentence the caller is asked
    when the value is missing, so a form built from this uses it as the label."""

    type: str
    required: bool
    default: Any = None
    enum: list[str] | None = None
    description: str


class PlaybookMcpServer(_Strict):
    """One MCP server the playbook itself carries, as the file declares it.

    Shown so a reader can tell a portable carried server from a host server of
    the same name -- a node's ``mcps`` entry is only a name, and the two resolve
    to different processes.

    ``env`` and ``headers`` are the declarations, not resolved values: a carried
    server references a credential through ``{{ params.X }}`` and the run
    supplies it, so what the file holds is the reference and that is what this
    reports. Nothing here is ever a secret's value.
    """

    type: Literal["stdio", "sse", "streamableHttp"] | None = None
    """The transport the file declares, or ``None`` for the runtime to detect
    from ``command`` / ``url``. Reported rather than collapsed into a guess: an
    ``sse`` server presented as generic http is a different protocol."""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    tool_timeout: int = 30
    enabled: bool = True
    """False is a server the run will not dial. Without it a disabled definition
    reads as launchable, which is the opposite of what it does."""
    auth: Literal["none", "apikey", "oauth"] = "none"
    has_oauth_config: bool = False
    """Whether the file declares OAuth endpoints of its own, not what they are:
    a client id or a registration endpoint is the deployment's business and the
    reader only needs to know the server carries one."""


class PlaybookNode(_Strict):
    """One step, whole.

    ``subagent`` / ``node_summary`` / ``prompt_template`` may be empty: those
    three are the fields an author may leave blank for the caller to fill at run
    time. ``skills`` / ``mcps`` are three-state -- null means the author said
    nothing, ``[]`` means the author wrote an empty list, and a list names what
    to consider."""

    id: str
    subagent: str
    node_summary: str
    prompt_template: str
    depends_on: list[str]
    skills: list[str] | None = None
    mcps: list[str] | None = None
    instance: str
    inputs: dict[str, Any]


class PlaybookDetail(_Strict):
    """One whole playbook: its identity, its runtime inputs, and either the graph
    (``mode: dag``) or the assembly guidance a model turns into one
    (``mode: prompt``). ``path`` is the file this was read from.

    ``version`` is the spec format version the file declares, not a revision of
    the playbook's content."""

    name: str
    description: str
    task_summary: str
    version: int
    mode: Literal["dag", "prompt"]
    confirm: bool
    origin: str
    disabled: bool
    path: str
    keywords: list[str]
    params: dict[str, PlaybookParam]
    nodes: list[PlaybookNode]
    prompts: str
    mcp_servers: dict[str, PlaybookMcpServer] = Field(default_factory=dict)
    """The servers this playbook ships, keyed by the name a node's ``mcps`` uses.

    Empty for a playbook that names only servers the host configures, which is
    most of them."""


class PlaybooksListParams(_Strict):
    pass


class PlaybooksListResult(_Strict):
    playbooks: list[PlaybookRow]


class PlaybooksGetParams(_Strict):
    name: str


class PlaybooksGetResult(_Strict):
    playbook: PlaybookDetail


METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    # knowledge.* -- bases and their documents, served by the in-process engine
    "knowledge.status": (KnowledgeStatusParams, KnowledgeStatusResult),
    "knowledge.bases.list": (KnowledgeBasesListParams, KnowledgeBasesListResult),
    "knowledge.bases.create": (KnowledgeBasesCreateParams, KnowledgeBasesCreateResult),
    "knowledge.bases.rename": (KnowledgeBasesRenameParams, KnowledgeBasesRenameResult),
    "knowledge.bases.delete": (KnowledgeBasesDeleteParams, KnowledgeBasesDeleteResult),
    "knowledge.documents.list": (KnowledgeDocumentsListParams, KnowledgeDocumentsListResult),
    "knowledge.documents.add": (KnowledgeDocumentsAddParams, KnowledgeDocumentsAddResult),
    "knowledge.documents.index": (KnowledgeDocumentsIndexParams, KnowledgeDocumentsIndexResult),
    "knowledge.documents.delete": (KnowledgeDocumentsDeleteParams, KnowledgeDocumentsDeleteResult),
    "knowledge.search": (KnowledgeSearchParams, KnowledgeSearchResult),
    # playbooks.* -- the stored library, read-only
    "playbooks.list": (PlaybooksListParams, PlaybooksListResult),
    "playbooks.get": (PlaybooksGetParams, PlaybooksGetResult),
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
    "session.pin": (SessionPinParams, SessionPinResult),
    "session.archive": (SessionArchiveParams, SessionArchiveResult),
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
    "channels.configure": (ChannelsConfigureParams, ChannelsConfigureResult),
    "channels.qr": (ChannelsQrParams, ChannelsQrResult),
    "fs.list": (FsListParams, FsListResult),
    "fs.read": (FsReadParams, FsReadResult),
    "fs.upload": (FsUploadParams, FsUploadResult),
    "fs.reveal": (FsRevealParams, FsRevealResult),
    "fs.open": (FsOpenParams, FsOpenResult),
    "deliverables.list": (DeliverablesListParams, DeliverablesListResult),
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
    # subagent.* -- handed-off calls (singular; not the plural below)
    "subagent.list": (SubagentListParams, SubagentListResult),
    "subagent.context": (SubagentContextParams, SubagentContextResult),
    # subagents.*
    "subagents.list": (SubagentsListParams, SubagentsListResult),
    "subagents.add": (SubagentsAddParams, SubagentsAddResult),
    "subagents.update": (SubagentsUpdateParams, SubagentsUpdateResult),
    "subagents.remove": (SubagentsRemoveParams, SubagentsRemoveResult),
    "subagents.build": (SubagentsBuildParams, SubagentsBuildResult),
    "subagents.toggle": (SubagentsToggleParams, SubagentsToggleResult),
    "subagents.probe": (SubagentsProbeParams, SubagentsProbeResult),
    "subagents.test": (SubagentsTestParams, SubagentsTestResult),
    "subagents.test_cancel": (SubagentsTestCancelParams, SubagentsTestCancelResult),
    # subagents.instance*
    "subagents.instances": (SubagentsInstancesParams, SubagentsInstancesResult),
    "subagents.instance.create": (SubagentsInstanceCreateParams, SubagentsInstanceCreateResult),
    "subagents.instance.history": (SubagentsInstanceHistoryParams, SubagentsInstanceHistoryResult),
    "subagents.instance.forget": (SubagentsInstanceForgetParams, SubagentsInstanceForgetResult),
    "subagents.instance.steer": (SubagentsInstanceSteerParams, SubagentsInstanceSteerResult),
    "subagents.instance.set_mode": (SubagentsInstanceSetModeParams, SubagentsInstanceSetModeResult),
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
    # browser.* -- the shared page
    "browser.state": (BrowserStateParams, BrowserStateResult),
    "browser.open": (BrowserOpenParams, BrowserOpenResult),
    "browser.tabs": (BrowserTabsParams, BrowserTabsResult),
    "browser.frame": (BrowserFrameParams, BrowserFrameResult),
    "browser.read": (BrowserReadParams, BrowserReadResult),
    "browser.input": (BrowserInputParams, BrowserInputResult),
    "browser.mode": (BrowserModeParams, BrowserModeResult),
    "browser.close": (BrowserCloseParams, BrowserCloseResult),
    "browser.watch": (BrowserWatchParams, BrowserWatchResult),
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
    "DirectTarget",
    "InstanceRow",
    "DirectTurn",
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
    "SessionArchiveParams",
    "SessionArchiveResult",
    "SessionClearParams",
    "SessionClearResult",
    "SessionUndoParams",
    "SessionUndoResult",
    "SessionExportParams",
    "SessionExportResult",
    "MessageStartEvent",
    "SessionNamingEndedEvent",
    "SessionNamingEndedPayload",
    "SessionTitledEvent",
    "SessionTitledPayload",
    "EpisodeStartEvent",
    "NoticeEvent",
    "NoticePayload",
    "TokenDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolStartEvent",
    "ToolProgressEvent",
    "ToolCompleteEvent",
    "FileChange",
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
    "CronMissedEvent",
    "MediaEvent",
    "MediaItem",
    "MediaPayload",
    "CronMissedItem",
    "CronMissedPayload",
    # playbooks
    "PlaybookDetail",
    "PlaybookNode",
    "PlaybookNodeShape",
    "PlaybookParam",
    "PlaybookRow",
    "PlaybooksGetParams",
    "PlaybooksGetResult",
    "PlaybooksListParams",
    "PlaybooksListResult",
    # registry
    "METHOD_MODELS",
]
