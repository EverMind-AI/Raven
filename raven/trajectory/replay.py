"""Deterministic trajectory replay — recorded model replies and tool results
fed back through the live harness.

Record-replay debugging for the agent loop: a Trajectory Bundle already holds
every model call's full input/output (``llm.input`` / ``llm.output`` artifacts)
and every tool call's input/result (``tool.input`` / ``tool.output``). Replay
re-runs the *harness code* (the agent loop, message assembly, recovery logic)
against those recordings:

- :class:`ReplayProvider` implements the ``LLMProvider`` interface and answers
  each ``chat`` / ``chat_stream`` call with the next recorded ``llm.output``,
  in recording order.
- :class:`ReplayToolRegistry` replaces the loop's tool registry and answers
  each ``execute`` call with the next recorded tool result. **No real tool
  code ever runs in replay** — the registry never dispatches to a ``Tool``.
- :func:`run_replay` drives an ``AgentLoop`` through the bundle's recorded
  turn inputs in an isolated temporary workspace.

Divergence
----------

A divergence is the point where the live harness's request no longer matches
the recording — expected after a bug fix (the fixed code behaves differently),
and exactly what replay exists to surface. Two policies, shared by the model
and tool feeds:

- ``strict``: the first mismatch halts the replay. The recorded divergence
  names the call index, the field, and an expected-vs-actual excerpt.
- ``warn``: mismatches are recorded (and reported) but the replay keeps
  feeding recorded data by order.

Running out of recording always halts, in both modes — there is nothing left
to feed.

What is compared (and how it is normalized)
-------------------------------------------

Per model call, the live request is compared against the recorded
``llm.input``: the ``model`` id, the streaming vs non-streaming path (from the
recorded ``llm.stream`` attribute), the message-list length, each message's
role, content, and reasoning fields, assistant ``tool_calls`` (name +
arguments), and the *names* of the offered tools (schema bodies drift with
cosmetic edits and are not compared). Normalization is deliberately narrow — only text the harness
regenerates freshly on every run is masked, so a date or an id inside user
content or tool arguments still counts as a real divergence:

- the ``Current Time:`` line of the runtime-context header (the one clock
  rendering that reaches compared, non-system content);
- the per-call random nonces on ``[BEGIN/END UNTRUSTED ...]`` fence marker
  lines (``security.trust.wrap_untrusted``) — fence *bodies* stay untouched;
- prompt-cache breakpoints, removed from both sides via
  ``providers.prompt_cache.strip`` — cache placement is a transport
  optimization, not conversation semantics.

System-role message *content* is deliberately not compared: it is assembled
from the live environment (workspace paths, current time, memory state) that
a fresh replay workspace cannot reproduce, so comparing it would flag every
replay as divergent at call #1 for environmental reasons. Harness bugs in
prompt assembly are unit-test territory, not replay territory.

Tool calls are matched by order; the live call's tool name and arguments are
compared against the recorded ``tool.input`` under the same normalization.

For programmatic assertions (the trajectory regression suite), every live
request the harness makes is also captured verbatim on the report
(:attr:`ReplayReport.llm_requests` / :attr:`ReplayReport.tool_requests`), and
each :class:`Divergence` carries the structured ``expected`` / ``actual``
values of its mismatching field alongside the human-readable ``detail``.

Session history
---------------

An attempt can start mid-conversation, and the recorded requests then carry
the pre-attempt turns as history. :func:`run_replay` reseeds that state from
the bundle's ``session.jsonl``: every message *before* the attempt's first
turn is loaded into the replay session, and nothing from inside or after the
attempt is (the attempt's own turns are re-created by the replay itself). The
cut point is located by the manifest's ``time_range`` and confirmed by the
first turn's input text (see :func:`_pre_attempt_messages`); when the two
signals cannot pin it down, nothing is seeded — missing history shows up as a
divergence, never as attempt messages loaded twice.

Known limits (v1): recorded media attachments are not re-fed; a recorded
``ToolOutput``'s display/abort/blocks metadata is not in the recording (only
the model-facing text is), so replay feeds plain text; parallel tool or
subagent interleavings are assumed sequential — a reordering shows up as a
divergence rather than being re-matched.

Tracing
-------

A replay run suppresses tracing for its own task tree
(:func:`raven.tracing.trace.suppress`, a context variable — not an env var or
process-global flag, so real turns running concurrently in the same process
keep tracing). A replay is a mock re-run: letting it emit spans would mint
fake trajectories in the live store (they would surface in ``raven trajectory
list`` and could be bundled or pinned). The replay's own observability is the
:class:`ReplayReport`.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, AsyncIterator

from loguru import logger

from raven.agent.tools.registry import ToolRegistry
from raven.providers import prompt_cache
from raven.providers.base import (
    ErrorClassification,
    LLMProvider,
    LLMResponse,
    StreamDelta,
    ToolCallRequest,
)
from raven.tracing import trace

REPLAY_MODES = ("strict", "warn")

_STREAM_CHUNK_CHARS = 80

_NONCE_RE = re.compile(r"#[0-9a-f]{8}\b")
_CLOCK_LINE_RE = re.compile(r"^(Current Time:).*$", re.MULTILINE)
_UNTRUSTED_MARKER_LINE_RE = re.compile(r"^\[(?:BEGIN|END) UNTRUSTED [^\n]*$", re.MULTILINE)


@dataclass(frozen=True)
class Mismatch:
    """One compared field where the live request departs from the recording."""

    field: str
    detail: str  # human-readable expected/actual excerpt
    expected: Any = None  # the recorded value of the field
    actual: Any = None  # the live value of the field


@dataclass(frozen=True)
class Divergence:
    """One point where the live harness departed from the recording."""

    kind: str  # "llm" | "tool"
    index: int  # 0-based call index within its kind
    fatal: bool  # halted the replay (strict mismatch, exhaustion, missing data)
    field: str
    detail: str
    expected: Any = None  # recorded value of the field (None for exhaustion/unconsumed)
    actual: Any = None  # live value of the field

    def render(self) -> str:
        return f"{self.kind} call #{self.index + 1}: {self.field} — {self.detail}"


@dataclass
class RecordedLLMCall:
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    stream: bool = False
    trace_id: str | None = None


@dataclass
class RecordedToolCall:
    name: str | None
    params: Any
    result: str | None


@dataclass
class RecordedTurn:
    content: str
    channel: str | None
    chat_id: str | None
    session_key: str | None
    trace_id: str | None = None


@dataclass
class Recording:
    """The replayable view of one bundle: ordered calls and turn inputs."""

    bundle_dir: Path
    manifest: dict[str, Any]
    llm_calls: list[RecordedLLMCall]
    tool_calls: list[RecordedToolCall]
    turns: list[RecordedTurn]

    @property
    def model(self) -> str | None:
        for call in self.llm_calls:
            if call.input and call.input.get("model"):
                return call.input["model"]
        return None


@dataclass
class ReplayState:
    """Cursor + divergence state shared by the model and tool feeds."""

    mode: str = "warn"
    llm_cursor: int = 0
    llm_fed: int = 0
    llm_streamed: int = 0
    tool_cursor: int = 0
    tool_fed: int = 0
    divergences: list[Divergence] = field(default_factory=list)
    halted: bool = False
    # Every live request the feeds received, verbatim — the raw material for
    # regression assertions about what the harness actually did.
    llm_requests: list[dict[str, Any]] = field(default_factory=list)
    tool_requests: list[dict[str, Any]] = field(default_factory=list)

    def record(self, div: Divergence) -> None:
        self.divergences.append(div)
        if div.fatal:
            self.halted = True
            logger.error("replay halted: {}", div.render())
        else:
            logger.warning("replay divergence: {}", div.render())


@dataclass
class ReplayReport:
    """What one replay run did, and where (if anywhere) it diverged."""

    bundle_dir: Path
    mode: str
    turns_replayed: int
    turns_recorded: int
    llm_calls_replayed: int
    llm_calls_recorded: int
    llm_calls_streamed: int
    tool_calls_replayed: int
    tool_calls_recorded: int
    divergences: list[Divergence]
    halted: bool
    replies: list[str | None]
    # Live requests in call order: each llm entry holds model/stream/messages
    # plus the offered tool names; each tool entry holds name/params.
    llm_requests: list[dict[str, Any]] = field(default_factory=list)
    tool_requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.halted


def _artifact_path(bundle_dir: Path, ref: Any) -> Path | None:
    """Resolve a bundle artifact reference without allowing it to escape."""
    if not isinstance(ref, str) or not ref:
        return None
    rel = PurePosixPath(ref)
    if rel.is_absolute() or PureWindowsPath(ref).is_absolute():
        return None
    error = ValueError(f"artifact reference {ref!r} escapes the bundle's artifacts/ directory")
    if rel.parts[:1] != ("artifacts",) or any(part in (".", "..") for part in rel.parts):
        raise error

    bundle_root = bundle_dir.resolve()
    artifacts_root = (bundle_root / "artifacts").resolve()
    resolved = bundle_root.joinpath(*rel.parts).resolve()
    if not artifacts_root.is_relative_to(bundle_root) or not resolved.is_relative_to(artifacts_root):
        raise error
    return resolved


def _load_artifact(bundle_dir: Path, ref: Any) -> Any:
    """The JSON payload behind a (bundle-relative) artifact reference.

    A reference that is still absolute (the source file was missing at pack
    time) or that does not exist in the bundle yields ``None`` — the caller
    decides whether that is fatal.
    """
    resolved = _artifact_path(bundle_dir, ref)
    if resolved is None:
        return None
    if not resolved.is_file():
        return None
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_recording(bundle_dir: Path) -> Recording:
    """Parse a bundle directory into the ordered calls replay feeds from.

    ``spans.jsonl`` is deduplicated by span id keeping the last record (a root
    turn span is checkpointed at open and emitted again at close; the close
    record wins and its position preserves chronological order for sequential
    turns). Model calls are any span carrying an ``llm.output`` artifact
    reference; tool calls any span carrying a ``tool.input`` reference — by
    artifact key rather than span name, because a skill-tool call is retyped
    to ``skill.read`` but records the same artifacts.
    """
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    spans_path = bundle_dir / "spans.jsonl"
    if not manifest_path.is_file() or not spans_path.is_file():
        raise ValueError(f"{bundle_dir} is not a trajectory bundle (missing manifest.json/spans.jsonl)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    deduped: dict[str, dict[str, Any]] = {}
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(span, dict):
            continue
        span_id = span.get("spanId") or f"anon-{len(deduped)}"
        if span_id in deduped:
            del deduped[span_id]
        deduped[span_id] = span

    llm_calls: list[RecordedLLMCall] = []
    tool_calls: list[RecordedToolCall] = []
    turns: list[RecordedTurn] = []
    for span in deduped.values():
        attrs = span.get("attributes") or {}
        if attrs.get("llm.output.artifact_path") or span.get("name") == "llm.call":
            llm_calls.append(
                RecordedLLMCall(
                    input=_load_artifact(bundle_dir, attrs.get("llm.input.artifact_path")),
                    output=_load_artifact(bundle_dir, attrs.get("llm.output.artifact_path")),
                    stream=bool(attrs.get("llm.stream")),
                    trace_id=span.get("traceId"),
                )
            )
            continue
        if attrs.get("tool.input.artifact_path") or attrs.get("tool.output.artifact_path"):
            tool_input = _load_artifact(bundle_dir, attrs.get("tool.input.artifact_path")) or {}
            tool_output = _load_artifact(bundle_dir, attrs.get("tool.output.artifact_path")) or {}
            result = tool_output.get("result")
            tool_calls.append(
                RecordedToolCall(
                    name=tool_input.get("name"),
                    params=tool_input.get("params"),
                    result=result if isinstance(result, str) else None,
                )
            )
            continue
        if attrs.get("turn.input.artifact_path"):
            payload = _load_artifact(bundle_dir, attrs.get("turn.input.artifact_path")) or {}
            content = payload.get("content")
            if isinstance(content, str) and content:
                turns.append(
                    RecordedTurn(
                        content=content,
                        channel=payload.get("channel"),
                        chat_id=payload.get("chat_id"),
                        session_key=attrs.get("session.key"),
                        trace_id=span.get("traceId"),
                    )
                )

    return Recording(
        bundle_dir=bundle_dir,
        manifest=manifest,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        turns=turns,
    )


def _normalize_text(text: str) -> str:
    """Mask only the text the harness regenerates freshly on every run.

    Scoped to the runtime-context clock line and the untrusted-fence marker
    lines — never applied to arbitrary content, so a date in a user message or
    a tool argument still counts as a real divergence.
    """
    text = _CLOCK_LINE_RE.sub(r"\1 <clock>", text)
    return _UNTRUSTED_MARKER_LINE_RE.sub(lambda m: _NONCE_RE.sub("#<nonce>", m.group(0)), text)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, str):
        return _normalize_text(value)
    return value


def _canonical_message(msg: Any) -> Any:
    if not isinstance(msg, dict):
        return _canonical(msg)
    out: dict[str, Any] = {"role": msg.get("role")}
    if msg.get("role") == "system":
        out["content"] = "<system prompt: not compared>"
    else:
        out["content"] = _canonical(msg.get("content"))
    for key in ("name", "tool_call_id", "reasoning_content", "thinking_blocks"):
        if msg.get(key) is not None:
            out[key] = _canonical(msg[key])
    calls = msg.get("tool_calls")
    if calls:
        rendered = []
        for tc in calls:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            rendered.append({"name": fn.get("name"), "arguments": _canonical(args)})
        out["tool_calls"] = rendered
    return out


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _first_diff_excerpt(expected: str, actual: str, window: int = 60) -> str:
    pos = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b), min(len(expected), len(actual)))
    lo = max(0, pos - window)

    def clip(s: str) -> str:
        seg = s[lo : pos + window]
        prefix = "…" if lo > 0 else ""
        suffix = "…" if pos + window < len(s) else ""
        return f"{prefix}{seg}{suffix}"

    return f"expected {clip(expected)!r}, got {clip(actual)!r}"


def compare_llm_request(
    recorded_input: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    model: str | None,
) -> Mismatch | None:
    """First mismatch between a live request and the recorded ``llm.input``;
    ``None`` when they match under normalization."""
    recorded_model = recorded_input.get("model")
    if recorded_model and model and recorded_model != model:
        return Mismatch("model", f"expected {recorded_model!r}, got {model!r}", recorded_model, model)

    # Cache breakpoints are placed per token strategy, not per conversation;
    # strip() also undoes the block-list rewrite a mark forces on string
    # content, so a marked recording compares equal to an unmarked replay.
    recorded_msgs, recorded_tool_defs = prompt_cache.strip(
        recorded_input.get("messages") or [], recorded_input.get("tools")
    )
    live_msgs, live_tool_defs = prompt_cache.strip(list(messages or []), tools)
    if len(recorded_msgs) != len(live_msgs):
        return Mismatch(
            "messages.length",
            f"expected {len(recorded_msgs)} message(s), got {len(live_msgs)}",
            len(recorded_msgs),
            len(live_msgs),
        )
    for i, (rec, live) in enumerate(zip(recorded_msgs, live_msgs)):
        rec_c, live_c = _dump(_canonical_message(rec)), _dump(_canonical_message(live))
        if rec_c != live_c:
            return Mismatch(f"messages[{i}]", _first_diff_excerpt(rec_c, live_c), rec, live)

    def names(items: list[dict[str, Any]] | None) -> list[Any]:
        return [((t or {}).get("function") or {}).get("name") for t in (items or [])]

    recorded_tools, live_tools = names(recorded_tool_defs), names(live_tool_defs)
    if recorded_tools != live_tools:
        return Mismatch("tools", f"expected {recorded_tools!r}, got {live_tools!r}", recorded_tools, live_tools)
    return None


_HALTED_CLASSIFICATION = ErrorClassification(category="replay_divergence")


def _halted_response(detail: str) -> LLMResponse:
    """The error-shaped response a halted feed answers with.

    Raising here would not reach the driver: ``_chat_attempt_with_retry``
    converts any provider exception into an error response. Answering with an
    explicitly non-retryable classification ends the turn on the loop's normal
    error path instead, and the driver reads the halt off the shared state.
    """
    return LLMResponse(
        content=f"Error calling LLM (replay_divergence): {detail}",
        finish_reason="error",
        error_classification=_HALTED_CLASSIFICATION,
    )


class ReplayProvider(LLMProvider):
    """Feeds recorded ``llm.output`` payloads back in recording order.

    See the module docstring for the divergence policy and the field/
    normalization choices behind :func:`compare_llm_request`.
    """

    def __init__(self, recording: Recording, state: ReplayState):
        super().__init__()
        self._recording = recording
        self._state = state

    def get_default_model(self) -> str:
        return self._recording.model or "replay"

    def _next_response(
        self,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        model: str | None,
        *,
        stream: bool,
    ) -> LLMResponse:
        state = self._state
        if state.halted:
            return _halted_response("replay already halted; no further calls are fed")
        state.llm_requests.append(
            {
                "model": model,
                "stream": stream,
                "messages": copy.deepcopy(list(messages or [])),
                "tools": [((t or {}).get("function") or {}).get("name") for t in (tools or [])],
            }
        )
        index = state.llm_cursor
        if index >= len(self._recording.llm_calls):
            div = Divergence(
                kind="llm",
                index=index,
                fatal=True,
                field="exhausted",
                detail=f"the harness asked for call #{index + 1} but only {index} were recorded",
            )
            state.record(div)
            return _halted_response(div.render())
        state.llm_cursor += 1
        rec = self._recording.llm_calls[index]

        if rec.output is None:
            div = Divergence(
                kind="llm",
                index=index,
                fatal=True,
                field="missing output",
                detail="the recorded llm.output artifact is missing from the bundle; nothing to feed",
            )
            state.record(div)
            return _halted_response(div.render())

        mismatch: Mismatch | None = None
        if stream != rec.stream:
            recorded_as, requested_as = ("streaming", "non-streaming") if rec.stream else ("non-streaming", "streaming")
            mismatch = Mismatch(
                "stream mode", f"recorded as {recorded_as}, requested as {requested_as}", recorded_as, requested_as
            )
        elif rec.input is not None:
            mismatch = compare_llm_request(rec.input, messages, tools, model)
        if mismatch is not None:
            fatal = state.mode == "strict"
            div = Divergence(
                kind="llm",
                index=index,
                fatal=fatal,
                field=mismatch.field,
                detail=mismatch.detail,
                expected=mismatch.expected,
                actual=mismatch.actual,
            )
            state.record(div)
            if fatal:
                return _halted_response(div.render())

        state.llm_fed += 1
        if stream:
            state.llm_streamed += 1
        return self._to_response(rec.output, index)

    @staticmethod
    def _to_response(output: dict[str, Any], index: int) -> LLMResponse:
        tool_calls = [
            ToolCallRequest(
                id=tc.get("id") or f"replay-{index}-{j}",
                name=tc.get("name") or "",
                arguments=tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {},
            )
            for j, tc in enumerate(output.get("tool_calls") or [])
        ]
        return LLMResponse(
            content=output.get("content"),
            tool_calls=tool_calls,
            finish_reason=output.get("finish_reason") or "stop",
            usage=output.get("usage") or {},
            reasoning_content=output.get("reasoning_content"),
            thinking_blocks=output.get("thinking_blocks"),
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return self._next_response(messages, tools, model, stream=False)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Replay one recorded response as a delta stream.

        Recorded output is re-chunked at a fixed granularity (reasoning first,
        then content), not token-by-token: the aggregate is what replay
        promises to reproduce, and no recorded trajectory holds the original
        chunk boundaries.
        """
        response = self._next_response(messages, tools, model, stream=True)
        if response.finish_reason == "error":
            yield StreamDelta(
                content=response.content,
                finish_reason="error",
                error_classification=response.error_classification,
            )
            return
        for chunk in _chunks(response.reasoning_content):
            yield StreamDelta(content=None, reasoning_content=chunk)
        for chunk in _chunks(response.content):
            yield StreamDelta(content=chunk)
        tool_call_delta: dict[str, Any] | None = None
        if response.tool_calls:
            tool_call_delta = {
                "tool_calls": [
                    {
                        "index": j,
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                    }
                    for j, tc in enumerate(response.tool_calls)
                ]
            }
        yield StreamDelta(
            content=None,
            tool_call_delta=tool_call_delta,
            usage=response.usage or None,
            finish_reason=response.finish_reason,
        )


def _chunks(text: str | None, size: int = _STREAM_CHUNK_CHARS) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


class ReplayToolRegistry(ToolRegistry):
    """Answers ``execute`` with recorded tool results — no real dispatch, ever.

    Matching is by order; the live call's name and arguments are compared
    against the recorded ``tool.input`` under the same normalization and
    strict/warn semantics as the model feed. ``get_definitions`` serves the
    tool schemas the recording offered the model (from the first recorded
    ``llm.input``), so the live loop advertises the recorded tool surface.
    """

    def __init__(self, recording: Recording, state: ReplayState):
        super().__init__()
        self._recording = recording
        self._state = state
        self._definitions: list[dict[str, Any]] = next(
            (list(c.input.get("tools") or []) for c in recording.llm_calls if c.input), []
        )

    def get_definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions)

    async def execute(self, name: str, params: dict[str, Any], *, run_meta: Any = None) -> str:
        state = self._state
        if state.halted:
            return "Error: replay halted; no further tool results are fed."
        state.tool_requests.append({"name": name, "params": copy.deepcopy(params)})
        index = state.tool_cursor
        if index >= len(self._recording.tool_calls):
            div = Divergence(
                kind="tool",
                index=index,
                fatal=True,
                field="exhausted",
                detail=f"the harness asked for tool call #{index + 1} but only {index} were recorded",
            )
            state.record(div)
            return f"Error: replay halted: {div.render()}"
        state.tool_cursor += 1
        rec = self._recording.tool_calls[index]

        mismatch: Mismatch | None = None
        if rec.name is not None and name != rec.name:
            mismatch = Mismatch("tool name", f"expected {rec.name!r}, got {name!r}", rec.name, name)
        elif rec.params is not None:
            rec_p, live_p = _dump(_canonical(rec.params)), _dump(_canonical(params))
            if rec_p != live_p:
                mismatch = Mismatch("tool params", _first_diff_excerpt(rec_p, live_p), rec.params, params)
        if mismatch is not None:
            fatal = state.mode == "strict"
            div = Divergence(
                kind="tool",
                index=index,
                fatal=fatal,
                field=mismatch.field,
                detail=mismatch.detail,
                expected=mismatch.expected,
                actual=mismatch.actual,
            )
            state.record(div)
            if fatal:
                return f"Error: replay halted: {div.render()}"

        if rec.result is None:
            div = Divergence(
                kind="tool",
                index=index,
                fatal=True,
                field="missing output",
                detail="the recorded tool.output artifact is missing from the bundle; nothing to feed",
            )
            state.record(div)
            return f"Error: replay halted: {div.render()}"

        state.tool_fed += 1
        return rec.result


def _parse_ts(value: Any) -> datetime | None:
    """A session timestamp as an aware datetime; ``None`` when unparseable.

    Session records stamp naive local time; reading a naive value in the local
    timezone is exact on the recording machine (the dominant replay case) and
    documented slack elsewhere.
    """
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts.astimezone() if ts.tzinfo is None else ts


def _session_records(session_path: Path) -> list[dict[str, Any]]:
    """The message records of a ``session.jsonl`` file (metadata rows skipped)."""
    records: list[dict[str, Any]] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("_type") != "metadata":
            records.append(data)
    return records


def diagnose_history_cut(recording: Recording) -> str:
    """Whether the attempt's starting point in the session history is locatable.

    ``"no-history"`` — no session file or no turns (file-level checks own that
    case); ``"located"`` — :func:`_history_cut` found the cut (0 included: a
    session with no pre-attempt history is fine); ``"unlocatable"`` — the cut
    is ambiguous, so a replay will seed empty history and a mid-session
    attempt's first request will diverge. Diagnostic only; the cut semantics
    live solely in :func:`_history_cut`.
    """
    session_path = recording.bundle_dir / "session.jsonl"
    if not session_path.is_file() or not recording.turns:
        return "no-history"
    records = _session_records(session_path)
    return "located" if _history_cut(recording, records) is not None else "unlocatable"


def validate_recording(recording: Recording) -> list[str]:
    """Stable categories for every payload shape replay would crash on.

    ``load_recording`` verifies turn and tool payloads by consuming them, but
    LLM input/output and manifest fields flow through unshaped and only crash
    at their consumption points (``_to_response``, ``compare_llm_request``,
    ``_history_cut``, ``run_replay``). This validator states the consumption
    contract explicitly — new dereferences belong here too — so completeness
    checks can classify a bad recording deterministically instead of blaming
    a probe crash. Returns deduplicated human-readable categories; empty means
    the shapes are consumable.
    """
    problems: list[str] = []

    def _add(category: str) -> None:
        if category not in problems:
            problems.append(category)

    manifest = recording.manifest
    if not isinstance(manifest, dict):
        _add("the manifest is not an object")
    else:
        time_range = manifest.get("time_range")
        if time_range is not None and not isinstance(time_range, dict):
            _add("the manifest time_range is not an object")
    for turn in recording.turns:
        if turn.session_key is not None and not isinstance(turn.session_key, str):
            _add("a turn session key is not a string")
    for call in recording.llm_calls:
        output = call.output
        if output is not None and not isinstance(output, dict):
            _add("a model call output payload is not an object")
        elif isinstance(output, dict):
            tool_calls = output.get("tool_calls")
            if tool_calls is not None and (
                not isinstance(tool_calls, list) or any(not isinstance(tc, dict) for tc in tool_calls)
            ):
                _add("a model call output tool_calls entry is not an object")
            for key in ("content", "reasoning_content", "finish_reason"):
                value = output.get(key)
                if value is not None and not isinstance(value, str):
                    _add(f"a model call output {key} is not a string")
            usage = output.get("usage")
            if usage is not None and not isinstance(usage, dict):
                _add("a model call output usage is not an object")
            thinking = output.get("thinking_blocks")
            if thinking is not None and not isinstance(thinking, list):
                _add("a model call output thinking_blocks is not a list")
        payload = call.input
        if payload is not None and not isinstance(payload, dict):
            _add("a model call input payload is not an object")
        elif isinstance(payload, dict):
            for key in ("messages", "tools"):
                value = payload.get(key)
                if value is not None and (
                    not isinstance(value, list) or any(not isinstance(item, dict) for item in value)
                ):
                    _add(f"a model call input {key} entry is not an object")
            model = payload.get("model")
            if model is not None and not isinstance(model, str):
                _add("a model call input model is not a string")
    return problems


def _history_cut(recording: Recording, records: list[dict[str, Any]]) -> int | None:
    """The index of the attempt's own opening record; ``None`` = unlocatable.

    The cut is located by time first and confirmed by content, because
    neither signal is safe alone: the attempt's first input may repeat text
    the user typed long before (a bare ``go``/``yes``), so taking the first
    content match cuts too early, while timestamps can disagree with the
    manifest window when a bundle is replayed under a different timezone.

    1. With ``time_range.start`` and every record timestamped, cut at the
       first record at-or-after start. A user message there equal to the
       first turn input confirms the cut; if nothing in the whole session
       matches that input (persistence transformed it), the time cut stands
       on its own — but only when it found attempt records at all.
    2. When time and content disagree (matches exist away from the time cut),
       content wins only inside the attempt window [start, end]: cut at the
       first match within it.
    3. Without a usable time anchor, only a *unique* content match is
       accepted — the attempt's own opening message is in the file whenever
       its text survived persistence verbatim, so a unique match is it.
    """
    if not records or not recording.turns:
        return None
    first_input = recording.turns[0].content
    matches = [i for i, m in enumerate(records) if m.get("role") == "user" and m.get("content") == first_input]

    time_range = recording.manifest.get("time_range") or {}
    start = _parse_ts(time_range.get("start"))
    end = _parse_ts(time_range.get("end"))
    stamps = [_parse_ts(m.get("timestamp")) for m in records]

    if start is not None and all(s is not None for s in stamps):
        cut = next((i for i, s in enumerate(stamps) if s >= start), None)
        if cut is not None:
            msg = records[cut]
            if msg.get("role") == "user" and msg.get("content") == first_input:
                return cut
            if not matches:
                return cut
        in_window = [i for i in matches if start <= stamps[i] and (end is None or stamps[i] <= end)]
        if in_window:
            return in_window[0]
        return None

    if len(matches) == 1:
        return matches[0]
    return None


