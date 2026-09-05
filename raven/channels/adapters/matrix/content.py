"""Pure content helpers for the Matrix adapter.

Markdown->HTML rendering, m.room.message payload construction, event-field
extraction, and room/mention decisions — all I/O-free and free of any nio
client, so they unit-test directly. The sync/E2EE/upload machinery lives in
:mod:`.channel`.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import nh3
from mistune import create_markdown

from raven.utils.helpers import safe_filename

HTML_FORMAT = "org.matrix.custom.html"
DEFAULT_ATTACH_NAME = "attachment"

_MSGTYPE_TO_KIND = {"m.image": "image", "m.audio": "audio", "m.video": "video", "m.file": "file"}
_KIND_TO_MSGTYPE = {"image": "m.image", "audio": "m.audio", "video": "m.video"}

# ── markdown -> sanitized HTML ────────────────────────────────────────

_MARKDOWN = create_markdown(
    escape=True,
    plugins=["table", "strikethrough", "url", "superscript", "subscript"],
)

_ALLOWED_TAGS = {
    "p",
    "a",
    "strong",
    "em",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "br",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "caption",
    "sup",
    "sub",
    "img",
}
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href"},
    "code": {"class"},
    "ol": {"start"},
    "img": {"src", "alt", "title", "width", "height"},
}
_ALLOWED_SCHEMES = {"https", "http", "matrix", "mailto", "mxc"}


def _attr_filter(tag: str, attr: str, value: str) -> str | None:
    """Constrain attribute values to a Matrix-safe subset."""
    if tag == "a" and attr == "href":
        ok = value.lower().startswith(("https://", "http://", "matrix:", "mailto:"))
        return value if ok else None
    if tag == "img" and attr == "src":
        return value if value.lower().startswith("mxc://") else None
    if tag == "code" and attr == "class":
        langs = [c for c in value.split() if c.startswith("language-") and not c.startswith("language-_")]
        return " ".join(langs) if langs else None
    return value


_CLEANER = nh3.Cleaner(
    tags=_ALLOWED_TAGS,
    attributes=_ALLOWED_ATTRS,
    attribute_filter=_attr_filter,
    url_schemes=_ALLOWED_SCHEMES,
    strip_comments=True,
    link_rel="noopener noreferrer",
)


def render_markdown_html(text: str) -> str | None:
    """Render markdown to sanitized HTML, or None when the result is plain text."""
    try:
        formatted = _CLEANER.clean(_MARKDOWN(text)).strip()
    except Exception:
        return None
    if not formatted:
        return None
    # A bare <p>plain text</p> carries no formatting worth a formatted_body.
    if formatted.startswith("<p>") and formatted.endswith("</p>"):
        inner = formatted[3:-4]
        if "<" not in inner and ">" not in inner:
            return None
    return formatted


def build_text_content(text: str) -> dict[str, Any]:
    """Build an m.text payload, attaching an HTML formatted_body when useful."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": text, "m.mentions": {}}
    if html := render_markdown_html(text):
        content["format"] = HTML_FORMAT
        content["formatted_body"] = html
    return content


