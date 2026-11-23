"""Managed storage for a tool result too large to replay in full.

The bounded preview is what the model is sent and what the session files; the
full body is written to one temporary file under the workspace and named in a
trusted hint after the untrusted fence. This mirrors opencode's Model Tool
Output / Managed Tool Output File split: the preview is the durable replayable
record, the file is a retrieval aid that may expire.

A result is bounded by line count or UTF-8 byte count, whichever is reached
first; the legacy character cap is checked alongside them. Shell output keeps
its tail -- the part a command usually fails on -- while every other tool keeps
its head.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from raven.security.trust import fence_span
from raven.session.refs import TOOL_RESULT_TRUNCATION_MARKER

TOOL_OUTPUT_DIRNAME = "tool-output"
TOOL_OUTPUT_PREFIX = "tool_"
TOOL_OUTPUT_RETENTION = timedelta(days=7)
"""How long a managed full-output file is kept, mirroring opencode."""

TAIL_TOOLS = frozenset({"exec"})
"""Tools whose useful output is at the end, so their preview keeps the tail."""

_HINT_LEAD = "Full output saved to:"
"""What a result this function already bounded carries after its fence."""


@dataclass(frozen=True)
class ToolOutputLimits:
    """The budgets a tool result is bounded by; whichever is hit first wins."""

    max_lines: int = 2_000
    max_bytes: int = 50 * 1024
    max_chars: int = 1_000_000

    def enabled(self) -> bool:
        return self.max_lines > 0 or self.max_bytes > 0 or self.max_chars > 0


def direction_for(tool_name: str | None) -> str:
    """``tail`` for a shell result, ``head`` for every other tool."""
    return "tail" if tool_name in TAIL_TOOLS else "head"


def tool_output_dir(workspace: Path) -> Path:
    """The directory managed full-output files live in for one agent home."""
    return workspace / TOOL_OUTPUT_DIRNAME


def bound_tool_result(
    content: Any,
    workspace: Path,
    *,
    limits: ToolOutputLimits,
    direction: str = "head",
    readers: Sequence[str] = ("read_file", "grep"),
    now: datetime | None = None,
) -> Any:
    """``content`` bounded by ``limits``, with the full body filed.

    The model-visible form is a preview of the chosen side plus the truncation
    marker, followed by a trusted hint naming the file that holds the full
    body. The preview is what the model reads and what the session files; the
    file is the retrieval path.

    Non-string content is returned unchanged (the cap has always guarded
    ``str`` content only), and a result this function already filed is left
    alone so a second pass cannot file it twice.
    """
    if not isinstance(content, str) or not limits.enabled():
        return content
    # A lone surrogate cannot be encoded to UTF-8, so measuring, cutting or
    # filing the body would raise. Every consumer of this string needs valid
    # UTF-8; replacing the unencodable code points keeps the rest intact.
    content = _utf8_safe(content)

    span = fence_span(content)
    opening, body, closing = span if span is not None else ("", content, "")
    # Idempotence is keyed on our own hint, not on the truncation marker: a
    # tool may have cut its own output and still be over this cap.
    if _HINT_LEAD in closing or (span is None and _HINT_LEAD in content):
        return content

    bounded = _bounded_preview(body, limits, direction)
    if bounded is None:
        return content
    preview, omitted, unit = bounded
    visible = _visible_body(preview, direction)

    path = _file_full_output(workspace, body, now=now)
    if path is None:
        # A lost side file costs the model the full tail, never the turn. The
        # result is still bounded, so the send and the write stay equal.
        return f"{opening}{visible}{closing}"

    side = "start" if direction == "head" else "end"
    if "read_file" in readers:
        how = "Use read_file with offset/limit to view specific sections"
        how += ", or grep to search the full content." if "grep" in readers else "."
        how += " Do not read the whole file at once."
    elif "grep" in readers:
        how = "Use grep to search the full content."
    else:
        how = "The full output is at the path above."
    hint = (
        f"The tool output was truncated: {omitted:,} {unit} omitted; "
        f"the preview shows the {side} of the output. {_HINT_LEAD} {path}\n{how}"
    )
    return f"{opening}{visible}{closing}\n{hint}"


def prune_tool_outputs(directory: Path, *, now: datetime | None = None) -> int:
    """Remove managed files older than the retention; best effort, never raises."""
    cutoff = (now.timestamp() if now is not None else time.time()) - TOOL_OUTPUT_RETENTION.total_seconds()
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return removed
    for path in entries:
        if not path.name.startswith(TOOL_OUTPUT_PREFIX):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _utf8_safe(text: str) -> str:
    """``text`` with any lone surrogate replaced, so every later encode succeeds."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return text


