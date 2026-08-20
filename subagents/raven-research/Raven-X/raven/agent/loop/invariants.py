"""Per-turn invariants — the instrument that must not be gated on what it measures.

Five defects in six batches were found by forensics after a batch had already
reported, not by the harness at the first question: turns killed silently by a
context-window overflow, a chain of thought committed as the final answer, a
salvage counter that never reached the trajectory, a search backend returning
error text inside a successful-looking result, and a tool the config could not
account for. Each one cost a whole batch, and in every case the artifacts needed
to notice it at question one were already in hand.

Three deliberate design choices:

* **Not an AgentHook.** ``AgentLoop`` builds its hook context only when at least
  one hook is registered, so a checker dispatched through hooks would stop
  running exactly when someone prunes the default observers — the failure mode it
  exists to catch. These run unconditionally from the turn-end path.
* **Read-only.** Nothing here can change what the model sees or which turns are
  re-sampled, so the stamp is wire-safe and needs no flow-version bump. That also
  means it runs identically on the measurement anchor, which is the only way a
  cross-arm comparison of the counts means anything.
* **Always emitted, even on a clean turn.** The observer namespaces omit
  themselves when they have no signal, and that convention is what allowed a
  missing key to be read as a measured zero. Here, absence of the stamp means the
  build predates the checker — a different fact, and it must stay distinguishable
  from "checked, nothing wrong".
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from raven.agent.flow.answer_text import visible_answer

CHECKER_VERSION = 1

_UNTRUSTED_HEAD = re.compile(r"\s*\[BEGIN UNTRUSTED [^\]]*\]\s*\n?")
_ERROR_HEAD = re.compile(r"\s*(Error:|Proxy error:)")
_EMPTY_HEAD = "No results for:"


def _unwrap(content: str) -> str:
    """Strip the untrusted-data fence the loop adds around every tool result.

    The fence is applied on this side, which is why the error verdict belongs on
    this side too: downstream detection has already broken twice, once because it
    matched two backend-specific strings and once because the fence pushed the
    error head off the first line and an anchored match stopped firing. Both times
    the counter silently read zero while the real rate was tens of percent.
    """
    head = _UNTRUSTED_HEAD.match(content)
    return content[head.end() :] if head else content


def classify_tool_result(content: Any) -> str:
    """``error`` | ``empty`` | ``ok`` for one tool result.

    ``empty`` is kept apart from ``error``: a search that legitimately found
    nothing is a retrieval outcome, while an error means the retrieval surface
    itself is not answering, and only the second one invalidates a batch.
    """
    text = content if isinstance(content, str) else str(content or "")
    body = _unwrap(text).lstrip()
    if _ERROR_HEAD.match(body[:120]) or body[:12].startswith('{"error"'):
        return "error"
    if "Serper API key not configured" in text:
        return "error"
    if body.startswith(_EMPTY_HEAD):
        return "empty"
    return "ok"


def turn_invariants(
    messages: list[dict[str, Any]],
    *,
    declared_tools: Iterable[str],
    metadata: dict[str, Any] | None = None,
    closing_tag_required: bool = False,
) -> dict[str, Any]:
    """Check the turn's terminal state against four invariants.

    Returns a stamp that always carries ``ok`` and ``checker``, so a batch can
    tell "checked and clean" from "not checked".
    """
    meta = metadata or {}
    force_finalize = meta.get("force_finalize") or {}
    salvage_committed = int(force_finalize.get("salvage_committed") or 0)

    called: dict[str, int] = {}
    tool_results: dict[str, dict[str, int]] = {}
    pending: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or call.get("name")
                if not name:
                    continue
                called[name] = called.get(name, 0) + 1
                call_id = call.get("id")
                if call_id:
                    pending[call_id] = name
        elif role == "tool":
            name = message.get("name") or pending.get(message.get("tool_call_id") or "") or "?"
            verdict = classify_tool_result(message.get("content"))
            tool_results.setdefault(name, {})
            tool_results[name][verdict] = tool_results[name].get(verdict, 0) + 1

    final_content = ""
    for message in reversed(messages):
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                final_content = content
                break

    answer = visible_answer(final_content, closing_tag_required=closing_tag_required)
    declared = {name for name in declared_tools}
    undeclared = sorted(set(called) - declared)

    violations: list[str] = []
    # A committed answer whose shape cannot be accounted for. Under a template
    # that prefills the opening think tag, a completed turn carries a closing
    # one; a salvage carries none because the reasoning was already folded away,
    # which is exactly why it must be marked. Anything else is a chain of thought
    # about to be scored as an answer.
    if final_content and not answer and salvage_committed == 0:
        violations.append("answer_shape_unaccounted")
    # "An empty outcome must be RECORDED as empty" — not "a gate must have fired".
    # Keying this on ``force_finalize`` would make it arm-correlated by
    # construction: with the flow off the terminal gate is never built, so every
    # empty anchor turn would read as a violation while the same turn on the other
    # arm read as clean. The loop records its own verdict in ``turn_end`` on both
    # arms, so the invariant is that the record exists.
    recorded = "turn_end" in meta or bool(force_finalize.get("terminal_hits"))
    if not answer and not recorded:
        violations.append("silently_empty")
    if undeclared:
        violations.append("undeclared_tool")
    if any(counts.get("error") for counts in tool_results.values()):
        violations.append("tool_error_result")

    stamp: dict[str, Any] = {
        "checker": CHECKER_VERSION,
        "ok": not violations,
        "tool_calls": called,
        "tool_results": tool_results,
        "answer_chars": len(answer),
        "salvage_committed": salvage_committed,
    }
    if violations:
        stamp["violations"] = violations
    if undeclared:
        stamp["undeclared"] = undeclared
    return stamp


__all__ = ["CHECKER_VERSION", "classify_tool_result", "turn_invariants"]
