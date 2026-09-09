"""User rules and default tiers: which standing one tool call has.

The user's ``permissions.tools`` node maps a tool name to a tier, or -- for
``exec`` -- to a table of command patterns each mapping to a tier. Several
matching rules resolve to the strictest (deny > ask > allow), never to the one
written last. A call no rule speaks about falls to the tool's default tier:
read-only tools run, everything else asks -- with one exception, and
``DEFAULT_ALLOW_TOOLS`` says why it is there: ``deliver_files`` hands the user
a file they asked for, which is not a read and is not an effect they approve.

Exec pattern matching is prefix-by-token on the raw command, deliberately
without wrapper stripping: ``git *`` must not allow ``sudo git push``. A
compound command allows only when every segment allows; a command carrying
redirection, substitution or expansion is not split and answers only to a
``*`` rule, so a rule written for a prefix cannot be smuggled past by what
follows it. The builtin classifier
(``raven.permissions.builtin``) keeps its own, stripping view of the same
command; a rule here can never lift what it rules.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from raven.contracts.permissions import Tier

_STRICTNESS = {Tier.DENY: 2, Tier.ASK: 1, Tier.ALLOW: 0}

# Tools whose worst case is reading what the agent may already read, plus
# deliver_files: its recipient is the user themself, and it is the only route a
# finished artifact has to them. Asking there cost the work rather than guarding
# it -- the request expires while the agent waits, and the reply that follows
# hands over a path instead, which reaches nobody. Everything absent from this
# set defaults to asking, unknown (MCP) tools included.
DEFAULT_ALLOW_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_dir",
        "grep",
        "find",
        "glob",
        "tool_search",
        "tool_call",
        "web_search",
        "web_fetch",
        "deliver_files",
        "message",
        "ask_user",
        "read_skill",
        "use_skill",
        "load_playbook",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
    }
)

# Text the shell would expand or reroute before running: a token view of the
# written command is not a view of what runs, so pattern matching falls back to
# the whole-command rules only.
_OPAQUE = re.compile(r"[><$`]")
_SEGMENT_BOUNDARY = re.compile(r"(?:\|\|?|&&?|;|\n)")


def _strictest(tiers: "list[Tier]") -> Tier | None:
    if not tiers:
        return None
    return max(tiers, key=lambda t: _STRICTNESS[t])


def default_tier(tool_name: str) -> Tier:
    return Tier.ALLOW if tool_name in DEFAULT_ALLOW_TOOLS else Tier.ASK


def _pattern_tiers(table: dict[str, str]) -> list[tuple[str, list[str] | None, Tier]]:
    """Each rule as (raw pattern, prefix tokens or None for ``*``, tier)."""
    rules: list[tuple[str, list[str] | None, Tier]] = []
    for pattern, tier_name in table.items():
        try:
            tier = Tier(tier_name)
        except ValueError:
            continue
        if pattern.strip() == "*":
            rules.append((pattern, None, tier))
            continue
        try:
            tokens = shlex.split(pattern)
        except ValueError:
            continue
        rules.append((pattern, tokens, tier))
    return rules


def _segment_tier(tokens: list[str], rules: list[tuple[str, list[str] | None, Tier]]) -> Tier | None:
    """Strictest among the specific patterns that match; ``*`` answers only when
    none does -- it is the table's own fallback, and letting it compete would
    make ``{"*": "ask", "git *": "allow"}`` unable to allow anything."""
    matched: list[Tier] = []
    fallback: Tier | None = None
    for _, prefix, tier in rules:
        if prefix is None:
            fallback = tier if fallback is None else _strictest([fallback, tier])
            continue
        if prefix and prefix[-1] == "*":
            head = prefix[:-1]
            if len(tokens) >= len(head) and tokens[: len(head)] == head:
                matched.append(tier)
        elif tokens == prefix:
            matched.append(tier)
    if matched:
        return _strictest(matched)
    return fallback


def exec_rule_tier(command: str, table: dict[str, str]) -> Tier | None:
    """The user's tier for one shell command, or None when no rule speaks.

    Deny wins on any segment; allow requires every segment to allow. A segment
    no rule speaks about leaves the command unresolved unless another segment
    already denied -- half-covered is not covered.
    """
    rules = _pattern_tiers(table)
    if not rules:
        return None
    if _OPAQUE.search(command):
        return _strictest([t for _, pref, t in rules if pref is None])

    segment_tiers: list[Tier | None] = []
    for part in _SEGMENT_BOUNDARY.split(command):
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            return Tier.ASK
        if not tokens:
            continue
        segment_tiers.append(_segment_tier(tokens, rules))

    if not segment_tiers:
        return None
    if any(t is Tier.DENY for t in segment_tiers):
        return Tier.DENY
    if any(t is None for t in segment_tiers):
        return None
    return _strictest([t for t in segment_tiers if t is not None])


def user_tier(tool_name: str, params: dict[str, Any], tools_node: dict[str, Any]) -> Tier | None:
    """The user's tier for this call, or None when their config has no rule."""
    entry = tools_node.get(tool_name)
    if entry is None:
        return None
    if isinstance(entry, str):
        try:
            return Tier(entry)
        except ValueError:
            return None
    if isinstance(entry, dict):
        command = params.get("command")
        if tool_name == "exec" and isinstance(command, str):
            return exec_rule_tier(command, entry)
        return None
    return None


__all__ = ["DEFAULT_ALLOW_TOOLS", "default_tier", "exec_rule_tier", "user_tier"]
