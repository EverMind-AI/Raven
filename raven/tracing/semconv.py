"""Semantic conventions for raven's tracing standard.

Owns both the standard span attribute/artifact *builders* (low-level helpers)
and the per-span-kind *extractors* used by ``@trace.instrument(extract=...)``.
Extraction is duck-typed / by-name binding, so it stays framework-agnostic and
survives raven refactors as long as the documented shapes hold.
See ``docs/TRACING_STANDARD_API.md``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import config
from . import usage as usage_mod
from .store import preview_text

_SKILL_TOOLS = {"use_skill", "read_skill"}


_FILE_READ_TOOLS = {"read_file"}


def _preview(value: Any, n: int | None = None) -> str:
    return preview_text(value, n if n is not None else config.preview_len())


def _split_session_key(session_key: str | None) -> tuple[str | None, str | None]:
    if session_key and ":" in session_key:
        channel, _, chat_id = session_key.partition(":")
        return channel or None, chat_id or None
    return None, None


def _turn_capabilities(loop: Any) -> dict[str, Any]:
    """Snapshot what this turn's agent has loaded: tools, plugin backend +
    plugin-contributed tools, and the available skills. Read off the AgentLoop
    (``self``) at the turn probe. Each piece is best-effort — a missing attr
    just omits that field, never breaks the turn span. This makes every trace
    self-describing (e.g. a TUI trace plainly shows backend=null / no plugins)."""
    caps: dict[str, Any] = {}
    try:
        names = loop.tools.tool_names
        caps["turn.tools"] = list(names)
        caps["turn.tool_count"] = len(names)
    except Exception:  # noqa: BLE001
        pass
    try:
        backend = getattr(loop, "backend", None)
        caps["turn.plugin.backend"] = type(backend).__name__ if backend is not None else None
    except Exception:  # noqa: BLE001
        pass
    try:
        ptools = getattr(loop, "plugin_tools", None) or []
        caps["turn.plugin.tools"] = [getattr(t, "name", None) for t in ptools]
    except Exception:  # noqa: BLE001
        pass
    try:
        cat = getattr(getattr(loop, "context", None), "skills", None)
        reg = getattr(cat, "registry", None) or getattr(cat, "_registry", None)
        metas = list(reg.list_all()) if reg is not None else []
        caps["turn.skills"] = [getattr(m, "name", None) for m in metas][:50]
        caps["turn.skill_count"] = len(metas)
    except Exception:  # noqa: BLE001
        pass
    return caps


def _provider_label(model: str | None, provider_class: str | None) -> str | None:
    """Logical routing backend for a call.

    raven reaches every gateway through a single ``LiteLLMProvider`` class, so
    the class name hides which backend actually served the call. LiteLLM encodes
    that as the model prefix (``openrouter/anthropic/claude-...``), so the first
    path segment is the backend (``openrouter``). Fall back to the provider class
    name when the model carries no prefix (e.g. a native provider).
    """
    if model and "/" in model:
        return model.split("/", 1)[0]
    return provider_class


def _llm_attrs(resp: Any, provider: str, model: str | None, provider_class: str | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {"llm.provider": provider, "llm.model": model}
    if provider_class and provider_class != provider:
        attrs["llm.provider_class"] = provider_class
    if resp is None:
        return attrs
    attrs["llm.finish_reason"] = getattr(resp, "finish_reason", None)
    attrs["llm.output_preview"] = _preview(getattr(resp, "content", None))
    tool_calls = getattr(resp, "tool_calls", None) or []
    attrs["llm.tool_call_count"] = len(tool_calls)
    if tool_calls:
        attrs["llm.tool_names"] = [getattr(t, "name", None) for t in tool_calls]
    u = usage_mod.normalize(getattr(resp, "usage", None), model)
    attrs["llm.usage.input_tokens"] = u["input_tokens"]
    attrs["llm.usage.output_tokens"] = u["output_tokens"]
    attrs["llm.usage.cache_read_tokens"] = u["cache_read_tokens"]
    attrs["llm.usage.cache_write_tokens"] = u["cache_write_tokens"]
    attrs["llm.usage.total_tokens"] = u["total_tokens"]
    attrs["llm.usage.cost_total"] = u["cost_usd"]
    reasoning = getattr(resp, "reasoning_content", None)
    if reasoning:
        attrs["llm.reasoning_preview"] = _preview(reasoning)
    return attrs


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json as _json

        return _json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _llm_input_payload(
    provider: str, model: str | None, messages: Any, tools: Any, provider_class: str | None = None
) -> dict:
    """Artifact payload for the model-input card.

    raven passes ONE flat ``messages`` list (system + prior turns + current).
    We split it into three non-overlapping views for the viewer:
      - ``systemPrompt``: the system message,
      - ``prompt``: the latest user message (the current input to this call),
      - ``historyMessages``: the prior turns only — everything EXCEPT the system
        message and that latest user message (so it doesn't duplicate them).
    ``messages`` keeps the full raw list as the ground truth of what was sent.
    """
    msgs = messages if isinstance(messages, list) else []
    system_prompt = ""
    user_prompt = ""
    system_idxs: set[int] = set()
    last_user_idx: int | None = None
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "system":
            system_idxs.add(i)
            if not system_prompt:
                system_prompt = _coerce_text(m.get("content"))
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") == "user":
            user_prompt = _coerce_text(m.get("content"))
            last_user_idx = i
            break
    history = [m for i, m in enumerate(msgs) if i not in system_idxs and i != last_user_idx]
    return {
        "provider": provider,
        "providerClass": provider_class,
        "model": model,
        "systemPrompt": system_prompt,
        "prompt": user_prompt,
        "historyMessages": history,
        "messages": messages,
        "tools": tools,
    }


def _llm_output_payload(resp: Any) -> Any:
    if resp is None:
        return None
    content = getattr(resp, "content", None)
    return {
        "content": content,
        "output": content,  # field the shared viewer's model-output card reads
        "finish_reason": getattr(resp, "finish_reason", None),
        "tool_calls": [
            {"id": getattr(t, "id", None), "name": getattr(t, "name", None), "arguments": getattr(t, "arguments", None)}
            for t in (getattr(resp, "tool_calls", None) or [])
        ],
        "reasoning_content": getattr(resp, "reasoning_content", None),
        "usage": getattr(resp, "usage", None),
    }


def _parse_skill_name(result: Any) -> str | None:
    """Skill tools return ``"## {name}\\n..."`` (hub: ``"## {name} ({version})"``)."""
    if not isinstance(result, str) or not result.strip():
        return None
    first = result.lstrip().splitlines()[0]
    if not first.startswith("## "):
        return None
    name = first[3:].strip()
    if name.endswith(")") and " (" in name:
        name = name[: name.rindex(" (")].strip()
    return name or None


def _skill_scripts_dir(result: Any) -> str | None:
    """``use_skill`` embeds a ``scripts_dir: <path>`` line when it materialized a
    runnable bundle; a plain body read (``read_skill`` / ``read_file``) never does.
    Surface that path so "pulled a bundle" vs "just instructions" is a first-class
    signal on the one Skill node, rather than buried in the output blob."""
    if not isinstance(result, str):
        return None
    for line in result.splitlines():
        stripped = line.strip()
        if stripped.startswith("scripts_dir:"):
            return stripped[len("scripts_dir:") :].strip() or None
    return None


def _skill_attrs(tool_name: str, params: Any, result: Any) -> dict[str, Any]:
    skill_id = params.get("skill_id") if isinstance(params, dict) else None
    source = native = None
    if isinstance(skill_id, str) and "/" in skill_id:
        source, _, native = skill_id.partition("/")
    attrs: dict[str, Any] = {
        "skill.id": skill_id,
        "skill.source": source,
        "skill.native_id": native,
        "skill.name": _parse_skill_name(result),
        "skill.tool": tool_name,
        "skill.read.via_tool": tool_name,
        "skill.result_preview": _preview(result),
    }
    scripts_dir = _skill_scripts_dir(result)
    if scripts_dir:
        attrs["skill.scripts_dir"] = scripts_dir
    return attrs


def _skill_name_from_path(path: str | None) -> str | None:
    """``…/skills/weather/SKILL.md`` → ``weather`` (the skill dir name)."""
    if not path:
        return None
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if len(parts) >= 2 and parts[-1].lower() == "skill.md":
        return parts[-2]
    return parts[-1] if parts else None


def _skill_read_path(name: str, params: Any) -> str | None:
    """If a read_file targets a SKILL.md, return that path; else ``None``.

    This is the discovery→injection follow-through: raven's summary mode
    tells the agent to ``read_file`` a skill's SKILL.md, and subagents (which
    only get the skill *catalog*) do the same. Those reads carry the real body
    into context but look like a plain file read — re-type them to skill.read.
    """
    if name not in _FILE_READ_TOOLS:
        return None
    path = params.get("path") if isinstance(params, dict) else (params if isinstance(params, str) else None)
    if isinstance(path, str) and path.replace("\\", "/").lower().rstrip("/").endswith("skill.md"):
        return path
    return None


# Public aliases (the standard's stable attribute/payload builders).
provider_label = _provider_label
llm_attrs = _llm_attrs
llm_input_payload = _llm_input_payload
llm_output_payload = _llm_output_payload


__all__ = [
    "llm_attrs",
    "llm_input_payload",
    "llm_output_payload",
    "provider_label",
    "llm_call",
    "llm_call_stream",
    "tool_call",
    "turn_seed",
    "turn_open",
    "turn",
    "memory_recall",
    "memory_store",
    "memory_feedback",
    "memory_extract",
    "memory_profile_refresh",
    "memory_consolidate",
    "plugin_load",
    "skill_inject_active",
    "skill_inject_skills",
    "subagent",
]


def subagent(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """Extractor for ``SubagentManager._run_subagent``.

    The subagent runs the same decorated primitives (``chat_with_retry`` /
    ``tools.execute``), so its llm/tool spans are captured automatically and — via
    the contextvars snapshot ``asyncio.create_task`` takes at spawn — nest under
    this node under the spawning turn. This node just describes the spawn.
    """
    origin = bound.get("origin") or {}
    span.set(
        {
            "subagent.task_id": bound.get("task_id"),
            "subagent.task": _preview(bound.get("task"), 300),
            "subagent.label": bound.get("label"),
            "subagent.origin_session": origin.get("session_key") if isinstance(origin, dict) else None,
        }
    )


def plugin_load(contribution: str):
    """Return an extractor for a sync ``PluginRegistry.build_*`` factory call."""

    def _extract(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
        span.set(
            {
                "plugin.contribution": contribution,
                "plugin.name": bound.get("name"),
                "plugin.result_type": type(result).__name__ if result is not None else None,
                "plugin.opt_out": exc is None and result is None,
            }
        )

    return _extract


def _skill_inject_fill(span, *, via: str, names: list, ids: list, sources: dict, body_len: int) -> None:
    span.set(
        {
            "skill.inject.via": via,
            "skill.inject.count": len(ids),
            "skill.inject.names": names,
            "skill.inject.ids": ids,
            "skill.inject.sources": sources,
            "skill.inject.body_len": body_len,
        }
    )
    span.artifact(
        "skill.inject",
        {
            "via": via,
            "skills": [{"name": n, "id": i} for n, i in zip(names, ids)],
            "sources": sources,
            "body_len": body_len,
        },
    )


def skill_inject_active(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """``# Active Skills`` — emit only when always-on skills were force-injected."""
    if result is not None and getattr(result, "text", ""):
        self = bound.get("self")
        metas = list(self._skills.get_always_skills() or [])
        cfg = getattr(self._skills, "_config", None)
        always_max = getattr(cfg, "always_max", 5) or 5
        if always_max:
            metas = metas[:always_max]
        if metas:
            _skill_inject_fill(
                span,
                via="active_skills",
                names=[getattr(m, "name", None) for m in metas],
                ids=[str(getattr(m, "id", "")) for m in metas],
                sources=dict(Counter((getattr(m, "source", None) or "?") for m in metas)),
                body_len=len(result.text),
            )
            return
    span.cancel()