def _bounded_preview(body: str, limits: ToolOutputLimits, direction: str) -> tuple[str, int, str] | None:
    """``(preview, omitted, unit)`` for a body over a limit, or ``None`` under all."""
    lines = body.split("\n")
    total_bytes = len(body.encode("utf-8"))
    total_chars = len(body)
    if not (
        (limits.max_lines and len(lines) > limits.max_lines)
        or (limits.max_bytes and total_bytes > limits.max_bytes)
        or (limits.max_chars and total_chars > limits.max_chars)
    ):
        return None

    ordered = lines if direction == "head" else list(reversed(lines))
    kept: list[str] = []
    kept_bytes = 0
    kept_chars = 0
    unit = "lines"
    for line in ordered:
        if limits.max_lines and len(kept) + 1 > limits.max_lines:
            break
        separator = 1 if kept else 0
        line_bytes = len(line.encode("utf-8")) + separator
        line_chars = len(line) + separator
        if limits.max_bytes and kept_bytes + line_bytes > limits.max_bytes:
            unit = "bytes"
            break
        if limits.max_chars and kept_chars + line_chars > limits.max_chars:
            unit = "characters"
            break
        kept.append(line)
        kept_bytes += line_bytes
        kept_chars += line_chars

    if ordered and unit in ("bytes", "characters") and not any(line.strip() for line in kept):
        # A line that is itself over budget has no whole line to keep, and a
        # preview of blank lines alone is empty in everything but size. Keep the
        # head or tail of the line the budget stopped at -- ``ordered[len(kept)]``,
        # not ``ordered[0]``: the lines before it were kept, and the one that did
        # not fit is the one to cut. The character cap applies before the byte cap
        # so a UTF-8 cut never splits a character.
        first = ordered[len(kept)]
        if limits.max_chars:
            first = first[: limits.max_chars] if direction == "head" else first[-limits.max_chars :]
        if limits.max_bytes:
            encoded = first.encode("utf-8")
            encoded = encoded[: limits.max_bytes] if direction == "head" else encoded[-limits.max_bytes :]
            first = encoded.decode("utf-8", "ignore")
        kept = [first]
        kept_bytes = len(first.encode("utf-8"))
        kept_chars = len(first)

    if direction == "tail":
        kept.reverse()
    preview = "\n".join(kept)
    if unit == "bytes":
        omitted = total_bytes - kept_bytes
    elif unit == "characters":
        omitted = total_chars - kept_chars
    else:
        omitted = len(lines) - len(kept)
    return preview, omitted, unit


def _visible_body(preview: str, direction: str) -> str:
    """The fenced body: the marker follows a head preview and precedes a tail one."""
    if direction == "tail":
        return f"{TOOL_RESULT_TRUNCATION_MARKER}\n{preview}"
    return f"{preview}{TOOL_RESULT_TRUNCATION_MARKER}"


def _file_full_output(workspace: Path, body: str, *, now: datetime | None) -> Path | None:
    directory = tool_output_dir(workspace)
    try:
        # A pre-existing tool-output symlink would make mkdir, chmod, open and
        # prune all act on its target. Refuse it, and re-check containment after
        # mkdir so a path that escaped through any other link is refused too.
        if directory.is_symlink():
            raise OSError(f"refusing symlinked tool-output directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = workspace.resolve()
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise OSError(f"tool-output directory escapes the workspace: {directory}")
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        path = directory / f"{TOOL_OUTPUT_PREFIX}{uuid.uuid4().hex}.txt"
        # O_EXCL refuses an existing path (including a symlink), and the mode is
        # applied at creation: there is no window where the file is wider than
        # owner-only on POSIX, which a create-then-chmod would leave. Windows
        # inherits the workspace directory's ACL.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # newline="" keeps the body's own line endings; text mode on Windows
        # would turn every \n into \r\n and double an existing \r\n.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not file a large tool result under {}: {}", directory, exc)
        return None
    prune_tool_outputs(directory, now=now)
    return path.resolve()


__all__ = [
    "TAIL_TOOLS",
    "TOOL_OUTPUT_DIRNAME",
    "TOOL_OUTPUT_PREFIX",
    "TOOL_OUTPUT_RETENTION",
    "ToolOutputLimits",
    "bound_tool_result",
    "direction_for",
    "prune_tool_outputs",
    "tool_output_dir",
]
