"""Pure parsing/decision helpers for the DingTalk adapter.

Inbound message parsing (text + media-request extraction across picture /
file / richText), media-ref type/name guessing, and chat-id routing — all
I/O-free and unit-tested directly. The Stream client, token refresh and
httpx upload/download live in :mod:`.channel`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

_DEFAULT_UPLOAD_NAMES = {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}


def is_http_url(value: str) -> bool:
    return urlparse(value).scheme in ("http", "https")


def guess_upload_type(media_ref: str) -> str:
    """Map a media ref's extension to a DingTalk upload type."""
    ext = Path(urlparse(media_ref).path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "voice"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def guess_filename(media_ref: str, upload_type: str) -> str:
    name = os.path.basename(urlparse(media_ref).path)
    return name or _DEFAULT_UPLOAD_NAMES.get(upload_type, "file.bin")


@dataclass
class MediaRequest:
    """One attachment to fetch: its download code, target name, and the text
    placeholder to use when the message carries no text of its own."""

    download_code: str
    filename: str
    placeholder: str


@dataclass
class ParsedInbound:
    text: str
    media: list[MediaRequest] = field(default_factory=list)
    sender_id: str | None = None
    sender_uid: str = "unknown"
    sender_name: str = "Unknown"
    conversation_type: str | None = None
    conversation_id: str | None = None


def _base_text(chatbot_msg: Any, raw_data: dict[str, Any]) -> str:
    """Text content with recognition (voice) and raw-dict fallbacks."""
    text_obj = getattr(chatbot_msg, "text", None)
    if text_obj and getattr(text_obj, "content", None):
        return text_obj.content.strip()
    extensions = getattr(chatbot_msg, "extensions", None) or {}
    recognition = (extensions.get("content") or {}).get("recognition")
    if recognition:
        return recognition.strip()
    return ((raw_data.get("text") or {}).get("content") or "").strip()


def parse_inbound(chatbot_msg: Any, raw_data: dict[str, Any]) -> ParsedInbound:
    """Extract text + media requests + sender/conversation routing from a
    DingTalk ChatbotMessage. Performs no I/O — media is returned as fetch
    requests for the channel to download."""
    text = _base_text(chatbot_msg, raw_data)
    media: list[MediaRequest] = []
    message_type = getattr(chatbot_msg, "message_type", None)

    if message_type == "picture":
        image = getattr(chatbot_msg, "image_content", None)
        if code := (getattr(image, "download_code", None) if image else None):
            media.append(MediaRequest(code, "image.jpg", "[Image]"))
    elif message_type == "file":
        raw = raw_data.get("content") or {}
        if code := (raw.get("downloadCode") or raw_data.get("downloadCode")):
            fname = raw.get("fileName") or raw_data.get("fileName") or "file"
            media.append(MediaRequest(code, fname, "[File]"))
    elif message_type == "richText":
        rich = getattr(chatbot_msg, "rich_text_content", None)
        items = getattr(rich, "rich_text_list", None) if rich else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                if t := item.get("text", "").strip():
                    text = f"{text} {t}".strip() if text else t
            elif code := item.get("downloadCode"):
                media.append(MediaRequest(code, item.get("fileName") or "file", "[File]"))

    sender_id = getattr(chatbot_msg, "sender_staff_id", None) or getattr(chatbot_msg, "sender_id", None)
    return ParsedInbound(
        text=text,
        media=media,
        sender_id=sender_id,
        sender_uid=sender_id or "unknown",
        sender_name=getattr(chatbot_msg, "sender_nick", None) or "Unknown",
        conversation_type=raw_data.get("conversationType"),
        conversation_id=raw_data.get("conversationId") or raw_data.get("openConversationId"),
    )


def resolve_chat_id(conversation_type: str | None, conversation_id: str | None, sender_id: str | None) -> str | None:
    """Group chats route by an `group:`-prefixed conversation id; 1:1 by sender."""
    if conversation_type == "2" and conversation_id:
        return f"group:{conversation_id}"
    return sender_id


def append_files_footer(text: str, file_paths: list[str]) -> str:
    if not file_paths:
        return text
    return text + "\n\nReceived files:\n" + "\n".join(f"- {p}" for p in file_paths)
