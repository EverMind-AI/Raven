"""Shared JSON-verdict parsing for the flow's LLM gates.

The verify gate and the conversation gate each ask a model for one JSON
object carrying one boolean, and each carried its own copy of the same
25-line parser. Two copies is how the ``"pass": 0`` hole survived: the
string form ``"false"`` was coerced in both, the equally JSON-legal
integer form in neither, so an integer verdict parsed to ``None`` and
took each gate's fail-open path. The extraction and the coercion live
here once; each gate keeps its own fail-open direction and its own
post-processing of the surrounding dict.
"""

from __future__ import annotations

import json


def coerce_bool(value: object) -> bool | None:
    """A JSON verdict boolean, accepting the shapes models actually emit.

    ``true``/``false`` literals, the string forms ``"true"``/``"false"``,
    and the integer forms ``0``/``1``. Anything else is ``None`` - the
    caller decides which way that fails.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def parse_bool_verdict(text: str, key: str) -> tuple[dict, bool] | None:
    """First JSON object in ``text`` whose ``key`` coerces to a boolean.

    Tries the whole text, then the outermost brace span, through
    ``json_repair`` with a strict-``json`` fallback. Returns the parsed
    dict (with ``key`` normalised to a real bool) and the bool, or
    ``None`` when no candidate carries a usable verdict.
    """
    if not text:
        return None
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        data = None
        try:
            import json_repair

            data = json_repair.loads(candidate)
        except Exception:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
        if not isinstance(data, dict):
            continue
        verdict = coerce_bool(data.get(key))
        if verdict is None:
            continue
        data[key] = verdict
        return data, verdict
    return None


__all__ = ["coerce_bool", "parse_bool_verdict"]
