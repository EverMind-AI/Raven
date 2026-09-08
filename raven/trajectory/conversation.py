"""Rebuild one attempt's conversation as labeled records from its trace spans.

:func:`attempt_conversation` turns an attempt's span snapshot into a flat,
causally ordered stream of :class:`ConversationRecord` — the data layer behind
the trajectory browser's full-conversation preview (display-agnostic, so other
front-ends can reuse it).

Contracts:

- Event-time semantics: input records sort at their span's ``startTime``,
  output/completion records at ``endTime`` (a missing endTime falls back to
  the span's own startTime, so an output can never precede its input). Ties
  break by phase (input before output), then by nesting depth computed from
  ``parentSpanId`` — ancestor inputs before descendant inputs, descendant
  outputs before ancestor outputs; start times and span-id ordering never
  stand in for the real nesting relation. The key is a plain tuple, so the
  order is total, transitive, and reproducible.
- Full content comes from artifact files; span preview attributes are only a
  fallback. Every degradation (missing artifact, size cap, non-JSON payload,
  unreadable span) is spelled out in ``degraded``: ERROR spans and
  expected-but-unreadable payloads always yield a record — silent loss would
  make "the call never happened" and "the content was lost" look the same.
- Artifact paths are untrusted log data: only files under the state dir's
  ``logs/`` tree are read, capped at 512 KiB per file.
- LLM inputs repeat the whole message history on every call. A call's record
  omits exactly the item-by-item verified common prefix against the previous
  call in the same ``(traceId, parentSpanId)`` chain (main-loop chains
  continue across turns) and shows everything else in full; any omission or
  detected rewrite is announced in the record itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore

_log = logging.getLogger("raven.trajectory.conversation")

_ARTIFACT_LIMIT = 512 * 1024
_DEPTH_LIMIT = 64

_PHASE_INPUT = 0
_PHASE_OUTPUT = 1

_ARTIFACT_META_SUFFIXES = (".artifact_path", ".artifact_sha1", ".artifact_bytes", ".artifact_error")

# Message keys rendered structurally; everything else falls back to a
# "key: value" line so prefix-compared content is never invisible.
_MSG_KNOWN_KEYS = {"role", "content", "tool_calls", "tool_call_id", "reasoning_content", "thinking"}

_KIND_BY_DOMAIN = {
    "session": "user",
    "llm": "llm",
    "tool": "tool",
    "skill": "skill",
    "subagent": "subagent",
    "memory": "memory",
    "personalize": "memory",
    "context": "memory",
}


@dataclass(frozen=True)
class ConversationRecord:
    """One labeled conversation event rebuilt from an attempt's spans."""

    label: str
    kind: str
    text: str
    trace_id: str
    span_id: str
    turn_span_id: str | None
    event_time: str
    seq: int
    degraded: str | None = None
    error: str | None = None
    meta: str | None = None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _compact(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _pretty(value)


def _label(name: str) -> str:
    parts = [p for p in name.split(".") if p]
    if not parts:
        return "?"
    head = parts[0].capitalize()
    return " ".join([head, *parts[1:]])


def _kind(name: str) -> str:
    return _KIND_BY_DOMAIN.get(name.split(".", 1)[0], "other")


def _read_artifact(state: Path, path: str) -> tuple[str | None, str | None]:
    """(text, degraded reason); (None, reason) means unreadable.

    Reads at most ``_ARTIFACT_LIMIT + 1`` bytes — the cap bounds actual I/O and
    memory, not just the returned text, because the pointer may name any file
    under ``logs/`` (a rotated multi-GB log included). Non-regular files (a
    FIFO would block the read) are rejected outright.
    """
    try:
        logs_root = (state / "logs").resolve()
        target = Path(path).resolve()
        if not target.is_relative_to(logs_root):
            return None, "artifact path outside the trace store"
        if target.exists() and not target.is_file():
            return None, "artifact is not a regular file"
        with target.open("rb") as handle:
            data = handle.read(_ARTIFACT_LIMIT + 1)
    except OSError:
        return None, "artifact missing"
    if len(data) > _ARTIFACT_LIMIT:
        return data[:_ARTIFACT_LIMIT].decode("utf-8", errors="replace"), "content over 512 KiB — truncated"
    return data.decode("utf-8", errors="replace"), None


def _slot(
    state: Path, attrs: dict[str, Any], key: str, preview_key: str | None = None
) -> tuple[str, str | None] | None:
    """Load one input/output slot: (text, degraded), degraded None = complete.

    ``None`` means the span carries no evidence the slot ever existed (no
    artifact pointer and no preview attribute) — only then may a record be
    omitted. A pointer or preview that cannot be read yields an empty-text
    placeholder with the reason, never silence.
    """
    pointer = _str(attrs.get(f"{key}.artifact_path"))
    read_reason = None
    if pointer is not None:
        text, read_reason = _read_artifact(state, pointer)
        if text is not None:
            return text, read_reason
    preview = _str(attrs.get(preview_key)) if preview_key else None
    if preview is not None:
        return preview, f"{read_reason or 'artifact missing'} — truncated preview"
    # Evidence is judged on key presence, not value shape: a type-corrupt
    # pointer or preview still proves the slot existed and must stay visible.
    if f"{key}.artifact_path" in attrs or (preview_key is not None and preview_key in attrs):
        return "", f"content unavailable — {read_reason or 'artifact missing'}"
    return None


def _parse_json(text: str) -> tuple[Any, bool]:
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


def _slot_payload(
    state: Path, attrs: dict[str, Any], key: str, preview_key: str | None = None
) -> tuple[Any, str, str | None] | None:
    """(parsed payload or None, display text, degraded) for one slot.

    A complete read that fails JSON parsing keeps the raw text but is marked
    degraded — degraded content is excluded from dedup references and from
    the LLM prefix-comparison state.
    """
    slot = _slot(state, attrs, key, preview_key)
    if slot is None:
        return None
    text, degraded = slot
    if degraded is None:
        obj, ok = _parse_json(text)
        if ok:
            return obj, text, None
        return None, text, "artifact is not valid JSON — shown raw"
    return None, text, degraded


def _payload_field(obj: Any, text: str, field: str) -> str:
    if isinstance(obj, dict) and field in obj:
        return _display(obj.get(field))
    return text


def _tool_call_line(tc: Any) -> str:
    if not isinstance(tc, dict):
        return f"→ tool call {_compact(tc)}"
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name = _str(tc.get("name")) or _str(fn.get("name")) or "?"
    tc_id = _str(tc.get("id"))
    args = tc.get("arguments", fn.get("arguments"))
    args_text = args if isinstance(args, str) else ("" if args is None else _compact(args))
    suffix = f"#{tc_id}" if tc_id else ""
    return f"→ tool call {name}{suffix}({args_text})"


def _render_message(message: Any) -> str:
    if not isinstance(message, dict):
        return _compact(message)
    role = _str(message.get("role")) or "?"
    tc_id = _str(message.get("tool_call_id"))
    lines = [f"[{role} #{tc_id}]" if tc_id else f"[{role}]"]
    content = message.get("content")
    if isinstance(content, str):
        if content:
            lines.append(content)
    elif content is not None:
        lines.append(_compact(content))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        lines.extend(_tool_call_line(tc) for tc in tool_calls)
    reasoning = message.get("reasoning_content") or message.get("thinking")
    if reasoning:
        lines.append("[reasoning]")
        lines.append(reasoning if isinstance(reasoning, str) else _compact(reasoning))
    for key in sorted(set(message) - _MSG_KNOWN_KEYS):
        lines.append(f"{key}: {_compact(message[key])}")
    return "\n".join(lines)


def _render_messages(messages: Sequence[Any]) -> str:
    return "\n\n".join(_render_message(m) for m in messages)


def _llm_meta(attrs: dict[str, Any]) -> str | None:
    parts = []
    model = _str(attrs.get("llm.model"))
    if model:
        parts.append(model)
    tokens = []
    for label, key in (("in", "llm.usage.input_tokens"), ("out", "llm.usage.output_tokens")):
        value = attrs.get(key)
        if isinstance(value, int):
            tokens.append(f"{label} {value}")
    if tokens:
        parts.append(" / ".join(tokens) + " tok")
    return " · ".join(parts) or None


class _ChainState:
    """Per-attempt LLM-input prefix state, keyed by ``(traceId, parentSpanId)``.

    ``chains`` values are the previous call's normalized+raw messages, or None
    when the previous call's input could not be fully read (the next call must
    then show everything and say why). ``prev_main`` carries the last complete
    main-loop messages so a new turn's chain can continue the omission.
    """

    def __init__(self) -> None:
        self.chains: dict[tuple[str, str | None], tuple[list[str], list[Any]] | None] = {}
        self.prev_main: tuple[list[str], list[Any]] | None = None

    @staticmethod
    def _normalize(message: Any) -> str:
        try:
            return json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(message)

    def render_input(self, chain: tuple[str, str | None], is_main: bool, messages: list[Any]) -> tuple[str, str | None]:
        """(text, degraded) for this call; records the call as the chain's last."""
        norm = [self._normalize(m) for m in messages]
        seen_chain = chain in self.chains
        base = self.chains.get(chain)
        in_chain = base is not None
        first_note: str | None = None
        if seen_chain and base is None:
            first_note = "previous call unreadable — full messages shown"
        elif not seen_chain and is_main:
            base = self.prev_main
        self.chains[chain] = (norm, messages)
        if is_main:
            self.prev_main = (norm, messages)
        if first_note is not None or base is None or not base[0]:
            return _render_messages(messages), first_note
        base_norm, _ = base
        if len(norm) >= len(base_norm) and norm[: len(base_norm)] == base_norm:
            unchanged = f"(… {len(base_norm)} earlier messages unchanged)"
            fresh = messages[len(base_norm) :]
            if not fresh:
                return unchanged, None
            return unchanged + "\n\n" + _render_messages(fresh), None
        # A rewrite is only announced for the chain's own history; a fresh
        # chain that fails to continue the previous main loop simply shows
        # everything — full content is its normal state, not a degradation.
        return _render_messages(messages), "history rewritten — full messages shown" if in_chain else None

    def mark_unreadable(self, chain: tuple[str, str | None]) -> None:
        self.chains[chain] = None


@dataclass
class _SpanInfo:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start: str
    end: str
    turn_span_id: str | None
    depth: int
    attrs: dict[str, Any]
    error: str | None
    malformed: bool


def _emit(info: _SpanInfo, records: list[dict[str, Any]], label: str, phase: int, text: str, **kw: Any) -> None:
    records.append(
        {
            "label": label,
            "kind": kw.get("kind") or _kind(info.name),
            "text": text,
            "phase": phase,
            "info": info,
            "degraded": kw.get("degraded"),
            "error": kw.get("error"),
            "meta": kw.get("meta"),
            "complete": kw.get("degraded") is None,
        }
    )


def _emit_slot(
    info: _SpanInfo,
    records: list[dict[str, Any]],
    state: Path,
    label: str,
    phase: int,
    key: str,
    preview_key: str | None = None,
    field: str | None = None,
    meta: str | None = None,
) -> None:
    payload = _slot_payload(state, info.attrs, key, preview_key)
    if payload is None:
        return
    obj, text, degraded = payload
    if field is not None:
        text = _payload_field(obj, text, field)
    _emit(info, records, label, phase, text, degraded=degraded, meta=meta)


def _emit_turn(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    before = len(records)
    _emit_slot(info, records, state, "User input", _PHASE_INPUT, "turn.input", "turn.input_preview", field="content")
    has_input = len(records) > before
    _emit_slot(
        info, records, state, "Agent reply", _PHASE_OUTPUT, "turn.output", "turn.output_preview", field="content"
    )
    # A turn's start must stay addressable whatever the root carries: without
    # an input-phase record (empty body, or an output-only root) the renderer
    # could not place the turn's own start time, so a marker stands in.
    if not has_input:
        _emit(info, records, "Turn", _PHASE_INPUT, "")


def _emit_llm(info: _SpanInfo, records: list[dict[str, Any]], state: Path, chains: _ChainState) -> None:
    meta = _llm_meta(info.attrs)
    chain = (info.trace_id, info.parent_id)
    payload = _slot_payload(state, info.attrs, "llm.input")
    if payload is not None:
        obj, text, degraded = payload
        messages = obj.get("messages") if isinstance(obj, dict) else None
        if degraded is None and isinstance(messages, list):
            is_main = info.turn_span_id is not None and info.parent_id == info.turn_span_id
            text, degraded = chains.render_input(chain, is_main, messages)
        else:
            chains.mark_unreadable(chain)
        _emit(info, records, "LLM input", _PHASE_INPUT, text, degraded=degraded, meta=meta)
    payload = _slot_payload(state, info.attrs, "llm.output", "llm.output_preview")
    if payload is None:
        return
    obj, text, degraded = payload
    if isinstance(obj, dict):
        lines = []
        content = _display(obj.get("content"))
        if content:
            lines.append(content)
        tool_calls = obj.get("tool_calls")
        if isinstance(tool_calls, list):
            lines.extend(_tool_call_line(tc) for tc in tool_calls)
        text = "\n".join(lines)
        thinking = _thinking_text(obj)
        if thinking:
            _emit(info, records, "LLM thinking", _PHASE_OUTPUT, thinking, meta=meta)
    _emit(info, records, "LLM output", _PHASE_OUTPUT, text, degraded=degraded, meta=meta)


def _thinking_text(obj: dict[str, Any]) -> str:
    """Reasoning evidence from the output payload, whichever fields carry it.

    ``reasoning_content`` and ``thinking_blocks`` are persisted independently
    and either may be the only copy; identical block text is not repeated.
    """
    parts: list[str] = []
    reasoning = obj.get("reasoning_content")
    if reasoning:
        parts.append(_display(reasoning))
    blocks = obj.get("thinking_blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and _str(block.get("thinking")):
                text = block["thinking"]
            else:
                text = _compact(block)
            if text not in parts:
                parts.append(text)
    return "\n\n".join(parts)


def _emit_tool(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    meta = _str(info.attrs.get("tool.name"))
    if meta is None:
        payload = _slot_payload(state, info.attrs, "tool.input")
        if payload is not None and isinstance(payload[0], dict):
            meta = _str(payload[0].get("name"))
    _emit_slot(
        info, records, state, "Tool input", _PHASE_INPUT, "tool.input", "tool.args_preview", field="params", meta=meta
    )
    _emit_slot(
        info,
        records,
        state,
        "Tool output",
        _PHASE_OUTPUT,
        "tool.output",
        "tool.result_preview",
        field="result",
        meta=meta,
    )


def _emit_skill_read(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    meta = _str(info.attrs.get("skill.name")) or _str(info.attrs.get("skill.id"))
    _emit_slot(
        info,
        records,
        state,
        "Skill read",
        _PHASE_OUTPUT,
        "tool.output",
        "skill.result_preview",
        field="result",
        meta=meta,
    )


def _emit_skill_inject(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    payload = _slot_payload(state, info.attrs, "skill.inject")
    if payload is not None:
        obj, text, degraded = payload
        if isinstance(obj, dict):
            skills = obj.get("skills") if isinstance(obj.get("skills"), list) else []
            names = [_str(s.get("name")) or "?" for s in skills if isinstance(s, dict)]
            lines = [f"skills: {', '.join(names)}" if names else "skills: (none)"]
            via, body_len = obj.get("via"), obj.get("body_len")
            detail = " · ".join(
                p for p in (f"via {via}" if via else "", f"body {body_len} chars" if body_len else "") if p
            )
            if detail:
                lines.append(detail)
            text = "\n".join(lines)
        _emit(info, records, "Skill inject", _PHASE_OUTPUT, text, degraded=degraded)
        return
    names = info.attrs.get("skill.inject.names")
    if isinstance(names, list) and names:
        _emit(info, records, "Skill inject", _PHASE_OUTPUT, f"skills: {', '.join(str(n) for n in names)}")


def _emit_subagent(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    task = _str(info.attrs.get("subagent.task"))
    text = task if task is not None else _domain_summary(info.attrs, info.name)
    if text is None:
        return
    _emit(info, records, "Subagent", _PHASE_INPUT, text, meta=_str(info.attrs.get("subagent.label")))


def _domain_summary(attrs: dict[str, Any], name: str) -> str | None:
    domain = name.split(".", 1)[0]
    keep = {
        k: v
        for k, v in attrs.items()
        if isinstance(k, str) and k.startswith(domain + ".") and not k.endswith(_ARTIFACT_META_SUFFIXES)
    }
    return _pretty(keep) if keep else None


def _emit_io_pair(info: _SpanInfo, records: list[dict[str, Any]], state: Path, base_key: str) -> bool:
    label_base = _label(info.name)
    before = len(records)
    _emit_slot(info, records, state, f"{label_base} input", _PHASE_INPUT, f"{base_key}.input")
    _emit_slot(info, records, state, f"{label_base} output", _PHASE_OUTPUT, f"{base_key}.output")
    return len(records) > before


def _emit_generic(info: _SpanInfo, records: list[dict[str, Any]], state: Path) -> None:
    keys = sorted(
        k[: -len(".artifact_path")] for k in info.attrs if isinstance(k, str) and k.endswith(".artifact_path")
    )
    for key in keys:
        if key.endswith(".input"):
            label, phase = _label(key[: -len(".input")]) + " input", _PHASE_INPUT
        elif key.endswith(".output"):
            label, phase = _label(key[: -len(".output")]) + " output", _PHASE_OUTPUT
        else:
            label, phase = _label(key), _PHASE_OUTPUT
        _emit_slot(info, records, state, label, phase, key)
    if keys:
        return
    summary = _domain_summary(info.attrs, info.name)
    if summary is not None:
        _emit(info, records, _label(info.name), _PHASE_OUTPUT, summary)


def _emit_span(info: _SpanInfo, records: list[dict[str, Any]], state: Path, chains: _ChainState) -> None:
    name = info.name
    if name == "session.turn":
        _emit_turn(info, records, state)
    elif name == "llm.call":
        _emit_llm(info, records, state, chains)
    elif name == "tool.call":
        _emit_tool(info, records, state)
    elif name == "skill.read":
        _emit_skill_read(info, records, state)
    elif name == "skill.inject":
        _emit_skill_inject(info, records, state)
    elif name == "subagent.run":
        _emit_subagent(info, records, state)
    elif name in ("skill.rewrite", "skill.gate", "context.curate"):
        if not _emit_io_pair(info, records, state, name):
            _emit_generic(info, records, state)
    elif name.startswith("personalize."):
        if not _emit_io_pair(info, records, state, "personalize"):
            _emit_generic(info, records, state)
    elif name in ("memory.recall", "memory.store"):
        payload = _slot_payload(state, info.attrs, name)
        if payload is not None:
            _obj, text, degraded = payload
            _emit(info, records, _label(name), _PHASE_OUTPUT, text, degraded=degraded)
        else:
            summary = _domain_summary(info.attrs, name)
            if summary is not None:
                _emit(info, records, _label(name), _PHASE_OUTPUT, summary)
    else:
        _emit_generic(info, records, state)


def _dedup_span_records(records: list[dict[str, Any]]) -> None:
    """Collapse identical fully-read payloads within one span.

    The reference target must itself be complete (never degraded/truncated
    content) and equality is judged on the actual text — artifact_sha1
    attributes are untrusted metadata and play no part.
    """
    seen: dict[str, str] = {}
    for record in records:
        text = record["text"]
        if not record["complete"] or not text:
            continue
        if text in seen:
            record["text"] = f"same content as {seen[text]} above"
        else:
            seen[text] = record["label"]


def _span_error(span: dict[str, Any]) -> str | None:
    status = span.get("status")
    if isinstance(status, dict) and status.get("code") == "ERROR":
        return _str(status.get("message")) or "ERROR"
    return None


def _collect_spans(traces: Sequence[str], state: Path) -> list[dict[str, Any]]:
    wanted = {t for t in traces if isinstance(t, str) and t}
    logical: dict[Any, dict[str, Any]] = {}
    bogus = 0
    for span in tstore.iter_spans(state):
        trace_id = _str(span.get("traceId"))
        if trace_id not in wanted:
            continue
        span_id = _str(span.get("spanId"))
        if span_id:
            logical[(trace_id, span_id)] = span
        else:
            logical[(trace_id, bogus)] = span
            bogus += 1
    return list(logical.values())


def _build_infos(spans: list[dict[str, Any]]) -> list[_SpanInfo]:
    parent_of: dict[tuple[str, str], str | None] = {}
    name_of: dict[tuple[str, str], str] = {}
    for span in spans:
        trace_id, span_id = _str(span.get("traceId")) or "", _str(span.get("spanId")) or ""
        if span_id:
            parent_of[(trace_id, span_id)] = _str(span.get("parentSpanId"))
            name_of[(trace_id, span_id)] = _str(span.get("name")) or ""

    def _walk(trace_id: str, span_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = span_id
        while current and current not in seen and len(chain) < _DEPTH_LIMIT:
            seen.add(current)
            chain.append(current)
            current = parent_of.get((trace_id, current))
        return chain

    infos = []
    for span in spans:
        trace_id, span_id = _str(span.get("traceId")) or "", _str(span.get("spanId")) or ""
        attrs = span.get("attributes")
        status = span.get("status")
        chain = _walk(trace_id, span_id) if span_id else [""]
        turn_span_id = next((s for s in chain if name_of.get((trace_id, s)) == "session.turn"), None)
        infos.append(
            _SpanInfo(
                trace_id=trace_id,
                span_id=span_id,
                parent_id=parent_of.get((trace_id, span_id)),
                name=_str(span.get("name")) or "",
                start=_str(span.get("startTime")) or "",
                end=_str(span.get("endTime")) or "",
                turn_span_id=turn_span_id,
                depth=len(chain) - 1,
                attrs=attrs if isinstance(attrs, dict) else {},
                error=_span_error(span),
                malformed=(attrs is not None and not isinstance(attrs, dict))
                or (status is not None and not isinstance(status, dict)),
            )
        )
    infos.sort(key=lambda i: (i.start, i.span_id, i.trace_id))
    return infos


def attempt_conversation(traces: Sequence[str], state_dir: Path | None = None) -> list[ConversationRecord]:
    """Rebuild the attempt covering ``traces`` as an ordered record stream."""
    state = state_dir if state_dir is not None else tracing_config.state_dir()
    infos = _build_infos(_collect_spans(traces, state))
    chains = _ChainState()
    all_records: list[dict[str, Any]] = []
    for info in infos:
        records: list[dict[str, Any]] = []
        try:
            _emit_span(info, records, state, chains)
            _dedup_span_records(records)
        except Exception as exc:  # noqa: BLE001 — one bad span must stay visible, not sink the preview
            _log.debug("conversation: span %s/%s unreadable", info.trace_id, info.span_id, exc_info=True)
            records = []
            _emit(
                info, records, _label(info.name), _PHASE_OUTPUT, "", degraded=f"span unreadable — {type(exc).__name__}"
            )
        if info.malformed:
            # Always a separate evidence record: readable payloads must not
            # make a span with a corrupt status/attributes read as fully OK.
            _emit(
                info,
                records,
                _label(info.name),
                _PHASE_OUTPUT,
                "",
                degraded="span record malformed — original status/attributes unreadable",
            )
        if info.error and records:
            records[-1]["error"] = info.error
        elif info.error:
            _emit(info, records, _label(info.name), _PHASE_OUTPUT, "", error=info.error)
        all_records.extend(records)

    def _key(record: dict[str, Any]) -> tuple:
        info: _SpanInfo = record["info"]
        if record["phase"] == _PHASE_INPUT:
            event_time, signed_depth = info.start, info.depth
        else:
            event_time, signed_depth = info.end or info.start, -info.depth
        return (event_time, record["phase"], signed_depth, info.start, info.trace_id, info.span_id, record["seq"])

    for seq, record in enumerate(all_records):
        record["seq"] = seq
    # Per-span emission order (input, thinking, output, ...) seeds seq; the
    # tuple's earlier components own the causal order across spans.
    all_records.sort(key=_key)

    out = []
    for record in all_records:
        info = record["info"]
        event_time = info.start if record["phase"] == _PHASE_INPUT else (info.end or info.start)
        out.append(
            ConversationRecord(
                label=record["label"],
                kind=record["kind"],
                text=record["text"],
                trace_id=info.trace_id,
                span_id=info.span_id,
                turn_span_id=info.turn_span_id,
                event_time=event_time,
                seq=record["seq"],
                degraded=record["degraded"],
                error=record["error"],
                meta=record["meta"],
            )
        )
    return out


__all__ = ["ConversationRecord", "attempt_conversation"]
