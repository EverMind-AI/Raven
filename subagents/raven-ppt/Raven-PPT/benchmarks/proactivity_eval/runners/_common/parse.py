"""Extract the ``{should_help, proposed_task, reason}`` JSON decision from
a free-form agent reply.

Two fallback regex stages:
  1. ```json ... ``` fenced block (preferred — most LLMs emit this)
  2. Inline ``{ ... "should_help" ... }`` object

Returns a dict with parse_ok=False on failure so callers can still build a
row (treated as predicted_help=False for scoring).
"""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BRACE_RE = re.compile(r"\{[^{}]*?\"should_help\"[^{}]*?(?:\{[^{}]*\}[^{}]*)?\}", re.DOTALL)


def parse_decision(text: str | None) -> dict[str, Any]:
    """Return {should_help, proposed_task, reason, parse_ok}."""
    raw_json: str | None = None
    if text:
        m = _JSON_FENCE_RE.search(text)
        if m:
            raw_json = m.group(1)
        else:
            m = _JSON_BRACE_RE.search(text)
            if m:
                raw_json = m.group(0)

    if raw_json is None:
        return {
            "should_help": None,
            "proposed_task": None,
            "reason": "parse_error: no JSON",
            "parse_ok": False,
        }
    try:
        # strict=False tolerates raw control chars (notably unescaped LF)
        # inside string values — Qwen 27B-class models emit literal newlines
        # in long ``reason`` text, which is structurally valid JSON to most
        # consumers but stdlib rejects in strict mode.
        parsed = json.loads(raw_json, strict=False)
    except json.JSONDecodeError as e:
        return {
            "should_help": None,
            "proposed_task": None,
            "reason": f"parse_error: {e}",
            "parse_ok": False,
        }

    sh = parsed.get("should_help")
    pt = parsed.get("proposed_task")
    rn = parsed.get("reason", "")
    if isinstance(pt, str) and pt.strip().lower() in ("null", "none", ""):
        pt = None
    return {
        "should_help": bool(sh) if sh is not None else None,
        "proposed_task": pt,
        "reason": rn if isinstance(rn, str) else str(rn),
        "parse_ok": True,
    }


__all__ = ["parse_decision"]
