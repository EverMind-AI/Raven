"""The reader layout's report file: the delivered reply written over the file the turn saved.

A research turn may also save its report with ``write_file``. That call runs before the
draft is reviewed and shaped, and the shape gate, the reviewer and the appendix act on
the reply alone, so the saved file and the reply drift apart - a different structure,
unreviewed text. Under the reader layout the reply already is the form a reader gets, so
at turn end the flow writes it over the markdown file the turn last wrote, and the chat
reply and the file are one text. The research trail stays on the reply only.

The path is read from ``write_file``'s own result line, which names the path the tool
resolved; the model's argument may be relative to a workspace this plugin cannot see.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_WRITE_TOOL = "write_file"

# The success lines of ``WriteFileTool.execute`` (raven/agent/tools/filesystem.py).
_RESULT_PATH_RE = re.compile(
    r"^(?:Successfully (?:wrote|appended) \d+ bytes to (?P<written>.+?\.md)"
    r"|File unchanged: (?P<same>.+?\.md) already holds .*)$",
    re.M | re.I,
)

# A reply this much shorter than the file is a note about the file, not the report:
# a brief that asks for "reply with the path and a summary" gets a reply in the
# report's shape, and writing it over the file would replace the report with the
# note. The product run that showed this replied with 47% of the file's length.
_MIN_REPLY_SHARE = 0.8


def written_markdown(messages: Iterable[dict[str, Any]]) -> list[str]:
    """Markdown paths this turn's ``write_file`` calls wrote, oldest write first."""
    paths: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != _WRITE_TOOL:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        hit = _RESULT_PATH_RE.search(content)
        if hit is None:
            continue
        path = hit.group("written") or hit.group("same")
        if path in paths:
            paths.remove(path)
        paths.append(path)
    return paths


def sync_report_file(paths: list[str], reply: str) -> dict[str, Any]:
    """Write ``reply`` over the last markdown file written; the observer payload, or ``{}``.

    Never raises: the reply has already gone out, and a file that could not be
    rewritten is recorded rather than turned into a failed turn. A reply well short
    of the file is left out of it, so a summary never replaces the report.
    """
    if not paths:
        return {}
    target = Path(paths[-1])
    record: dict[str, Any] = {"path": str(target), "files_written": len(paths)}
    text = reply.strip()
    try:
        held = len(target.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        held = 0
    if len(text) < held * _MIN_REPLY_SHARE:
        record.update(synced=False, reason="reply_shorter_than_file", reply_chars=len(text), file_chars=held)
        return record
    try:
        target.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        record.update(synced=False, reason=f"write_failed:{type(exc).__name__}")
    else:
        record["synced"] = True
    return record


__all__ = ["sync_report_file", "written_markdown"]
