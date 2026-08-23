"""Pure parsing/decision helpers for the Slack adapter.

Markdown -> Slack mrkdwn conversion (including table flattening), bot-mention
stripping, and the sender/respond/dedup decisions. No I/O — unit-tested
directly. The Socket Mode SDK orchestration lives in :mod:`.channel`.
"""

from __future__ import annotations

import re
from typing import Any

from slackify_markdown import slackify_markdown

_TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")


def _convert_table(match: re.Match) -> str:
    """Flatten a Markdown table into a Slack-readable bullet list."""
    lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return match.group(0)
    headers = [h.strip() for h in lines[0].strip("|").split("|")]
    start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
    rows: list[str] = []
    for line in lines[start:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        cells = (cells + [""] * len(headers))[: len(headers)]
        parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
        if parts:
            rows.append(" · ".join(parts))
    return "\n".join(rows)


def _fixup_mrkdwn(text: str) -> str:
    """Repair markdown artifacts that slackify_markdown leaves behind, keeping
    code spans/blocks verbatim."""
    code_blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = _CODE_FENCE_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    text = _LEFTOVER_BOLD_RE.sub(r"*\1*", text)
    text = _LEFTOVER_HEADER_RE.sub(r"*\1*", text)
    text = _BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    return text


def to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn, flattening tables first."""
    if not text:
        return ""
    text = _TABLE_RE.sub(_convert_table, text)
    return _fixup_mrkdwn(slackify_markdown(text))


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the leading ``<@bot>`` mention token from inbound text."""
    if not text or not bot_user_id:
        return text
    return re.sub(rf"<@{re.escape(bot_user_id)}>\s*", "", text).strip()


def sender_permitted(config: Any, sender_id: str, chat_id: str, channel_type: str) -> bool:
    """Channel-type-aware permission: DMs gate on dm.enabled/policy, group and
    channel messages on group_policy allowlist."""
    if channel_type == "im":
        if not config.dm.enabled:
            return False
        if config.dm.policy == "allowlist":
            return sender_id in config.dm.allow_from
        return True
    if config.group_policy == "allowlist":
        return chat_id in config.group_allow_from
    return True


def should_respond_in_channel(config: Any, event_type: str, text: str, chat_id: str, bot_user_id: str | None) -> bool:
    """Whether a non-DM message warrants a reply under the group policy."""
    policy = config.group_policy
    if policy == "open":
        return True
    if policy == "mention":
        if event_type == "app_mention":
            return True
        return bot_user_id is not None and f"<@{bot_user_id}>" in text
    if policy == "allowlist":
        return chat_id in config.group_allow_from
    return False


def is_duplicate_mention(event_type: str, text: str, bot_user_id: str | None) -> bool:
    """Slack delivers both `message` and `app_mention` for a channel mention;
    treat the `message` copy as the duplicate to drop."""
    return event_type == "message" and bool(bot_user_id) and f"<@{bot_user_id}>" in text
