"""Pure data carriers and parsing/decision helpers for the Mochat adapter.

These have no I/O and are unit-tested directly; the socket / HTTP / buffering
machinery lives in :mod:`.channel`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from raven.config.schema import MochatConfig


@dataclass
class MochatBufferedEntry:
    """One inbound entry held for delayed (debounced) dispatch."""

    raw_body: str
    author: str
    sender_name: str = ""
    sender_username: str = ""
    timestamp: int | None = None
    message_id: str = ""
    group_id: str = ""


@dataclass
class MochatTarget:
    """Resolved outbound target: an id and whether it's a panel (group)."""

    id: str
    is_panel: bool


def safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def str_field(src: dict, *keys: str) -> str:
    """First non-empty stripped string among *keys*, else ''."""
    for key in keys:
        value = src.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def make_synthetic_event(
    message_id: str,
    author: str,
    content: Any,
    meta: Any,
    group_id: str,
    converse_id: str,
    timestamp: Any = None,
    *,
    author_info: Any = None,
) -> dict[str, Any]:
    """Build a synthetic ``message.add`` event (used by the polling fallback)."""
    payload: dict[str, Any] = {
        "messageId": message_id,
        "author": author,
        "content": content,
        "meta": safe_dict(meta),
        "groupId": group_id,
        "converseId": converse_id,
    }
    if author_info is not None:
        payload["authorInfo"] = safe_dict(author_info)
    return {
        "type": "message.add",
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def normalize_content(content: Any) -> str:
    """Flatten a content payload to text."""
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def resolve_target(raw: str) -> MochatTarget:
    """Resolve an outbound target id + kind from a user-supplied string.

    Honors ``mochat:`` / ``group:`` / ``channel:`` / ``panel:`` prefixes; the
    latter three force panel mode. Without a prefix, anything not starting
    with ``session_`` is treated as a panel.
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return MochatTarget(id="", is_panel=False)
    lowered = trimmed.lower()
    cleaned, forced_panel = trimmed, False
    for prefix in ("mochat:", "group:", "channel:", "panel:"):
        if lowered.startswith(prefix):
            cleaned = trimmed[len(prefix) :].strip()
            forced_panel = prefix in {"group:", "channel:", "panel:"}
            break
    if not cleaned:
        return MochatTarget(id="", is_panel=False)
    return MochatTarget(id=cleaned, is_panel=forced_panel or not cleaned.startswith("session_"))


def extract_mention_ids(value: Any) -> list[str]:
    """Pull mention ids out of a heterogeneous list (strings or dicts)."""
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            for key in ("id", "userId", "_id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(candidate.strip())
                    break
    return ids


def resolve_was_mentioned(payload: dict[str, Any], agent_user_id: str) -> bool:
    """Decide whether the agent was mentioned, from metadata flags/ids or a
    ``<@id>`` / ``@id`` token in the text."""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        if meta.get("mentioned") is True or meta.get("wasMentioned") is True:
            return True
        if agent_user_id:
            for f in ("mentions", "mentionIds", "mentionedUserIds", "mentionedUsers"):
                if agent_user_id in extract_mention_ids(meta.get(f)):
                    return True
    if not agent_user_id:
        return False
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return False
    return f"<@{agent_user_id}>" in content or f"@{agent_user_id}" in content


def resolve_require_mention(config: MochatConfig, session_id: str, group_id: str) -> bool:
    """Per-group/session mention requirement, falling back to the global flag."""
    groups = config.groups or {}
    for key in (group_id, session_id, "*"):
        if key and key in groups:
            return bool(groups[key].require_mention)
    return bool(config.mention.require_in_groups)


def build_buffered_body(entries: list[MochatBufferedEntry], is_group: bool) -> str:
    """Join buffered entries; in groups, prefix each with the sender label."""
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0].raw_body
    lines: list[str] = []
    for entry in entries:
        if not entry.raw_body:
            continue
        if is_group:
            label = entry.sender_name.strip() or entry.sender_username.strip() or entry.author
            if label:
                lines.append(f"{label}: {entry.raw_body}")
                continue
        lines.append(entry.raw_body)
    return "\n".join(lines).strip()


def build_entry(payload: dict[str, Any], timestamp: Any) -> MochatBufferedEntry:
    """Assemble a buffered entry from a message payload (author assumed valid)."""
    author_info = safe_dict(payload.get("authorInfo"))
    return MochatBufferedEntry(
        raw_body=normalize_content(payload.get("content")) or "[empty message]",
        author=str_field(payload, "author"),
        sender_name=str_field(author_info, "nickname", "email"),
        sender_username=str_field(author_info, "agentId"),
        timestamp=parse_timestamp(timestamp),
        message_id=str_field(payload, "messageId"),
        group_id=str_field(payload, "groupId"),
    )


def mention_gate(config: MochatConfig, target_kind: str, target_id: str, group_id: str) -> tuple[bool, bool]:
    """Return ``(require_mention, use_delay)`` for a panel message. The caller
    drops the message when ``require_mention and not mentioned and not use_delay``."""
    require_mention = target_kind == "panel" and bool(group_id) and resolve_require_mention(config, target_id, group_id)
    use_delay = target_kind == "panel" and config.reply_delay_mode == "non-mention"
    return require_mention, use_delay


def parse_timestamp(value: Any) -> int | None:
    """ISO-8601 string -> epoch milliseconds, or None."""
    if not isinstance(value, str):
        return None
    from raven.utils.text import parse_iso_ts_ms

    return parse_iso_ts_ms(value)
