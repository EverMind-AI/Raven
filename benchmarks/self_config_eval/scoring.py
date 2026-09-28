"""Deterministic scoring for the self-configuration eval.

A case's ``expect`` block is matched against the tool calls the turn made, as
the RPC stream reported them (``tool.start``: name and arguments). Calls made
through ``tool_call`` (tool search) are unwrapped to the tool they forwarded
to, so a deferred tool scores the same as a visible one.

Pure functions over plain data, so the scorer is testable without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class Verdict:
    passed: bool
    failures: list[str] = field(default_factory=list)


def unwrap(call: ToolCall) -> ToolCall:
    if call.name == "tool_call" and isinstance(call.arguments.get("name"), str):
        inner = call.arguments.get("arguments")
        return ToolCall(call.arguments["name"], inner if isinstance(inner, dict) else {})
    return call


def _value(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def matches(call: ToolCall, want: dict[str, Any]) -> bool:
    """Whether one recorded call satisfies one matcher."""
    call = unwrap(call)
    if want.get("tool") and call.name != want["tool"]:
        return False
    args = call.arguments
    action = args.get("action")
    if "action" in want and action not in want["action"]:
        return False
    path = str(args.get("path") or "")
    if "path" in want and path != want["path"]:
        return False
    if "path_prefix" in want and not path.startswith(want["path_prefix"]):
        return False
    if "path_contains" in want and want["path_contains"].lower() not in path.lower():
        return False
    if "value" in want and _value(args.get("value")) != want["value"]:
        return False
    if "args_contain" in want:
        blob = json.dumps(args, ensure_ascii=False).lower()
        if want["args_contain"].lower() not in blob:
            return False
    return True


def _describe(want: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in want.items())


def lookup(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def score(expect: dict[str, Any], calls: list[ToolCall], config_after: dict[str, Any] | None = None) -> Verdict:
    """Every ``must`` matcher hit, no ``must_not`` matcher hit, the file as expected."""
    failures: list[str] = []
    for want in expect.get("must", []) or []:
        if not any(matches(c, want) for c in calls):
            failures.append(f"missing call: {_describe(want)}")
    for want in expect.get("must_not", []) or []:
        hits = [unwrap(c) for c in calls if matches(c, want)]
        if hits:
            failures.append(
                f"forbidden call: {_describe(want)} -> {hits[0].name} {json.dumps(hits[0].arguments)[:160]}"
            )
    for dotted, expected in (expect.get("config_after") or {}).items():
        got = lookup(config_after or {}, dotted)
        if got != expected:
            failures.append(f"config {dotted} is {got!r}, expected {expected!r}")
    return Verdict(passed=not failures, failures=failures)


__all__ = ["ToolCall", "Verdict", "lookup", "matches", "score", "unwrap"]
