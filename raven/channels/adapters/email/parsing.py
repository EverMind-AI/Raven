"""Pure parsing helpers for the email adapter.

IMAP date formatting, fetch-tuple extraction, RFC 2047 header decoding, body
text extraction, reply-subject derivation, and raw-bytes -> inbound-dict
assembly. No I/O — unit-tested directly. IMAP/SMTP transport lives in
:mod:`.mailbox`.
"""

from __future__ import annotations

import html
import re
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_imap_date(value: date) -> str:
    """Format a date for IMAP search (always English month abbreviations)."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


def extract_message_bytes(fetched: list[Any]) -> bytes | None:
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def extract_uid(fetched: list[Any]) -> str:
    for item in fetched:
        if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
            head = bytes(item[0]).decode("utf-8", errors="ignore")
            if m := re.search(r"UID\s+(\d+)", head):
                return m.group(1)
    return ""


def decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _part_text(part: Any) -> str | None:
    try:
        payload = part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        payload = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    return payload if isinstance(payload, str) else None


def extract_text_body(msg: Any) -> str:
    """Best-effort readable body: prefer text/plain, fall back to html."""
    if msg.is_multipart():
        plain_parts, html_parts = [], []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            payload = _part_text(part)
            if payload is None:
                continue
            if part.get_content_type() == "text/plain":
                plain_parts.append(payload)
            elif part.get_content_type() == "text/html":
                html_parts.append(payload)
        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            return html_to_text("\n\n".join(html_parts)).strip()
        return ""

    payload = _part_text(msg)
    if payload is None:
        return ""
    if msg.get_content_type() == "text/html":
        return html_to_text(payload).strip()
    return payload.strip()


def html_to_text(raw_html: str) -> str:
    text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def reply_subject(base_subject: str, prefix: str = "Re: ") -> str:
    subject = (base_subject or "").strip() or "Raven reply"
    if subject.lower().startswith("re:"):
        return subject
    return f"{prefix or 'Re: '}{subject}"


def parse_message(raw_bytes: bytes, max_body_chars: int, uid: str = "") -> dict[str, Any] | None:
    """Parse raw RFC 822 bytes into an inbound dict, or None if the sender is
    unreadable (a message with no usable From is dropped)."""
    parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
    if not sender:
        return None
    subject = decode_header_value(parsed.get("Subject", ""))
    date_value = parsed.get("Date", "")
    message_id = parsed.get("Message-ID", "").strip()
    body = (extract_text_body(parsed) or "(empty email body)")[:max_body_chars]
    return {
        "sender": sender,
        "subject": subject,
        "message_id": message_id,
        "content": (f"Email received.\nFrom: {sender}\nSubject: {subject}\nDate: {date_value}\n\n{body}"),
        "metadata": {
            "message_id": message_id,
            "subject": subject,
            "date": date_value,
            "sender_email": sender,
            "uid": uid,
        },
    }