def build_attachment_content(
    *,
    filename: str,
    mime: str,
    size_bytes: int,
    mxc_url: str,
    encryption_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the m.image/m.audio/m.video/m.file payload for an uploaded file."""
    msgtype = _KIND_TO_MSGTYPE.get(mime.split("/")[0], "m.file")
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": filename,
        "filename": filename,
        "info": {"mimetype": mime, "size": size_bytes},
        "m.mentions": {},
    }
    if encryption_info:
        content["file"] = {**encryption_info, "url": mxc_url}
    else:
        content["url"] = mxc_url
    return content


def build_thread_relates_to(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Construct an m.thread m.relates_to block from carried thread metadata."""
    if not metadata:
        return None
    root_id = metadata.get("thread_root_event_id")
    if not isinstance(root_id, str) or not root_id:
        return None
    reply_to = metadata.get("thread_reply_to_event_id") or metadata.get("event_id")
    if not isinstance(reply_to, str) or not reply_to:
        return None
    return {
        "rel_type": "m.thread",
        "event_id": root_id,
        "m.in_reply_to": {"event_id": reply_to},
        "is_falling_back": True,
    }


# ── event field extraction ────────────────────────────────────────────


def event_content(event: Any) -> dict[str, Any]:
    """The event's `content` dict from its raw source, or {}."""
    source = getattr(event, "source", None)
    if not isinstance(source, dict):
        return {}
    content = source.get("content")
    return content if isinstance(content, dict) else {}


def thread_root_id(event: Any) -> str | None:
    relates_to = event_content(event).get("m.relates_to")
    if not isinstance(relates_to, dict) or relates_to.get("rel_type") != "m.thread":
        return None
    root_id = relates_to.get("event_id")
    return root_id if isinstance(root_id, str) and root_id else None


def thread_metadata(event: Any) -> dict[str, str] | None:
    if not (root_id := thread_root_id(event)):
        return None
    meta = {"thread_root_event_id": root_id}
    if isinstance(reply_to := getattr(event, "event_id", None), str) and reply_to:
        meta["thread_reply_to_event_id"] = reply_to
    return meta


def attachment_kind(event: Any) -> str:
    return _MSGTYPE_TO_KIND.get(event_content(event).get("msgtype"), "file")


def is_encrypted_media(event: Any) -> bool:
    return (
        isinstance(getattr(event, "key", None), dict)
        and isinstance(getattr(event, "hashes", None), dict)
        and isinstance(getattr(event, "iv", None), str)
    )


def declared_size_bytes(event: Any) -> int | None:
    info = event_content(event).get("info")
    size = info.get("size") if isinstance(info, dict) else None
    return size if isinstance(size, int) and size >= 0 else None


def media_mime(event: Any) -> str | None:
    info = event_content(event).get("info")
    if isinstance(info, dict) and isinstance(m := info.get("mimetype"), str) and m:
        return m
    m = getattr(event, "mimetype", None)
    return m if isinstance(m, str) and m else None


def media_filename(event: Any, kind: str) -> str:
    body = getattr(event, "body", None)
    if isinstance(body, str) and body.strip():
        if candidate := safe_filename(Path(body).name):
            return candidate
    return DEFAULT_ATTACH_NAME if kind == "file" else kind


def attachment_path(media_dir: Path, event: Any, kind: str, filename: str, mime: str | None) -> Path:
    """Collision-safe on-disk path: <event_id>_<stem><suffix>, suffix guessed
    from mime when the filename lacks one."""
    safe = safe_filename(Path(filename).name) or DEFAULT_ATTACH_NAME
    suffix = Path(safe).suffix
    if not suffix and mime:
        if guessed := mimetypes.guess_extension(mime, strict=False):
            safe, suffix = f"{safe}{guessed}", guessed
    stem = (Path(safe).stem or kind)[:72]
    suffix = suffix[:16]
    event_id = safe_filename(str(getattr(event, "event_id", "") or "evt").lstrip("$"))
    prefix = (event_id[:24] or "evt").strip("_")
    return media_dir / f"{prefix}_{stem}{suffix}"


# ── room / mention decisions ──────────────────────────────────────────


def is_direct_room(room: Any) -> bool:
    count = getattr(room, "member_count", None)
    return isinstance(count, int) and count <= 2


def is_bot_mentioned(event: Any, user_id: str, allow_room_mentions: bool) -> bool:
    """Whether the bot is named in the event's m.mentions payload."""
    mentions = event_content(event).get("m.mentions")
    if not isinstance(mentions, dict):
        return False
    user_ids = mentions.get("user_ids")
    if isinstance(user_ids, list) and user_id in user_ids:
        return True
    return bool(allow_room_mentions and mentions.get("room") is True)


# ── outbound media path handling ──────────────────────────────────────


def collect_media_candidates(media: list[str]) -> list[Path]:
    """Resolve and de-duplicate outbound attachment paths, order-preserving."""
    seen: set[str] = set()
    candidates: list[Path] = []
    for raw in media:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates
