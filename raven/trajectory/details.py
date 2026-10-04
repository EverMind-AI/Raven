"""Detail descriptors and block bodies for one trajectory entry.

The Web detail pane shows an entry as an overview (type, status, one summary
per block) followed by one tab per block. This module produces both from a
single consistent :class:`~raven.trajectory.index.EntryView`, captured under
one index lock and then read from disk with the index released, so a refresh
that lands mid-read can never pair a newer body with an older revision.

Contract:

- Blocks come from a registry keyed by entry kind: the kind's own blocks in
  a fixed order, then the common tail ``error`` (only when the operation
  failed), ``timing``, ``relations``, ``integrity`` (only when non-empty)
  and ``raw``. A required block that has no data stays with
  ``not_recorded``; an optional block appears only with evidence -- or, when
  its artifact could not be opened within the describe budget, with
  ``reason="not_loaded"`` so the tab is not silently lost.
- Availability is one of ``available`` / ``empty`` / ``not_recorded`` /
  ``missing`` / ``truncated`` / ``unreadable`` / ``unsupported``. Empty
  strings, empty lists, ``False`` and ``0`` are real values; ``empty`` is
  reserved for a recorded empty collection.
- Every artifact is read through the conversation layer's bounded reader
  (``logs/`` only, 512 KiB); message blobs resolve through
  :mod:`raven.tracing.artifact_v2`. A body or descriptor is measured as the
  transport would serialize it and degraded to fit ``RESPONSE_LIMIT`` with an
  explicit truncation marker.
- A tool's schema is attached only when the record proves which model call
  requested the tool: same trace, same parent span, a sequential run of model
  calls under that parent, and the tool started in the window between the
  requesting call's end and the next call's start. Anything less is
  ``schema_unproven``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from raven.tracing import artifact_v2
from raven.trajectory import conversation as _conv
from raven.trajectory import entries as _entries
from raven.trajectory import store as tstore
from raven.trajectory.index import CursorExpiredError, EntryView, SessionIndex

AVAILABLE = "available"
EMPTY = "empty"
NOT_RECORDED = "not_recorded"
MISSING = "missing"
TRUNCATED = "truncated"
UNREADABLE = "unreadable"
UNSUPPORTED = "unsupported"
AVAILABILITIES = (AVAILABLE, EMPTY, NOT_RECORDED, MISSING, TRUNCATED, UNREADABLE, UNSUPPORTED)

TEXT, JSON, MESSAGES, KEY_VALUES, ITEMS, REFERENCES = "text", "json", "messages", "key_values", "items", "references"
RENDERERS = (TEXT, JSON, MESSAGES, KEY_VALUES, ITEMS, REFERENCES)

ARTIFACT_LIMIT = _conv._ARTIFACT_LIMIT
RESPONSE_LIMIT = 1024 * 1024
RESPONSE_RESERVE = 64 * 1024
VALUE_LIMIT = 64 * 1024
JSON_DEPTH = 8
MESSAGES_PAGE = 20
ITEMS_PAGE = 50
PREVIEW_CHARS = 600
PREVIEW_LINES = 6
PREVIEW_ITEM_CHARS = 200
PREVIEW_ITEMS = 3
PREVIEW_MESSAGES = 2
PREVIEW_JSON_KEYS = 6
DESCRIBE_ARTIFACTS = 16
DESCRIBE_BLOBS = 4
DESCRIBE_BYTES_LIMIT = 4 * 1024 * 1024

REASON_NOT_LOADED = "not_loaded"
REASON_SCHEMA_UNPROVEN = "schema_unproven"
REASON_PREVIEW_DROPPED = "preview_dropped"

NOTE_OUTER_ONLY = "outer_only"
NOTE_IN_PROGRESS = "in_progress_snapshot"
NOTE_INCOMPLETE = "data_incomplete"

_ARTIFACT_META_SUFFIXES = _conv._ARTIFACT_META_SUFFIXES
_OPTIONAL_DEPENDENT = {
    "media",
    "toolCalls",
    "usage",
    "response",
    "tools",
    "request",
    "system",
    "prompt",
    "thinkingBlocks",
}


class UnknownBlockError(Exception):
    """``block_id`` is not one of the entry's blocks."""


class EntryGoneError(Exception):
    """The entry is no longer in the index."""


class RevisionChangedError(Exception):
    """The entry's revision or epoch moved on since the caller looked."""

    def __init__(self, current_revision: int, current_epoch: str) -> None:
        super().__init__(f"entry is now revision {current_revision} in epoch {current_epoch}")
        self.current_revision = current_revision
        self.current_epoch = current_epoch


# ── data types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BlockSpec:
    id: str
    renderer: str
    required: bool
    source: str
    field: str | None = None
    related: str | None = None


@dataclass(frozen=True)
class BlockDescriptor:
    id: str
    renderer: str
    availability: str
    preview: Any
    total_items: int | None
    related_operation: str | None
    reason: str | None


@dataclass(frozen=True)
class Descriptor:
    session_key: str
    epoch: str
    entry_id: str
    entry_revision: int
    kind: str
    span_name: str
    slot: str
    operation_status: str
    status_evidence: tuple[str, ...]
    failure_entry: bool
    integrity: tuple[str, ...]
    notes: tuple[str, ...]
    blocks: tuple[BlockDescriptor, ...]
    revision_changed: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class BlockBody:
    entry_id: str
    entry_revision: int
    epoch: str
    block_id: str
    renderer: str
    availability: str
    reason: str | None
    data: Any
    next_cursor: str | None
    total_items: int | None
    integrity: tuple[str, ...]
    truncated: bool


# ── block registry ────────────────────────────────────────────────────
# source kinds: "artifact:<key>" (payload field via ``field``), "attrs:<prefix or comma list>",
# "derived:<name>", "schema", "turn", "skeleton".

_A = "artifact:"


def _art(
    id_: str, renderer: str, key: str, field: str | None = None, *, required: bool = True, related: str | None = None
) -> BlockSpec:
    return BlockSpec(id_, renderer, required, f"{_A}{key}", field, related)


def _attrs(id_: str, renderer: str, keys: str, *, required: bool = True) -> BlockSpec:
    return BlockSpec(id_, renderer, required, f"attrs:{keys}")


_REGISTRY: dict[str, tuple[BlockSpec, ...]] = {
    "user.input": (
        _art("content", TEXT, "turn.input", "content"),
        _art("media", ITEMS, "turn.input", "media", required=False),
        BlockSpec("origin", KEY_VALUES, True, "derived:turn_origin"),
    ),
    "agent.reply": (
        _art("content", TEXT, "turn.output", "content"),
        _attrs(
            "capabilities",
            KEY_VALUES,
            "turn.tools,turn.tool_count,turn.plugin.backend,turn.plugin.tools,turn.skills,turn.skill_count",
            required=False,
        ),
    ),
    "turn.marker": (BlockSpec("turn", KEY_VALUES, True, "derived:turn"),),
    "llm.input": (
        _art("messages", MESSAGES, "llm.input", "messages"),
        _art("system", TEXT, "llm.input", "systemPrompt", required=False),
        _art("prompt", TEXT, "llm.input", "prompt", required=False),
        BlockSpec("model", KEY_VALUES, True, "derived:llm_model"),
        _art("tools", ITEMS, "llm.input", "tools", required=False),
        _art("request", KEY_VALUES, "llm.input", "request", required=False),
    ),
    "llm.thinking": (
        BlockSpec("thinking", TEXT, True, "derived:thinking", related="llm.output"),
        _art("thinkingBlocks", JSON, "llm.output", "thinking_blocks", required=False, related="llm.output"),
    ),
    "llm.output": (
        _art("content", TEXT, "llm.output", "content"),
        _art("toolCalls", ITEMS, "llm.output", "tool_calls", required=False),
        BlockSpec("finish", KEY_VALUES, True, "derived:llm_finish"),
        BlockSpec("usage", KEY_VALUES, False, "derived:llm_usage"),
        BlockSpec("response", JSON, False, "derived:llm_response"),
    ),
    "tool.input": (
        BlockSpec("tool", KEY_VALUES, True, "derived:tool"),
        _art("params", JSON, "tool.input", "params"),
        BlockSpec("schema", JSON, True, "schema", related="llm.input"),
    ),
    "tool.output": (
        _art("result", TEXT, "tool.output", "result"),
        BlockSpec("tool", KEY_VALUES, True, "derived:tool"),
        _art("params", JSON, "tool.input", "params", related="tool.input"),
        BlockSpec("schema", JSON, True, "schema", related="llm.input"),
    ),
    "skill.read": (
        _attrs("skill", KEY_VALUES, "skill."),
        _art("params", JSON, "tool.input", "params", related="tool.input"),
        _art("content", TEXT, "tool.output", "result"),
        _attrs("origin", KEY_VALUES, "skill.read.,skill.source,skill.path,tool.name"),
    ),
    "skill.inject": (
        _art("skills", ITEMS, "skill.inject", "skills"),
        BlockSpec("origin", KEY_VALUES, True, "derived:inject_origin"),
        BlockSpec("stats", KEY_VALUES, True, "derived:inject_stats"),
    ),
    "skill.rewrite.input": (_art("query", TEXT, "skill.rewrite.input", "query"),),
    "skill.rewrite.output": (
        _art("decision", KEY_VALUES, "skill.rewrite.output", "need_retrieval"),
        _art("query", TEXT, "skill.rewrite.output", "rewritten_query"),
    ),
    "skill.gate.input": (
        _art("task", TEXT, "skill.gate.input", "task"),
        _art("candidates", ITEMS, "skill.gate.input", "candidates"),
        BlockSpec("capabilities", KEY_VALUES, True, "derived:gate_capabilities"),
    ),
    "skill.gate.output": (
        _art("skills", ITEMS, "skill.gate.output", "selected"),
        _attrs("stats", KEY_VALUES, "skill.gate.candidate_count,skill.gate.selected_count"),
    ),
    "context.curate.input": (BlockSpec("request", KEY_VALUES, True, "derived:curate_request"),),
    "context.curate.output": (
        _art("content", TEXT, "context.curate.output", "working_state"),
        BlockSpec("stats", KEY_VALUES, True, "derived:curate_stats"),
    ),
    "subagent.run": (
        _attrs("task", TEXT, "subagent.task"),
        _attrs("agent", KEY_VALUES, "subagent.label,subagent.task_id,subagent.origin_session"),
    ),
    "memory.recall": (
        _attrs("query", TEXT, "memory.query"),
        _attrs("settings", KEY_VALUES, "memory.scope,memory.user_id,memory.top_k,memory.hits"),
        _art("hits", ITEMS, "memory.recall"),
    ),
    "memory.store": (
        _art("messages", MESSAGES, "memory.store", "messages"),
        BlockSpec("stats", KEY_VALUES, True, "derived:store_stats"),
    ),
    "memory.feedback": (
        _attrs("injected", ITEMS, "memory.injected"),
        _attrs("used", ITEMS, "memory.used"),
        _attrs("origin", KEY_VALUES, "memory.session_id"),
    ),
    "memory.extract": (
        _attrs("settings", KEY_VALUES, "memory.surface,memory.model,memory.enable_foresight"),
        _attrs("stats", KEY_VALUES, "memory.message_count"),
        _attrs("result", KEY_VALUES, "memory.annotated"),
    ),
    "memory.profile_refresh": (
        _attrs("settings", KEY_VALUES, "memory.model,memory.threshold"),
        _attrs("stats", KEY_VALUES, "memory.sections_rewritten"),
    ),
    "memory.consolidate": (
        _attrs("position", KEY_VALUES, "memory.session_key,memory.last_consolidated"),
        _attrs("stats", KEY_VALUES, "memory.message_count"),
    ),
    "memory.enqueue": (_attrs("operation", KEY_VALUES, "memory."),),
    "personalize.classify.input": (
        _art("content", TEXT, "personalize.input", "message"),
        _art("messages", MESSAGES, "personalize.input", "history", required=False),
    ),
    "personalize.classify.output": (_art("result", JSON, "personalize.output", "result"),),
    "personalize.question.input": (BlockSpec("request", KEY_VALUES, True, "derived:personalize_request"),),
    "personalize.question.output": (_art("content", TEXT, "personalize.output", "result"),),
    "personalize.extract.input": (
        _art("content", TEXT, "personalize.input", "original_message"),
        BlockSpec("qa", KEY_VALUES, True, "derived:personalize_qa"),
    ),
    "personalize.extract.output": (_art("result", KEY_VALUES, "personalize.output", "result"),),
    "personalize.postlearn.input": (
        _art("content", TEXT, "personalize.input", "message"),
        _art("summary", TEXT, "personalize.input", "response_summary"),
    ),
    "personalize.postlearn.output": (_art("result", KEY_VALUES, "personalize.output", "result"),),
    "plugin.load": (
        _attrs("plugin", KEY_VALUES, "plugin.name"),
        _attrs("contribution", KEY_VALUES, "plugin.contribution"),
        _attrs("result", KEY_VALUES, "plugin.result_type,plugin.opt_out"),
    ),
}

_EXTERNAL = (
    _attrs(
        "agent",
        KEY_VALUES,
        "subagent.external.agent,subagent.external.transport,subagent.external.instance,subagent.external.session_id,subagent.external.resumed",
    ),
    _attrs(
        "result",
        KEY_VALUES,
        "subagent.external.answer_chars,subagent.external.elapsed_ms,subagent.external.stop_reason,subagent.external.exit_code",
    ),
    _attrs("usage", KEY_VALUES, "subagent.external.usage", required=False),
    _attrs(
        "stats",
        KEY_VALUES,
        "subagent.external.update_counts,subagent.external.tool_calls,subagent.external.tool_call_count,subagent.external.thought_chars",
        required=False,
    ),
    _art("transcript", TEXT, "subagent.external.transcript", required=False),
    BlockSpec("frames", REFERENCES, False, "derived:frames"),
)

_TAIL = (
    BlockSpec("error", KEY_VALUES, False, "derived:error"),
    BlockSpec("timing", KEY_VALUES, True, "derived:timing"),
    BlockSpec("relations", KEY_VALUES, True, "derived:relations"),
    BlockSpec("integrity", ITEMS, False, "derived:integrity"),
    BlockSpec("raw", JSON, True, "derived:raw"),
)

_EVIDENCE_KINDS = ("span.error", "span.malformed", "span.unreadable")


def _kind_specs(entry: _entries.TrajectoryEntry) -> tuple[BlockSpec, ...]:
    kind = entry.kind
    if kind in _REGISTRY:
        return _REGISTRY[kind]
    for prefix, specs in (
        ("subagent.external", _EXTERNAL),
        ("memory.store", _REGISTRY["memory.store"]),
        ("memory.feedback", _REGISTRY["memory.feedback"]),
        ("memory.extract", _REGISTRY["memory.extract"]),
        ("memory.profile_refresh", _REGISTRY["memory.profile_refresh"]),
        ("memory.consolidate", _REGISTRY["memory.consolidate"]),
        ("memory.enqueue", _REGISTRY["memory.enqueue"]),
        ("plugin.load", _REGISTRY["plugin.load"]),
    ):
        if kind.startswith(prefix):
            return specs
    if kind in _EVIDENCE_KINDS:
        return ()
    if entry.slot.startswith("artifact:"):
        key = entry.slot[len("artifact:") :]
        return (_art("content", JSON, key), BlockSpec("attributes", KEY_VALUES, True, "derived:attributes"))
    return (BlockSpec("attributes", KEY_VALUES, True, "derived:attributes"),)


def block_specs(kind: str, slot: str = "") -> tuple[BlockSpec, ...]:
    """The ordered block specs for an entry kind (own blocks, then the common tail)."""
    probe = _entries.TrajectoryEntry(
        entry_id="", kind=kind, span_name="", slot=slot, trace_id="", span_id="", parent_span_id=None,
        turn_span_id=None, turn_number=None, turn_start=False, origin="main", sort_key=(), event_time="",
        preview=None, operation_status="unknown", status_evidence=(), failure_entry=False, integrity=(),
        operation_start=None, operation_end=None, duration_ms=None, charged_ms=None, timing_basis="unknown",
        duration_owner=None, meta={},
    )  # fmt: skip
    return (*_kind_specs(probe), *_TAIL)


# ── bounded reading ───────────────────────────────────────────────────


class _Reader:
    """Per-call artifact reader: one read per path, counted against the describe budget."""

    def __init__(self, state: Path, *, max_artifacts: int | None, max_blobs: int | None, max_bytes: int | None) -> None:
        self.state = state
        self.max_artifacts = max_artifacts
        self.max_blobs = max_blobs
        self.max_bytes = max_bytes
        self.artifacts = 0
        self.blobs = 0
        self.bytes = 0
        self.cache: dict[str, tuple[str | None, str | None]] = {}

    def _allowed(self, is_blob: bool) -> bool:
        if self.max_bytes is not None and self.bytes >= self.max_bytes:
            return False
        if is_blob:
            return self.max_blobs is None or self.blobs < self.max_blobs
        return self.max_artifacts is None or self.artifacts < self.max_artifacts

    def read(self, path: str, *, is_blob: bool = False) -> tuple[str | None, str | None] | None:
        """(text, reason) like the conversation reader, or None when the budget is spent."""
        if path in self.cache:
            return self.cache[path]
        if not self._allowed(is_blob):
            return None
        text, reason = _conv._read_artifact(self.state, path, ARTIFACT_LIMIT)
        if is_blob:
            self.blobs += 1
        else:
            self.artifacts += 1
        self.bytes += len(text.encode("utf-8", errors="replace")) if text else 0
        self.cache[path] = (text, reason)
        return text, reason

    def blob(self, sha1: str) -> tuple[str | None, str | None] | None:
        return self.read(str(artifact_v2.message_path(self.state / "logs" / "audit-artifacts", sha1)), is_blob=True)


@dataclass
class _Loaded:
    """One artifact as the detail layer sees it.

    ``json_ok`` says whether the body parsed as JSON. A body that did not is
    still a readable artifact for a text consumer; only a consumer that needs
    structure reports it as unreadable (see :func:`_structured_problem`).
    """

    availability: str
    reason: str | None
    payload: Any
    text: str | None
    integrity: tuple[str, ...]
    loaded: bool
    json_ok: bool = True


def _structured_problem(load: _Loaded) -> tuple[str, str] | None:
    """(availability, integrity code) when a structured consumer cannot use ``load``."""
    if not load.loaded or load.availability in (AVAILABLE, NOT_RECORDED) and load.json_ok:
        return None
    if load.availability == AVAILABLE and not load.json_ok:
        return UNREADABLE, "artifact_unreadable"
    if load.availability in (MISSING, UNREADABLE, TRUNCATED):
        return load.availability, load.reason or load.availability
    return None


_REASON_RULES = (
    ("outside the trace store", UNREADABLE, "artifact_outside_store"),
    ("artifact missing", MISSING, "artifact_missing"),
    ("not a regular file", UNREADABLE, "artifact_unreadable"),
)


def _load(reader: _Reader, attrs: dict[str, Any], key: str) -> _Loaded:
    pointer = tstore._str_value(attrs.get(f"{key}.artifact_path"))
    if pointer is None:
        return _Loaded(NOT_RECORDED, NOT_RECORDED, None, None, (), True)
    result = reader.read(pointer)
    if result is None:
        return _Loaded(AVAILABLE, REASON_NOT_LOADED, None, None, (), False)
    text, reason = result
    if text is None:
        for needle, availability, code in _REASON_RULES:
            if reason and needle in reason:
                return _Loaded(availability, code, None, None, (code,), True)
        return _Loaded(MISSING, "artifact_missing", None, None, ("artifact_missing",), True)
    if reason is not None:
        return _Loaded(TRUNCATED, "artifact_truncated", None, text, ("artifact_truncated",), True)
    payload, ok = _conv._parse_json(text)
    if not ok:
        return _Loaded(AVAILABLE, None, None, text, (), True, json_ok=False)
    return _Loaded(AVAILABLE, None, payload, text, (), True)


def _field(loaded: _Loaded, field: str | None) -> tuple[bool, Any]:
    """(present, value) of ``field`` in a loaded JSON payload; whole payload when field is None."""
    if field is None:
        return True, loaded.payload
    if isinstance(loaded.payload, dict) and field in loaded.payload:
        return True, loaded.payload[field]
    return False, None


# ── serialization budget ──────────────────────────────────────────────


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, default=str))


def _limit_depth(value: Any, depth: int) -> tuple[Any, bool]:
    if depth <= 0 and isinstance(value, (dict, list)):
        return {"$depth_truncated": True}, True
    if isinstance(value, dict):
        hit = False
        out = {}
        for key, item in value.items():
            limited, flag = _limit_depth(item, depth - 1)
            out[str(key)] = limited
            hit = hit or flag
        return out, hit
    if isinstance(value, list):
        hit = False
        out_list = []
        for item in value:
            limited, flag = _limit_depth(item, depth - 1)
            out_list.append(limited)
            hit = hit or flag
        return out_list, hit
    return value, False


