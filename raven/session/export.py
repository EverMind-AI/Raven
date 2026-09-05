"""Render a stored session into a human-readable Markdown transcript.

Pure rendering (``render_transcript``) is separated from the file write
(``write_transcript``) so every export surface — the ``session.export`` RPC,
the TUI ``/export`` slash command, and the CLI ``session export`` — shares one
rendering and one destination convention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.session.manager import Session
from raven.utils.helpers import ensure_dir, safe_filename

_ROLE_HEADINGS = {
    "user": "## 🧑 User",
    "assistant": "## 🤖 Assistant",
    "system": "## ⚙️ System",
    "tool": "## 🛠 Tool result",
}


def render_transcript(session: Session) -> str:
    """Render ``session`` to a full-fidelity Markdown transcript.

    Includes a header (key, timestamps, message count, title when set) and each
    message in order: user/assistant/system/tool under distinct headings, the
    assistant reasoning block when present, and tool calls/results as fenced
    blocks. Pure — performs no I/O.
    """
    parts: list[str] = [_render_header(session)]
    for msg in session.messages:
        parts.append(_render_message(msg))
    return "\n\n".join(p for p in parts if p) + "\n"


def default_export_path(workspace: Path, key: str) -> Path:
    """Default destination for a session export: ``<workspace>/exports/<sid>.md``.

    The session key's ``:`` is folded to a filesystem-safe name via
    ``safe_filename`` (same encoding the session store uses for its files).
    """
    return Path(workspace) / "exports" / f"{safe_filename(key)}.md"


def write_transcript(session: Session, dest: Path) -> Path:
    """Render ``session`` and write it to ``dest``, returning the absolute path.

    Creates the parent directory if absent and overwrites any existing file so
    a re-export reflects the session's current state.
    """
    dest = Path(dest)
    ensure_dir(dest.parent)
    dest.write_text(render_transcript(session), encoding="utf-8")
    return dest.resolve()


# ── internals ──────────────────────────────────────────────────────────


def _render_header(session: Session) -> str:
    title = (session.metadata or {}).get("title")
    meta = (
        f"_{session.created_at.isoformat(timespec='seconds')}"
        f" → {session.updated_at.isoformat(timespec='seconds')}"
        f" · {len(session.messages)} messages_"
    )
    lines = [f"# Session `{session.key}`", meta]
    if title:
        lines.insert(1, f"**{title}**")
    return "\n".join(lines)


def _render_message(msg: dict[str, Any]) -> str:
    role = msg.get("role", "")
    heading = _ROLE_HEADINGS.get(role, f"## {role or 'message'}")
    if role == "tool":
        name = msg.get("name") or msg.get("tool_call_id") or ""
        suffix = f": `{name}`" if name else ""
        return f"{heading}{suffix}\n\n{_fenced(_as_text(msg.get('content')))}"

    blocks: list[str] = [heading]
    reasoning = _reasoning_text(msg)
    if reasoning:
        quoted = "\n".join(f"> {line}" for line in reasoning.splitlines() or [""])
        blocks.append(f"> 💭 _thinking_\n{quoted}")
    content = _as_text(msg.get("content"))
    if content:
        blocks.append(content)
    for call in msg.get("tool_calls") or []:
        blocks.append(_render_tool_call(call))
    return "\n\n".join(blocks)


def _render_tool_call(call: dict[str, Any]) -> str:
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name") or "tool"
    args = fn.get("arguments")
    if args is None:
        args = call.get("arguments")
    return f"⏺ **{name}**\n\n{_fenced(_as_text(args))}"


def _reasoning_text(msg: dict[str, Any]) -> str:
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    blocks = msg.get("thinking_blocks")
    if isinstance(blocks, list):
        texts = [b.get("thinking", "") for b in blocks if isinstance(b, dict) and b.get("thinking")]
        if texts:
            return "\n".join(texts)
    return ""


def _as_text(content: Any) -> str:
    """Flatten a message content value (str, multimodal list, or dict) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif part.get("type") == "image_url":
                    out.append("[image]")
                else:
                    out.append(json.dumps(part, ensure_ascii=False))
            else:
                out.append(str(part))
        return "\n".join(out)
    return json.dumps(content, ensure_ascii=False)


def _fenced(text: str) -> str:
    return f"```\n{text}\n```"


__all__ = ["render_transcript", "default_export_path", "write_transcript"]
