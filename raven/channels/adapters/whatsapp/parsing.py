"""Pure parsing/decision helpers for the WhatsApp adapter.

Sender-id resolution across the phone-number / LID JID schemes, inbound content
assembly (voice placeholder + media tags), and the group mention gate. No I/O —
unit-tested directly. Bridge process/transport lives in :mod:`.bridge`.
"""

from __future__ import annotations

import mimetypes

_PHONE_SUFFIX = "@s.whatsapp.net"
_LID_SUFFIX = "@lid.whatsapp.net"
_VOICE_MARKER = "[Voice Message]"
_VOICE_PLACEHOLDER = "[Voice Message: Transcription not available for WhatsApp yet]"


def _local_part(jid: str) -> str:
    return jid.split("@")[0] if "@" in jid else jid


def classify_sender(pn: str, sender: str, lid_to_phone: dict[str, str]) -> tuple[str, str, str]:
    """Resolve ``(phone_id, lid_id, sender_id)`` from the bridge's pn/sender JIDs.

    The bridge's pn/sender fields don't map consistently across versions, so
    classify by JID suffix; a bare value (no suffix) is taken as a phone id.
    ``sender_id`` prefers the phone, then a cached phone for the LID, then the
    LID itself, then any local part.
    """
    raw_phone, raw_lid = pn or "", sender or ""
    local_phone, local_lid = _local_part(raw_phone), _local_part(raw_lid)

    phone_id = lid_id = ""
    for raw, local in ((raw_phone, local_phone), (raw_lid, local_lid)):
        if _PHONE_SUFFIX in raw:
            phone_id = local
        elif _LID_SUFFIX in raw:
            lid_id = local
        elif local and not phone_id:
            phone_id = local

    sender_id = phone_id or lid_to_phone.get(lid_id, "") or lid_id or local_phone or local_lid
    return phone_id, lid_id, sender_id


def should_skip_group(is_group: bool, group_policy: str, was_mentioned: bool) -> bool:
    """Mention-gated groups drop messages that don't address the bot."""
    return bool(is_group) and group_policy == "mention" and not was_mentioned


def build_inbound_content(content: str, media_paths: list[str]) -> str:
    """Assemble the text body: swap the voice marker for a placeholder and
    append an ``[image: ...]`` / ``[file: ...]`` tag per attachment."""
    if content == _VOICE_MARKER:
        content = _VOICE_PLACEHOLDER
    for path in media_paths:
        mime, _ = mimetypes.guess_type(path)
        kind = "image" if mime and mime.startswith("image/") else "file"
        tag = f"[{kind}: {path}]"
        content = f"{content}\n{tag}" if content else tag
    return content
