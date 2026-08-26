"""Classify a Raven-X tool call for a client that has to draw it.

``ToolKind`` picks an icon and lets a client optimise how it renders progress.
It is optional in the schema, so the cost of getting it wrong is cosmetic and
the cost of omitting it is also cosmetic -- which is exactly why this file is
small and refuses to guess. Every name Raven-X actually registers is mapped;
anything else, including the ``mcp_<server>_<tool>`` names an MCP server brings
at runtime, is ``other``. A wrong icon is worse than a generic one.

``locations`` is not cosmetic. The spec requires paths inside the protocol to
be absolute, and raven's file tools accept workspace-relative ones, so a
location forwarded verbatim points a client's "follow along" cursor at nothing.
The extractor resolves against the session's working directory and drops
anything it cannot make absolute.

Ported from the main raven repo's ``raven/acp/tool_kinds.py``; the ``_KINDS``
table is rebuilt against Raven-X's own registry
(``AgentLoop._register_default_tools``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Every tool name Raven-X registers, by the ToolKind that describes what it
# does. Grouped rather than alphabetised so a reader can check the
# classification against the ten kinds instead of against the tool list.
_KINDS: dict[str, str] = {
    # Reading files or data. ``use_skill`` / ``read_skill`` return a skill body
    # for the model to read.
    "read_file": "read",
    "list_dir": "read",
    "read_skill": "read",
    "use_skill": "read",
    # Modifying files or content.
    "write_file": "edit",
    "edit_file": "edit",
    # Searching for information. ``web_search`` is here rather than under
    # fetch: it returns results to choose from, while ``web_fetch`` retrieves
    # one named thing.
    "grep": "search",
    "find": "search",
    "tool_search": "search",
    "web_search": "search",
    # Running commands or code. ``spawn`` is a process running, not a file
    # changing.
    "exec": "execute",
    "spawn": "execute",
    # Retrieving external data.
    "web_fetch": "fetch",
    # Everything Raven-X has that is none of the above. Listed explicitly so
    # that adding a tool is a visible decision: a name absent from this table
    # gets "other" either way, but a name present in it has been looked at.
    "message": "other",
    "ask_user": "other",
    "cron": "other",
    "image_generate": "other",
    "text_to_speech": "other",
    "video_generate": "other",
}

# Argument keys that hold a path. ``path`` is the convention every raven file
# and search tool follows; the others are the exceptions.
_PATH_KEYS = ("path", "file_path")
_PATH_LIST_KEYS = ("paths", "files")

# How many locations one tool call may contribute: a client draws these, and
# the first handful is what a person reads anyway.
MAX_LOCATIONS = 8


def tool_kind(name: str | None) -> str:
    """The ``ToolKind`` for a tool name, defaulting to ``other``."""
    if not name:
        return "other"
    return _KINDS.get(name, "other")


def title_for(name: str | None, arguments: dict[str, Any] | None, display: str | None = None) -> str:
    """One line naming what the tool is doing, for the tool row's title.

    ``display`` is the runtime's own rendering when it set one, and it wins.
    Otherwise the tool name plus its most identifying argument: the path for a
    file tool, the command for exec. Never empty -- ``title`` is required on
    ``ToolCall``, and an empty string renders as a blank row.
    """
    if display and display.strip():
        return display.strip()
    args = arguments if isinstance(arguments, dict) else {}
    subject = ""
    if isinstance(args.get("command"), str):
        subject = args["command"]
    else:
        for key in (*_PATH_KEYS, "pattern", "query", "url"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                subject = value.strip()
                break
    label = name or "tool"
    if not subject:
        return label
    # Truncated by characters rather than words: a title is one line in a panel
    # whose width nobody here knows.
    if len(subject) > 120:
        subject = subject[:119] + "…"
    return f"{label}: {subject}"


def locations(arguments: dict[str, Any] | None, cwd: str | Path | None) -> list[dict[str, Any]]:
    """The absolute paths this call touches, as ``ToolCallLocation`` objects.

    Relative paths are resolved against ``cwd`` -- the session's working
    directory, not the process's. A path that cannot be made absolute is
    dropped, not sent relative: the spec requires absolute, and a client
    following a relative path finds nothing or a different file.
    """
    args = arguments if isinstance(arguments, dict) else {}
    base = Path(cwd) if cwd else None
    raw: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            raw.append(value.strip())
    for key in _PATH_LIST_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            raw.extend(item.strip() for item in value if isinstance(item, str) and item.strip())

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        resolved = absolute_path(item, base)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        out.append({"path": resolved})
        if len(out) >= MAX_LOCATIONS:
            break
    return out


def absolute_path(value: str, base: Path | None) -> str | None:
    """``value`` as an absolute path string, or ``None`` if it cannot be one.

    No ``resolve()``: symlinks are left alone deliberately. The client is being
    told which file the agent is working on, and resolving can produce a
    different string from the one the editor has open (macOS ``/tmp`` vs
    ``/private/tmp``), breaking the follow-along it is for.
    """
    try:
        path = Path(value).expanduser()
    except (ValueError, RuntimeError):
        # A null byte, or a ``~user`` that does not exist. One unusable
        # argument must not cost the whole tool row.
        return None
    if path.is_absolute():
        return str(path)
    if base is None or not base.is_absolute():
        return None
    return str(base / path)


__all__ = ["MAX_LOCATIONS", "absolute_path", "locations", "title_for", "tool_kind"]
