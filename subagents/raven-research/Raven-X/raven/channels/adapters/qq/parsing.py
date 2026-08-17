"""Pure routing/content helpers for the QQ adapter.

Resolve the chat/user route and chat-type from a botpy message, and normalize
its text. No I/O — unit-tested directly. The botpy SDK orchestration lives in
:mod:`.channel`.
"""

from __future__ import annotations

from typing import Any


def clean_content(data: Any) -> str:
    return (getattr(data, "content", None) or "").strip()


def resolve_route(data: Any, is_group: bool) -> tuple[str, str, str]:
    """Return ``(chat_id, user_id, chat_type)`` for an inbound message.

    Group messages route by group openid (sender = member openid). Guild
    direct messages (botpy ``DirectMessage``) carry a ``guild_id`` — the DM
    session id that replies must go back through (``post_dms``), distinct from
    QQ C2C. Plain C2C messages route by the author's id, falling back to
    user_openid.
    """
    if is_group:
        return data.group_openid, data.author.member_openid, "group"
    user_id = str(getattr(data.author, "id", None) or getattr(data.author, "user_openid", "unknown"))
    if guild_id := getattr(data, "guild_id", None):
        return str(guild_id), user_id, "guild_dm"
    return user_id, user_id, "c2c"