def _fit_text(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if _size({"text": text}) <= budget:
        return text, False
    keep = max(0, budget - 64)
    while keep > 0:
        candidate = encoded[:keep].decode("utf-8", errors="ignore")
        if _size({"text": candidate}) <= budget:
            return candidate, True
        keep = keep * 3 // 4
    return "", True


def _fit_json(value: Any, budget: int) -> tuple[Any, bool]:
    limited, truncated = _limit_depth(value, JSON_DEPTH)
    for depth in (JSON_DEPTH, 6, 4, 2):
        limited, flag = _limit_depth(value, depth)
        truncated = truncated or flag
        if _size({"value": limited}) <= budget:
            return limited, truncated
    return {"$oversize": True, "bytes": _size(value)}, True


def _fit_items(items: Sequence[Any], offset: int, page: int, budget: int) -> tuple[list[Any], int, bool]:
    """(page items, next offset, truncated) -- an item too large for the budget is replaced, never skipped."""
    out: list[Any] = []
    used = _size({"items": [], "offset": offset})
    truncated = False
    position = offset
    while position < len(items) and len(out) < page:
        item = items[position]
        cost = _size(item) + 2
        if used + cost > budget:
            if not out:
                out.append({"$oversize": True, "index": position, "bytes": _size(item)})
                position += 1
                truncated = True
            break
        out.append(item)
        used += cost
        position += 1
    return out, position, truncated


def _fit_key_values(items: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], bool]:
    truncated = False
    fitted: list[dict[str, Any]] = []
    for item in items:
        if _size(item["value"]) > VALUE_LIMIT:
            fitted.append({**item, "value": {"$oversize": True, "bytes": _size(item["value"])}})
            truncated = True
        else:
            fitted.append(item)
    while fitted and _size({"items": fitted}) > budget:
        omitted = [entry["key"] for entry in fitted[len(fitted) // 2 :]]
        fitted = fitted[: len(fitted) // 2] + [{"key": "$omitted", "value": omitted, "source": "derived"}]
        truncated = True
        if len(fitted) <= 2:
            break
    return fitted, truncated


# ── previews ──────────────────────────────────────────────────────────


def _clip(text: str, chars: int = PREVIEW_ITEM_CHARS) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"


def _text_preview(text: str) -> str:
    lines = text.splitlines()
    clipped = "\n".join(lines[:PREVIEW_LINES])
    if len(lines) > PREVIEW_LINES or len(clipped) > PREVIEW_CHARS:
        clipped = clipped[:PREVIEW_CHARS]
        return clipped + "…"
    return clipped


def _json_preview(value: Any) -> Any:
    limited, _ = _limit_depth(value, 2)
    if isinstance(limited, dict):
        keys = list(limited)[:PREVIEW_JSON_KEYS]
        out = {key: _clip_value(limited[key]) for key in keys}
        if len(limited) > PREVIEW_JSON_KEYS:
            out["$more_keys"] = len(limited) - PREVIEW_JSON_KEYS
        return out
    if isinstance(limited, list):
        return [_clip_value(item) for item in limited[:PREVIEW_ITEMS]]
    return _clip_value(limited)


def _clip_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, (dict, list)):
        return _clip(_conv._compact(value))
    return value


# ── derived blocks ────────────────────────────────────────────────────


def _kv(items: list[tuple[str, Any, str]]) -> list[dict[str, Any]]:
    return [{"key": key, "value": value, "source": source} for key, value, source in items]


def _attr_items(attrs: dict[str, Any], keys: str) -> list[dict[str, Any]]:
    out: list[tuple[str, Any, str]] = []
    for spec in keys.split(","):
        if spec.endswith("."):
            for key in sorted(k for k in attrs if isinstance(k, str) and k.startswith(spec)):
                if not key.endswith(_ARTIFACT_META_SUFFIXES):
                    out.append((key, attrs[key], "attribute"))
        elif spec in attrs:
            out.append((spec, attrs[spec], "attribute"))
    return _kv(out)


_Derived = tuple[str, Any, tuple[str, ...], str | None]
_Loader = Callable[[str], _Loaded]


def _not_loaded(loaded: _Loaded) -> str | None:
    return REASON_NOT_LOADED if not loaded.loaded else None


def _kv_block(
    items: list[tuple[str, Any, str]], integrity: tuple[str, ...] = (), reason: str | None = None
) -> _Derived:
    return (AVAILABLE if items else NOT_RECORDED), {"items": _kv(items)}, integrity, reason


def _kv_from(load: _Loaded, items: list[tuple[str, Any, str]]) -> _Derived:
    """A key-value block fed by ``load`` plus attribute fallbacks.

    A source that was recorded but could not be used (missing, out of store,
    truncated, not JSON) is reported as such -- with the attribute items when
    there are any, so the block still shows what it can, and as the failure
    alone when there are none. ``not_recorded`` is reserved for a source that
    was never recorded.
    """
    if not load.loaded:
        return (AVAILABLE if items else AVAILABLE), {"items": _kv(items)} if items else None, (), REASON_NOT_LOADED
    problem = _structured_problem(load)
    if problem is None:
        return _kv_block(items, load.integrity)
    availability, code = problem
    integrity = tuple(dict.fromkeys((*load.integrity, code)))
    if items:
        return AVAILABLE, {"items": _kv(items)}, integrity, code
    return availability, None, integrity, code


def _fields(loaded: _Loaded, keys: Sequence[str]) -> list[tuple[str, Any, str]]:
    return [(key, _field(loaded, key)[1], "artifact") for key in keys if _field(loaded, key)[0]]


def _attr_pairs(attrs: dict[str, Any], keys: Sequence[str]) -> list[tuple[str, Any, str]]:
    return [(key, attrs[key], "attribute") for key in keys if key in attrs]


def _from_payload_and_attrs(load_key: str, fields: Sequence[str], attr_keys: Sequence[str]) -> Callable[..., _Derived]:
    def build(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
        load = loaded(load_key)
        return _kv_from(load, _fields(load, fields) + _attr_pairs(attrs, attr_keys))

    return build


def _d_turn_origin(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    load = loaded("turn.input")
    return _kv_from(load, _fields(load, ("channel", "chat_id")) + _attr_pairs(attrs, ("channel", "surface", "chat_id")))


def _d_turn(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    entry = view.entry
    in_progress = bool(view.turn.in_progress) if view.turn else attrs.get("turn.in_progress") is True
    items = [
        ("turn_number", entry.turn_number, "derived"),
        ("start", entry.operation_start, "derived"),
        ("end", entry.operation_end, "derived"),
        ("in_progress", in_progress, "derived"),
        ("reason", "no_input_recorded", "derived"),
    ]
    return AVAILABLE, {"items": _kv(items)}, (), None


def _d_thinking(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    out = loaded("llm.output")
    if not out.loaded:
        return AVAILABLE, None, (), REASON_NOT_LOADED
    if isinstance(out.payload, dict):
        text = _conv._thinking_text(out.payload)
        return (AVAILABLE if text else NOT_RECORDED), {"text": text}, out.integrity, None
    return out.availability, {"text": out.text or ""}, out.integrity, out.reason


def _d_llm_usage(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    out = loaded("llm.output")
    items: list[tuple[str, Any, str]] = []
    present, value = _field(out, "usage")
    if present and value is not None:
        items.append(("usage", value, "artifact"))
    items += _attr_pairs(attrs, sorted(k for k in attrs if isinstance(k, str) and k.startswith("llm.usage.")))
    return _kv_from(out, items)


def _d_llm_response(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    out = loaded("llm.output")
    present, value = _field(out, "call")
    extra = {key: attrs[key] for key in ("llm.http_status", "llm.served_by", "llm.response_id") if key in attrs}
    problem = _structured_problem(out)
    if problem is not None:
        availability, code = problem
        integrity = tuple(dict.fromkeys((*out.integrity, code)))
        if extra:
            return AVAILABLE, {"value": extra}, integrity, code
        return availability, None, integrity, code
    if not present and not extra:
        return NOT_RECORDED, None, out.integrity, _not_loaded(out)
    return AVAILABLE, {"value": {"call": value, **extra}}, out.integrity, _not_loaded(out)


def _d_frames(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    refs = [
        {"key": key, "ref": str(value), "kind": "frame"}
        for key, value in sorted(attrs.items())
        if isinstance(key, str) and ".frames." in key and isinstance(value, (str, int))
    ]
    return (AVAILABLE if refs else NOT_RECORDED), {"items": refs}, (), None


def _d_attributes(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    domain = view.entry.span_name.split(".", 1)[0]
    items = [
        (key, value, "attribute")
        for key, value in sorted(attrs.items())
        if isinstance(key, str) and key.startswith(domain + ".") and not key.endswith(_ARTIFACT_META_SUFFIXES)
    ]
    return (AVAILABLE if items else EMPTY), {"items": _kv(items)}, (), None


def _d_error(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    entry = view.entry
    status = view.span.get("status") if view.span and isinstance(view.span.get("status"), dict) else {}
    items: list[tuple[str, Any, str]] = [
        ("operation_status", entry.operation_status, "derived"),
        ("evidence", list(entry.status_evidence), "derived"),
    ]
    if status.get("message"):
        items.append(("status_message", status.get("message"), "derived"))
    items += _attr_pairs(attrs, ("tool.error",))
    return AVAILABLE, {"items": _kv(items)}, (), None


def _d_timing(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    entry = view.entry
    items: list[tuple[str, Any, str]] = [
        ("event_time", entry.event_time, "derived"),
        ("operation_start", entry.operation_start, "derived"),
        ("operation_end", entry.operation_end, "derived"),
        ("duration_ms", entry.duration_ms, "derived"),
        ("charged_ms", entry.charged_ms, "derived"),
        ("timing_basis", entry.timing_basis, "derived"),
        ("duration_owner", entry.duration_owner, "derived"),
    ]
    items += _attr_pairs(attrs, ("tool.duration_ms", "subagent.external.elapsed_ms"))
    return AVAILABLE, {"items": _kv(items)}, (), None


def _d_relations(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    entry = view.entry
    items: list[tuple[str, Any, str]] = [
        ("session_key", attrs.get("session.key"), "attribute"),
        ("turn_number", entry.turn_number, "derived"),
        ("turn_span_id", entry.turn_span_id, "derived"),
        ("trace_id", entry.trace_id, "derived"),
        ("span_id", entry.span_id, "derived"),
        ("parent_span_id", entry.parent_span_id, "derived"),
        ("origin", entry.origin, "derived"),
        ("sibling_entries", [sibling.entry_id for sibling in view.siblings], "derived"),
    ]
    items += _attr_pairs(attrs, ("trace.dispatched_by_span_id", "trace.dispatched_in_trace_id"))
    return AVAILABLE, {"items": _kv(items)}, (), None


def _d_integrity(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    codes = list(view.entry.integrity)
    return (AVAILABLE if codes else EMPTY), {"items": codes, "offset": 0}, (), None


def _d_raw(view: EntryView, attrs: dict[str, Any], loaded: _Loader) -> _Derived:
    span = view.span or {}
    artifacts = []
    for key in sorted(k for k in attrs if isinstance(k, str) and k.endswith(".artifact_path")):
        base = key[: -len(".artifact_path")]
        artifacts.append(
            {
                "key": base,
                "path": attrs.get(key),
                "sha1": attrs.get(f"{base}.artifact_sha1"),
                "bytes": attrs.get(f"{base}.artifact_bytes"),
                "error": attrs.get(f"{base}.artifact_error"),
            }
        )
    value = {"status": span.get("status"), "events": span.get("events"), "attributes": attrs, "artifacts": artifacts}
    return AVAILABLE, {"value": value}, (), None


_DERIVED: dict[str, Callable[[EntryView, dict[str, Any], _Loader], _Derived]] = {
    "turn_origin": _d_turn_origin,
    "turn": _d_turn,
    "llm_model": _from_payload_and_attrs(
        "llm.input",
        ("provider", "providerClass", "model", "generation"),
        ("llm.provider", "llm.model", "llm.stream", "llm.invocation_source"),
    ),
    "thinking": _d_thinking,
    "llm_finish": _from_payload_and_attrs(
        "llm.output", ("finish_reason",), ("llm.truncated", "llm.max_tokens", "llm.finish_reason")
    ),
    "llm_usage": _d_llm_usage,
    "llm_response": _d_llm_response,
    "tool": _from_payload_and_attrs("tool.input", ("name",), ("tool.name", "tool.duration_ms", "tool.error")),
    "inject_origin": _from_payload_and_attrs("skill.inject", ("via", "sources"), ()),
    "inject_stats": _from_payload_and_attrs(
        "skill.inject", ("body_len",), ("skill.inject.count", "skill.inject.body_len")
    ),
    "gate_capabilities": _from_payload_and_attrs("skill.gate.input", ("available_tools", "available_subagents"), ()),
    "curate_request": _from_payload_and_attrs("context.curate.input", ("turn_id", "session_key"), ()),
    "curate_stats": _from_payload_and_attrs("context.curate.output", ("produced", "history_len"), ()),
    "store_stats": _from_payload_and_attrs(
        "memory.store", ("session_id",), ("memory.session_id", "memory.message_count")
    ),
    "personalize_request": _from_payload_and_attrs("personalize.input", ("message", "domain"), ()),
    "personalize_qa": _from_payload_and_attrs("personalize.input", ("question", "answer"), ()),
    "frames": _d_frames,
    "attributes": _d_attributes,
    "error": _d_error,
    "timing": _d_timing,
    "relations": _d_relations,
    "integrity": _d_integrity,
    "raw": _d_raw,
}


def _derived(name: str, view: EntryView, reader: _Reader, loaded: _Loader) -> _Derived:
    """(availability, data, integrity, reason) for a derived block."""
    build = _DERIVED.get(name)
    if build is None:
        return UNSUPPORTED, None, (), "unsupported"
    return build(view, tstore._span_attrs(view.span or {}), loaded)


# ── schema association ────────────────────────────────────────────────


def _schema(view: EntryView, reader: _Reader) -> tuple[str, Any, tuple[str, ...], str | None, str | None]:
    """(availability, data, integrity, reason, source_span_id) for a tool's schema."""
    span = view.span or {}
    attrs = tstore._span_attrs(span)
    tool_name = tstore._str_value(attrs.get("tool.name"))
    tool_start = _entries._parse_ts(span.get("startTime"))
    parent = tstore._str_value(span.get("parentSpanId"))
    if tool_name is None or tool_start is None or parent is None or not view.parent_llm_calls:
        return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
    intervals = []
    for llm in view.parent_llm_calls:
        start, end = _entries._parse_ts(llm.get("startTime")), _entries._parse_ts(llm.get("endTime"))
        if start is None or end is None:
            return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
        intervals.append((start, end, llm))
    intervals.sort(key=lambda item: (item[0], item[1]))
    for index in range(1, len(intervals)):
        if intervals[index][0] < intervals[index - 1][1]:
            return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
    for start, end, _ in intervals:
        if start <= tool_start < end:
            return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
    candidates = [
        (start, end, llm)
        for start, end, llm in intervals
        if end <= tool_start and tool_name in (tstore._span_attrs(llm).get("llm.tool_names") or [])
    ]
    if not candidates:
        return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
    chosen_start, chosen_end, chosen = max(candidates, key=lambda item: item[1])
    following = [start for start, _, _ in intervals if start > chosen_end]
    if following and not tool_start < min(following):
        return NOT_RECORDED, None, (), REASON_SCHEMA_UNPROVEN, None
    shell = _load(reader, tstore._span_attrs(chosen), "llm.input")
    if not shell.loaded:
        return AVAILABLE, None, (), REASON_NOT_LOADED, tstore._str_value(chosen.get("spanId"))
    tools = shell.payload.get("tools") if isinstance(shell.payload, dict) else None
    matches = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        if tool.get("name") == tool_name or function.get("name") == tool_name:
            matches.append(tool)
    if len(matches) != 1:
        return NOT_RECORDED, None, shell.integrity, REASON_SCHEMA_UNPROVEN, None
    return AVAILABLE, {"value": matches[0]}, shell.integrity, None, tstore._str_value(chosen.get("spanId"))


# ── block evaluation ──────────────────────────────────────────────────


@dataclass
class _Evaluated:
    spec: BlockSpec
    availability: str
    reason: str | None
    data: Any
    integrity: tuple[str, ...]
    total_items: int | None
    loaded: bool
    source_span_id: str | None = None


def _evaluate(spec: BlockSpec, view: EntryView, reader: _Reader, loaded: Callable[[str], _Loaded]) -> _Evaluated | None:
    """The block's full data (before budgeting), or None when an optional block has no evidence."""
    attrs = tstore._span_attrs(view.span or {})
    if spec.source == "schema":
        availability, data, integrity, reason, source = _schema(view, reader)
        return _Evaluated(spec, availability, reason, data, integrity, None, reason != REASON_NOT_LOADED, source)
    if spec.source.startswith("derived:"):
        availability, data, integrity, reason = _derived(spec.source[len("derived:") :], view, reader, loaded)
        if not spec.required and availability == NOT_RECORDED and reason != REASON_NOT_LOADED:
            return None
        total = len(data["items"]) if isinstance(data, dict) and isinstance(data.get("items"), list) else None
        return _Evaluated(spec, availability, reason, data, integrity, total, reason != REASON_NOT_LOADED)
    if spec.source.startswith("attrs:"):
        keys = spec.source[len("attrs:") :]
        items = _attr_items(attrs, keys)
        if not items:
            if not spec.required:
                return None
            # A prefix selects the span's whole attribute family: none recorded is
            # an empty collection, while a missing named key was never recorded.
            availability = EMPTY if keys.endswith(".") else NOT_RECORDED
            reason = None if availability == EMPTY else NOT_RECORDED
            return _Evaluated(
                spec,
                availability,
                reason,
                {"items": []} if availability == EMPTY else None,
                (),
                0 if availability == EMPTY else None,
                True,
            )
        if spec.renderer == TEXT:
            value = items[0]["value"]
            return _Evaluated(spec, AVAILABLE, None, {"text": _conv._display(value)}, (), None, True)
        if spec.renderer == ITEMS:
            value = items[0]["value"]
            listed = value if isinstance(value, list) else [value]
            return _Evaluated(
                spec, AVAILABLE if listed else EMPTY, None, {"items": listed, "offset": 0}, (), len(listed), True
            )
        return _Evaluated(spec, AVAILABLE, None, {"items": items}, (), len(items), True)
    key = spec.source[len(_A) :]
    load = loaded(key)
    if load.availability == NOT_RECORDED:
        return None if not spec.required else _Evaluated(spec, NOT_RECORDED, NOT_RECORDED, None, (), None, True)
    if not load.loaded:
        return _Evaluated(spec, AVAILABLE, REASON_NOT_LOADED, None, (), None, False)
    if load.availability != AVAILABLE:
        if (
            load.availability == TRUNCATED
            and load.text is not None
            and spec.field is None
            and spec.renderer in (TEXT, JSON)
        ):
            # The readable prefix of a whole-body artifact is still worth
            # showing; cut mid-JSON it can only be shown as text.
            text_spec = replace(spec, renderer=TEXT)
            return _Evaluated(text_spec, TRUNCATED, load.reason, {"text": load.text}, load.integrity, None, True)
        return _Evaluated(spec, load.availability, load.reason, None, load.integrity, None, True)
    if not load.json_ok:
        # A readable body that is not JSON: whole-body consumers show it as
        # text (the generic content block picks the text renderer for it);
        # a consumer that needs a field or a structure cannot use it.
        if spec.field is None and spec.renderer in (TEXT, JSON):
            text_spec = replace(spec, renderer=TEXT)
            return _Evaluated(text_spec, AVAILABLE, None, {"text": load.text or ""}, load.integrity, None, True)
        return _Evaluated(
            spec, UNREADABLE, "artifact_unreadable", None, (*load.integrity, "artifact_unreadable"), None, True
        )
    present, value = _field(load, spec.field)
    if not present:
        if not spec.required:
            return None
        return _Evaluated(spec, NOT_RECORDED, NOT_RECORDED, None, load.integrity, None, True)
    if spec.renderer == TEXT:
        return _Evaluated(spec, AVAILABLE, None, {"text": _conv._display(value)}, load.integrity, None, True)
    if spec.renderer == JSON:
        return _Evaluated(spec, AVAILABLE, None, {"value": value}, load.integrity, None, True)
    if spec.renderer in (MESSAGES, ITEMS):
        listed = value if isinstance(value, list) else ([] if value is None else [value])
        availability = AVAILABLE if listed else EMPTY
        return _Evaluated(spec, availability, None, {"items": listed, "offset": 0}, load.integrity, len(listed), True)
    if spec.renderer == KEY_VALUES:
        if isinstance(value, dict):
            items = _kv([(str(k), v, "artifact") for k, v in value.items()])
        else:
            items = _kv([(spec.field or key, value, "artifact")])
        return _Evaluated(spec, AVAILABLE if items else EMPTY, None, {"items": items}, load.integrity, len(items), True)
    return _Evaluated(spec, UNSUPPORTED, "unsupported", None, load.integrity, None, True)


def _loader(view: EntryView, reader: _Reader) -> Callable[[str], _Loaded]:
    cache: dict[str, _Loaded] = {}
    attrs = tstore._span_attrs(view.span or {})

    def loaded(key: str) -> _Loaded:
        if key not in cache:
            cache[key] = _load(reader, attrs, key)
        return cache[key]

    return loaded


def _resolve_messages(
    items: list[Any], reader: _Reader, *, limit: int | None = None
) -> tuple[list[Any], tuple[str, ...]]:
    """v2 ``$msg`` references resolved in place (first ``limit`` only when given), with integrity codes."""
    integrity: list[str] = []
    out: list[Any] = []
    for index, item in enumerate(items):
        sha1 = artifact_v2.ref_sha1(item)
        if sha1 is None or (limit is not None and index >= limit):
            out.append(item)
            continue
        result = reader.blob(sha1)
        if result is None:
            out.append({"$msg": sha1, "$not_loaded": True})
            continue
        text, reason = result
        if text is None:
            out.append(artifact_v2.placeholder(sha1))
            if "blob_missing" not in integrity:
                integrity.append("blob_missing")
        elif reason is not None:
            out.append({"role": "unknown", "content": f"[message blob over the cap: {sha1}]"})
            if "blob_truncated" not in integrity:
                integrity.append("blob_truncated")
        else:
            parsed, ok = _conv._parse_json(text)
            if ok:
                out.append(parsed)
            else:
                out.append(artifact_v2.placeholder(sha1))
                if "blob_missing" not in integrity:
                    integrity.append("blob_missing")
    return out, tuple(integrity)


def _resolve_text_ref(value: Any, reader: _Reader) -> tuple[str, tuple[str, ...]]:
    sha1 = artifact_v2.ref_sha1(value)
    if sha1 is None:
        return _conv._display(value), ()
    resolved, integrity = _resolve_messages([value], reader)
    message = resolved[0]
    content = message.get("content") if isinstance(message, dict) else message
    return _conv._display(content), integrity


def _preview_for(evaluated: _Evaluated, reader: _Reader) -> tuple[Any, tuple[str, ...]]:
    data = evaluated.data
    if data is None:
        return None, ()
    renderer = evaluated.spec.renderer
    if renderer == TEXT:
        text = data.get("text", "")
        if evaluated.spec.field in ("systemPrompt", "prompt"):
            return _text_preview(text), ()
        return _text_preview(text), ()
    if renderer == JSON:
        return _json_preview(data.get("value")), ()
    if renderer == MESSAGES:
        head, integrity = _resolve_messages(
            list(data.get("items", []))[:PREVIEW_MESSAGES], reader, limit=PREVIEW_MESSAGES
        )
        return [_json_preview(item) for item in head], integrity
    if renderer in (ITEMS, REFERENCES):
        return [_clip_value(item) for item in list(data.get("items", []))[:PREVIEW_ITEMS]], ()
    if renderer == KEY_VALUES:
        return [{"key": item["key"], "value": _clip_value(item["value"])} for item in data.get("items", [])], ()
    return None, ()


# ── public API ────────────────────────────────────────────────────────


def _blob_integrity(view: EntryView, loaded: _Loader, state: Path) -> list[str]:
    """Existence and size of every referenced message blob, by stat alone."""
    codes: list[str] = []
    attrs = tstore._span_attrs(view.span or {})
    for key in sorted(k for k in attrs if isinstance(k, str) and k.endswith(".artifact_path")):
        load = loaded(key[: -len(".artifact_path")])
        if not load.loaded or not isinstance(load.payload, dict):
            continue
        refs = []
        for item in load.payload.get("messages") or []:
            sha1 = artifact_v2.ref_sha1(item)
            if sha1 is not None:
                refs.append(sha1)
        for field in artifact_v2.TEXT_FIELDS:
            sha1 = artifact_v2.ref_sha1(load.payload.get(field))
            if sha1 is not None:
                refs.append(sha1)
        for sha1 in dict.fromkeys(refs):
            path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
            try:
                size = path.stat().st_size
            except OSError:
                if "blob_missing" not in codes:
                    codes.append("blob_missing")
                continue
            if size > ARTIFACT_LIMIT and "blob_truncated" not in codes:
                codes.append("blob_truncated")
    return codes


def _integrity_block(
    view: EntryView, evaluated: Sequence[_Evaluated], loaded: _Loader, state: Path
) -> _Evaluated | None:
    """The integrity block: the entry's codes plus everything this read found."""
    codes = list(view.entry.integrity)
    for block in evaluated:
        codes.extend(code for code in block.integrity if code not in codes)
    codes.extend(code for code in _blob_integrity(view, loaded, state) if code not in codes)
    if not codes:
        return None
    spec = next(s for s in _TAIL if s.id == "integrity")
    return _Evaluated(spec, AVAILABLE, None, {"items": codes, "offset": 0}, (), len(codes), True)


def _blocks_for(view: EntryView, reader: _Reader) -> list[_Evaluated]:
    loaded = _loader(view, reader)
    specs = (*_kind_specs(view.entry), *_TAIL)
    out: list[_Evaluated] = []
    for spec in specs:
        if spec.id == "error" and view.entry.operation_status != _entries.STATUS_ERROR:
            continue
        if spec.id == "integrity":
            continue
        evaluated = _evaluate(spec, view, reader, loaded)
        if evaluated is None:
            continue
        if spec.id in _OPTIONAL_DEPENDENT and not evaluated.loaded:
            evaluated.reason = REASON_NOT_LOADED
        out.append(evaluated)
    integrity = _integrity_block(view, out, loaded, reader.state)
    if integrity is not None:
        raw_index = next((i for i, b in enumerate(out) if b.spec.id == "raw"), len(out))
        out.insert(raw_index, integrity)
    return out


def _text_prepare(evaluated: _Evaluated, reader: _Reader) -> None:
    """Resolve ``systemPrompt``/``prompt`` references for text blocks (shell aliases of messages)."""
    if (
        evaluated.spec.renderer == TEXT
        and evaluated.data is not None
        and evaluated.spec.field in ("systemPrompt", "prompt")
    ):
        raw = evaluated.data.get("text")
        if isinstance(raw, str) and raw.startswith("{"):
            parsed, ok = _conv._parse_json(raw)
            if ok and artifact_v2.ref_sha1(parsed) is not None:
                text, integrity = _resolve_text_ref(parsed, reader)
                evaluated.data = {"text": text}
                evaluated.integrity = tuple(dict.fromkeys((*evaluated.integrity, *integrity)))


def describe(
    index: SessionIndex, entry_id: str, *, state: Path, expected_revision: int | None = None
) -> Descriptor | None:
    """The overview descriptor for ``entry_id``, or None when the entry is gone."""
    view = index.capture(entry_id)
    if view is None:
        return None
    reader = _Reader(state, max_artifacts=DESCRIBE_ARTIFACTS, max_blobs=DESCRIBE_BLOBS, max_bytes=DESCRIBE_BYTES_LIMIT)
    evaluated = _blocks_for(view, reader)
    descriptors: list[BlockDescriptor] = []
    extra_integrity: list[str] = []
    for block in evaluated:
        _text_prepare(block, reader)
        preview, integrity = _preview_for(block, reader) if block.loaded else (None, ())
        extra_integrity.extend(code for code in (*block.integrity, *integrity) if code not in extra_integrity)
        descriptors.append(
            BlockDescriptor(
                id=block.spec.id,
                renderer=block.spec.renderer,
                availability=block.availability,
                preview=preview,
                total_items=block.total_items,
                related_operation=block.spec.related,
                reason=block.reason,
            )
        )
    entry = view.entry
    notes: list[str] = []
    if NOTE_OUTER_ONLY in entry.status_evidence:
        notes.append(NOTE_OUTER_ONLY)
    if tstore._span_attrs(view.span or {}).get("turn.in_progress") is True:
        notes.append(NOTE_IN_PROGRESS)
    integrity = tuple(dict.fromkeys((*entry.integrity, *extra_integrity)))
    if integrity:
        notes.append(NOTE_INCOMPLETE)
    descriptor = Descriptor(
        session_key=index.session_key,
        epoch=view.epoch,
        entry_id=entry.entry_id,
        entry_revision=entry.revision,
        kind=entry.kind,
        span_name=entry.span_name,
        slot=entry.slot,
        operation_status=entry.operation_status,
        status_evidence=entry.status_evidence,
        failure_entry=entry.failure_entry,
        integrity=integrity,
        notes=tuple(notes),
        blocks=tuple(descriptors),
        revision_changed=expected_revision is not None and expected_revision != entry.revision,
    )
    return _fit_descriptor(descriptor)


def _fit_descriptor(descriptor: Descriptor) -> Descriptor:
    budget = RESPONSE_LIMIT - RESPONSE_RESERVE
    blocks = list(descriptor.blocks)
    truncated = False
    while blocks and _size([_block_wire(b) for b in blocks]) > budget:
        largest = max(range(len(blocks)), key=lambda i: _size(blocks[i].preview))
        if blocks[largest].preview is None:
            break
        blocks[largest] = replace(blocks[largest], preview=None, reason=REASON_PREVIEW_DROPPED)
        truncated = True
    return replace(descriptor, blocks=tuple(blocks), truncated=truncated)


def _block_wire(block: BlockDescriptor) -> dict[str, Any]:
    return {
        "id": block.id,
        "renderer": block.renderer,
        "availability": block.availability,
        "preview": block.preview,
        "total_items": block.total_items,
        "related_operation": block.related_operation,
        "reason": block.reason,
    }


def _encode_cursor(entry_id: str, revision: int, epoch: str, offset: int) -> str:
    raw = json.dumps({"e": entry_id, "r": revision, "p": epoch, "o": offset}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, entry_id: str, revision: int, epoch: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, TypeError):
        raise CursorExpiredError("malformed block cursor") from None
    if (
        not isinstance(data, dict)
        or data.get("e") != entry_id
        or data.get("r") != revision
        or data.get("p") != epoch
        or not isinstance(data.get("o"), int)
        or data["o"] < 0
    ):
        raise CursorExpiredError("block cursor does not match this entry revision")
    return data["o"]


def read_block(
    index: SessionIndex,
    entry_id: str,
    block_id: str,
    *,
    state: Path,
    entry_revision: int,
    epoch: str,
    cursor: str | None = None,
) -> BlockBody:
    """One block's full data for ``entry_id`` at ``entry_revision``/``epoch``, paged where the renderer pages."""
    view = index.capture(entry_id)
    if view is None:
        raise EntryGoneError(entry_id)
    if view.epoch != epoch or view.entry.revision != entry_revision:
        raise RevisionChangedError(view.entry.revision, view.epoch)
    offset = _decode_cursor(cursor, entry_id, entry_revision, epoch) if cursor is not None else 0
    reader = _Reader(state, max_artifacts=None, max_blobs=None, max_bytes=None)
    loaded = _loader(view, reader)
    specs = (*_kind_specs(view.entry), *_TAIL)
    spec = next((s for s in specs if s.id == block_id), None)
    if spec is None:
        raise UnknownBlockError(block_id)
    if spec.id == "error" and view.entry.operation_status != _entries.STATUS_ERROR:
        raise UnknownBlockError(block_id)
    if spec.id == "integrity":
        found = next((b for b in _blocks_for(view, reader) if b.spec.id == "integrity"), None)
        if found is None:
            raise UnknownBlockError(block_id)
        evaluated = found
    else:
        evaluated = _evaluate(spec, view, reader, loaded)
    if evaluated is None:
        evaluated = _Evaluated(spec, NOT_RECORDED, NOT_RECORDED, None, (), None, True)
    spec = evaluated.spec
    _text_prepare(evaluated, reader)
    budget = RESPONSE_LIMIT - RESPONSE_RESERVE
    data = evaluated.data
    next_cursor = None
    truncated = False
    integrity = list(evaluated.integrity)
    availability = evaluated.availability
    if data is not None:
        if spec.renderer == TEXT:
            text, truncated = _fit_text(data.get("text", ""), budget)
            data = {"text": text}
            if truncated:
                availability = TRUNCATED
        elif spec.renderer == JSON:
            value, truncated = _fit_json(data.get("value"), budget)
            data = {"value": value}
            if truncated:
                availability = TRUNCATED
        elif spec.renderer in (MESSAGES, ITEMS, REFERENCES):
            items = list(data.get("items", []))
            page = MESSAGES_PAGE if spec.renderer == MESSAGES else ITEMS_PAGE
            window = items[offset : offset + page]
            if spec.renderer == MESSAGES:
                window, blob_integrity = _resolve_messages(window, reader)
                integrity.extend(code for code in blob_integrity if code not in integrity)
            fitted, end, truncated = _fit_items(window, 0, page, budget)
            end_offset = offset + end
            data = {"items": fitted, "offset": offset}
            if end_offset < len(items):
                next_cursor = _encode_cursor(entry_id, entry_revision, epoch, end_offset)
        elif spec.renderer == KEY_VALUES:
            fitted, truncated = _fit_key_values(list(data.get("items", [])), budget)
            data = {"items": fitted}
    if evaluated.source_span_id is not None and isinstance(data, dict):
        data = {**data, "source_span_id": evaluated.source_span_id}
    truncated = truncated or availability == TRUNCATED
    return BlockBody(
        entry_id=entry_id,
        entry_revision=entry_revision,
        epoch=epoch,
        block_id=block_id,
        renderer=spec.renderer,
        availability=availability,
        reason=evaluated.reason,
        data=data,
        next_cursor=next_cursor,
        total_items=evaluated.total_items,
        integrity=tuple(integrity),
        truncated=truncated,
    )


__all__ = [
    "AVAILABILITIES",
    "RENDERERS",
    "BlockBody",
    "BlockDescriptor",
    "BlockSpec",
    "Descriptor",
    "EntryGoneError",
    "RevisionChangedError",
    "UnknownBlockError",
    "block_specs",
    "describe",
    "read_block",
]