def skill_inject_skills(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """``# Skills`` — emit only when gate-selected skills' bodies were rendered."""
    seg_meta = (getattr(result, "meta", None) or {}) if result is not None else {}
    ids = list(seg_meta.get("injected_skill_ids") or [])
    if ids:
        _skill_inject_fill(
            span,
            via="skills_segment",
            names=[str(i).split("/")[-1] for i in ids],
            ids=ids,
            sources=dict(seg_meta.get("skill_hits_by_source") or {}),
            body_len=len(getattr(result, "text", "") or ""),
        )
    else:
        span.cancel()


def _hit_ref(hit: Any) -> dict[str, Any]:
    """Light, serializable view of a RouterHit candidate (avoid dumping bodies)."""
    return {
        "id": getattr(hit, "id", None) or getattr(hit, "skill_id", None),
        "name": getattr(hit, "name", None),
        "source": getattr(hit, "source", None),
        "score": getattr(hit, "score", None),
    }


def skill_rewrite(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """``QueryRewriter.analyze`` — the need_retrieval judgment + query rewrite that
    precedes skill retrieval. Its inner model call nests here (invocation source)."""
    need = getattr(result, "need_retrieval", None)
    rewritten = getattr(result, "rewritten_query", None)
    span.set(
        {
            "skill.rewrite.query_preview": _preview(bound.get("query")),
            "skill.rewrite.need_retrieval": need,
            "skill.rewrite.rewritten": _preview(rewritten),
        }
    )
    span.artifact("skill.rewrite.input", {"query": bound.get("query")})
    span.artifact("skill.rewrite.output", {"need_retrieval": need, "rewritten_query": rewritten})


def skill_gate(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """``LLMGateFilter.filter`` — narrows the skill candidates to the selected few."""
    candidates = bound.get("candidates") or []
    selected = result if isinstance(result, list) else []
    span.set(
        {
            "skill.gate.task_preview": _preview(bound.get("task")),
            "skill.gate.candidate_count": len(candidates),
            "skill.gate.selected_count": len(selected),
        }
    )
    span.artifact(
        "skill.gate.input",
        {
            "task": bound.get("task"),
            "candidates": [_hit_ref(h) for h in candidates],
            "available_tools": bound.get("available_tools"),
        },
    )
    span.artifact("skill.gate.output", {"selected": [_hit_ref(h) for h in selected]})


def context_curate(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """``CuratorSegmentBuilder._slow_path`` — the bounded internal curator LLM loop
    (its per-step model + tool calls nest under this one node)."""
    seg = result
    state = bound.get("state")
    history = getattr(seg, "history", None) or [] if seg is not None else []
    span.set(
        {
            "context.curate.produced": seg is not None,
            "context.curate.history_len": len(history),
        }
    )
    span.artifact(
        "context.curate.input",
        {"turn_id": bound.get("turn_id"), "session_key": getattr(state, "session_key", None)},
    )
    span.artifact(
        "context.curate.output",
        {"produced": seg is not None, "history_len": len(history), "working_state": getattr(seg, "text", None)},
    )


def memory_recall(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    self = bound.get("self")
    span.set(
        {
            "memory.query": _preview(bound.get("query"), 300),
            "memory.scope": "user",
            "memory.user_id": getattr(self, "_user_id", None),
            "memory.top_k": getattr(self, "_memory_top_k", None),
        }
    )
    hits = list(result or [])
    span.set({"memory.hits": len(hits)})
    span.artifact(
        "memory.recall",
        [
            {
                "text": getattr(m, "text", None),
                "score": getattr(m, "score", None),
                "metadata": getattr(m, "metadata", None),
            }
            for m in hits
        ],
    )


def memory_store(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    msgs = bound.get("messages_slice") or []
    span.set({"memory.session_id": bound.get("session_key"), "memory.message_count": len(msgs)})
    span.artifact("memory.store", {"session_id": bound.get("session_key"), "messages": msgs})


def memory_feedback(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    span.set(
        {
            "memory.session_id": bound.get("session_key"),
            "memory.injected": bound.get("injected_skill_ids"),
            "memory.used": bound.get("used_skill_ids"),
        }
    )


def memory_extract(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    msgs = bound.get("messages") or []
    span.set(
        {
            "memory.surface": "host",
            "memory.model": bound.get("model"),
            "memory.message_count": len(msgs),
            "memory.enable_foresight": bound.get("enable_foresight"),
            "memory.annotated": bool(result),
        }
    )


def memory_profile_refresh(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    span.set(
        {
            "memory.model": bound.get("model"),
            "memory.threshold": bound.get("threshold"),
            "memory.sections_rewritten": result if isinstance(result, int) else None,
        }
    )


def memory_consolidate(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    session = bound.get("session")
    span.set(
        {
            "memory.session_key": getattr(session, "key", None),
            "memory.last_consolidated": getattr(session, "last_consolidated", None),
            "memory.message_count": len(getattr(session, "messages", []) or []),
        }
    )


def _turn_request(bound: dict[str, Any]) -> Any:
    """The turn payload (``TurnRequest``) — first arg after ``self``, by name or position."""
    for key in ("req", "msg"):
        if key in bound:
            return bound[key]
    vals = list(bound.values())
    return vals[1] if len(vals) > 1 else None


def _turn_ids(bound: dict[str, Any]) -> tuple[Any, Any, Any]:
    req = _turn_request(bound)
    sk = bound.get("session_key") or getattr(req, "session_key", None)
    channel = getattr(req, "channel", None)
    chat_id = getattr(req, "chat_id", None)
    if not channel or not chat_id:
        ch2, cid2 = _split_session_key(sk)
        channel = channel or ch2
        chat_id = chat_id or cid2
    return sk, channel, chat_id


def _turn_input(bound: dict[str, Any]) -> Any:
    req = _turn_request(bound)
    text = getattr(req, "text", None)
    return text if text is not None else getattr(req, "content", None)


def turn_seed(bound: dict[str, Any]) -> dict[str, Any]:
    """Seed the root turn span's session identity so every child span inherits it."""
    sk, channel, chat_id = _turn_ids(bound)
    return {"session_key": sk, "channel": channel, "chat_id": chat_id}


def turn_open(span, bound: dict[str, Any]) -> None:
    """Record turn input + emit an in-progress root so mid-turn children have a root."""
    _, channel, chat_id = _turn_ids(bound)
    user_input = _turn_input(bound)
    req = _turn_request(bound)
    span.set({"turn.input_preview": _preview(user_input), "turn.in_progress": True})
    span.artifact(
        "turn.input",
        {"content": user_input, "channel": channel, "chat_id": chat_id, "media": getattr(req, "media", None)},
    )
    span.checkpoint()


def turn(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """Finalize the turn span: output + capabilities snapshot."""
    user_input = _turn_input(bound)
    out_content = getattr(result, "content", None) if result is not None else None
    span.set(
        {
            "turn.input_preview": _preview(user_input),
            "turn.output_preview": _preview(out_content),
            "turn.in_progress": False,
        }
    )
    span.set(_turn_capabilities(bound.get("self")))
    span.artifact("turn.output", {"content": out_content})


def _finish_error(span, result) -> None:
    """Mark the span ERROR when the model returned a soft error response."""
    if getattr(result, "finish_reason", None) == "error":
        span.error((getattr(result, "content", "") or "")[:200])


def llm_call(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """Extractor for a non-streaming provider call (``self`` is the provider)."""
    provider = bound.get("self")
    messages = bound.get("messages")
    tools = bound.get("tools")
    model = bound.get("model")
    provider_class = type(provider).__name__ if provider is not None else None
    eff_model = model or getattr(provider, "default_model", None)
    pname = provider_label(eff_model, provider_class)
    span.artifact("llm.input", llm_input_payload(pname, eff_model, messages, tools, provider_class))
    attrs = llm_attrs(result, pname, eff_model, provider_class)
    if span.invocation_source:
        attrs["llm.invocation_source"] = span.invocation_source
    span.set(attrs)
    span.artifact("llm.output", llm_output_payload(result))
    _finish_error(span, result)


def llm_call_stream(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """Extractor for the streaming call (``self`` is the AgentLoop; provider = ``self.provider``)."""
    loop = bound.get("self")
    provider = getattr(loop, "provider", None)
    messages = bound.get("messages")
    tools = bound.get("tools")
    model = bound.get("model")
    provider_class = type(provider).__name__ if provider is not None else "stream"
    eff_model = model or getattr(provider, "default_model", None)
    pname = provider_label(eff_model, provider_class)
    span.artifact("llm.input", llm_input_payload(pname, eff_model, messages, tools, provider_class))
    attrs = llm_attrs(result, pname, eff_model, provider_class)
    attrs["llm.stream"] = True
    if span.invocation_source:
        attrs["llm.invocation_source"] = span.invocation_source
    span.set(attrs)
    span.artifact("llm.output", llm_output_payload(result))
    _finish_error(span, result)


def tool_call(span, bound: dict[str, Any], result: Any, exc: BaseException | None) -> None:
    """Extractor for ``ToolRegistry.execute``.

    Retypes to ``skill.read`` when the tool is a skill tool (``use_skill`` /
    ``read_skill``) or a ``read_file`` targeting a SKILL.md; otherwise stays
    ``tool.call``. All skill accesses share the one ``skill.read`` node kind
    (the tracing standard); the originating tool is preserved in
    ``skill.read.via_tool`` (``use_skill`` / ``read_skill`` / ``read_file``).
    """
    name = bound.get("name")
    params = bound.get("params")
    skill_read_path = _skill_read_path(name, params)
    if name in _SKILL_TOOLS:
        span.retype("skill.read", "skill")
        span.set(_skill_attrs(name, params, result))
    elif skill_read_path:
        span.retype("skill.read", "skill")
        span.set(
            {
                "skill.tool": name,
                "skill.read.via_tool": "read_file",
                "skill.injected_via": "read_file",
                "skill.path": skill_read_path,
                "skill.name": _skill_name_from_path(skill_read_path),
                "skill.result_preview": _preview(result),
            }
        )
    else:
        err = None
        if exc is not None:
            err = repr(exc)
        elif isinstance(result, str) and result.startswith("Error"):
            err = _preview(result, 200)
        span.set(
            {
                "tool.name": name,
                "tool.args_preview": _preview(params, 300),
                "tool.result_preview": _preview(result),
                "tool.error": err,
            }
        )
    span.set({"tool.duration_ms": span.elapsed_ms()})
    span.artifact("tool.input", {"name": name, "params": params})
    span.artifact("tool.output", {"result": result})
    if exc is None and isinstance(result, str) and result.startswith("Error"):
        span.error(_preview(result, 200))