def _pre_attempt_messages(recording: Recording) -> list[dict[str, Any]]:
    """The session messages that predate the attempt, from ``session.jsonl``.

    Everything before the cut :func:`_history_cut` locates; when the cut is
    ambiguous this seeds nothing — missing history surfaces as a visible
    divergence, whereas guessing can preload the attempt's own (or later)
    messages and silently corrupt every request after the cut.
    """
    session_path = recording.bundle_dir / "session.jsonl"
    if not session_path.is_file() or not recording.turns:
        return []
    records = _session_records(session_path)
    cut = _history_cut(recording, records)
    return records[:cut] if cut is not None else []


async def run_replay(bundle_dir: Path, mode: str = "warn") -> ReplayReport:
    """Drive an ``AgentLoop`` through a bundle's recorded turns.

    The loop runs in a fresh temporary workspace (session writes stay out of
    the user's real one), seeded with the pre-attempt conversation from the
    bundle's ``session.jsonl``, with tracing suppressed for this task tree —
    see the module docstring for both. The loop's tool registry is swapped for
    the replay registry after construction, so none of the real tools the
    constructor registers is reachable; MCP connect and the exec executor are
    run()-time steps this driver never takes.

    Each turn is driven down the path the recording took: a turn whose trace
    holds streamed model calls (``llm.stream``) replays through the streaming
    aggregation (``_llm_call_stream``), the rest through the non-streaming
    path. Ending with unconsumed recorded calls is itself a divergence —
    fatal in strict mode, recorded in warn mode.
    """
    if mode not in REPLAY_MODES:
        raise ValueError(f"mode must be one of {REPLAY_MODES}; got {mode!r}")
    recording = load_recording(bundle_dir)
    if not recording.turns:
        raise ValueError(f"{bundle_dir} holds no recorded turn inputs; nothing to drive the replay with")

    from raven.agent.loop import AgentLoop
    from raven.session.manager import SessionManager
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    state = ReplayState(mode=mode)
    provider = ReplayProvider(recording, state)
    registry = ReplayToolRegistry(recording, state)
    streamed_traces = {c.trace_id for c in recording.llm_calls if c.stream and c.trace_id}

    async def _drop_delta(_text: str) -> None:
        return None

    workspace = Path(tempfile.mkdtemp(prefix="raven-replay-"))
    replies: list[str | None] = []
    turns_replayed = 0
    try:
        with trace.suppress():
            sessions = SessionManager(workspace)
            session_key = recording.turns[0].session_key or recording.manifest.get("session_key")
            pre_attempt = _pre_attempt_messages(recording)
            if session_key and pre_attempt:
                session = sessions.get_or_create(session_key)
                for msg in pre_attempt:
                    session.record(dict(msg))
                sessions.save(session)

            loop = AgentLoop(
                provider=provider,
                workspace=workspace,
                model=recording.model,
                restrict_to_workspace=True,
                session_manager=sessions,
            )
            loop.tools = registry
            for turn in recording.turns:
                if state.halted:
                    break
                # The recording's runtime-context header renders the source's
                # channel/chat id into the user message, and a live session key
                # is "<channel>:<chat_id>" — splitting it reproduces the
                # recorded source identity exactly. The artifact's
                # channel/chat_id fields are the fallback for keys that don't
                # split.
                channel, _, chat_id = (turn.session_key or "").partition(":")
                req = TurnRequest(
                    origin=Origin.USER,
                    source=Source(
                        channel=channel or turn.channel or "replay",
                        chat_id=chat_id or turn.chat_id or "replay",
                        sender_id="replay",
                        chat_type=ChatType.DM,
                    ),
                    text=turn.content,
                )
                on_token_delta = _drop_delta if turn.trace_id in streamed_traces else None
                result = await loop._process_message(req, session_key=turn.session_key, on_token_delta=on_token_delta)
                replies.append(result[0] if result else None)
                turns_replayed += 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # A run that never asked for the rest of the recording diverged just as
    # surely as one that asked for the wrong thing — the fixed harness stopped
    # earlier than the recorded one did. Strict keeps its first-divergence
    # contract: the first fatal record ends the audit; warn lists every
    # leftover kind.
    if not state.halted:
        for kind, cursor, total in (
            ("llm", state.llm_cursor, len(recording.llm_calls)),
            ("tool", state.tool_cursor, len(recording.tool_calls)),
        ):
            leftover = total - cursor
            if leftover > 0:
                state.record(
                    Divergence(
                        kind=kind,
                        index=cursor,
                        fatal=mode == "strict",
                        field="unconsumed",
                        detail=f"{leftover} recorded {kind} call(s) were never requested by the harness",
                    )
                )
                if state.halted:
                    break

    return ReplayReport(
        bundle_dir=recording.bundle_dir,
        mode=mode,
        turns_replayed=turns_replayed,
        turns_recorded=len(recording.turns),
        llm_calls_replayed=state.llm_fed,
        llm_calls_recorded=len(recording.llm_calls),
        llm_calls_streamed=state.llm_streamed,
        tool_calls_replayed=state.tool_fed,
        tool_calls_recorded=len(recording.tool_calls),
        divergences=list(state.divergences),
        halted=state.halted,
        replies=replies,
        llm_requests=list(state.llm_requests),
        tool_requests=list(state.tool_requests),
    )
