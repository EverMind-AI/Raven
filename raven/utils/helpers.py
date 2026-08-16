"""Utility functions for raven."""

import base64
import binascii
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

import tiktoken
from loguru import logger

# Workspace sync runs before the CLI decides logger.enable/disable("raven"),
# so an unscoped debug in this module would spam stderr through loguru's
# default sink on every first run. logger.enable("raven") lifts this for
# processes that configure logging.
logger.disable(__name__)


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ImageURL(TypedDict):
    url: str


class ImagePart(TypedDict):
    """An OpenAI-shaped image content part."""

    type: Literal["image_url"]
    image_url: ImageURL


class TextPart(TypedDict):
    """A text content part."""

    type: Literal["text"]
    text: str


# What Raven *produces*. Deliberately not used to type what Raven *reads*:
# inbound content legitimately contains parts this union does not model (an
# Anthropic part carrying cache_control, an MCP audio block, a provider-specific
# extension), and the pass-through code that forwards them unchanged would
# otherwise become a type error for doing the right thing. So read-side helpers
# keep taking ``Any`` and check shape at runtime.
ContentPart = TextPart | ImagePart


def image_block(data_uri: str) -> ImagePart:
    """The single place the image content-part shape is written.

    CI runs no type checker, so this annotation does not gate a merge -- but an
    editor language server does flag a mistyped key against a TypedDict, and the
    signature documents the shape that ``dict[str, Any]`` could not. Combined
    with being the only constructor, a wrong key ("imageURL") stops being a
    silent dropped picture in five places and becomes one line with a test.
    """
    return {"type": "image_url", "image_url": {"url": data_uri}}


def text_block(text: str) -> TextPart:
    """Counterpart to :func:`image_block` for the text half of a block list."""
    return {"type": "text", "text": text}


def is_image_part(part: Any) -> bool:
    """True for any image content part, inline or remote."""
    return isinstance(part, dict) and part.get("type") == "image_url"


def is_inline_image(part: Any) -> bool:
    """True for a content part carrying inline base64 image bytes.

    The distinction matters wherever the *payload size* is the concern -- token
    accounting, persistence, emergency shrinking. A remote URL is a reference and
    costs nothing to keep.
    """
    if not is_image_part(part):
        return False
    url = part.get("image_url") or {}
    url = url.get("url", "") if isinstance(url, dict) else ""
    return isinstance(url, str) and url.startswith("data:image/")


# Vision models bill images by patch area, not by the size of the transport
# encoding. Counting a data URI as text charges ~350x the real cost (a 1000x1000
# JPEG is ~1.3k image tokens but ~460k base64 characters), which starves the
# history budget and can trip emergency shrinking on a prompt that would have
# fit comfortably.
_IMAGE_PATCH_PX = 28
_IMAGE_TOKEN_CAP = 1568
_IMAGE_HEADER_BYTES = 4096


def _image_pixel_size(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions from an image header, or None if not derivable.

    Header-only parsing on purpose: the caller has a whole image in memory
    already and this runs on every budget probe, so decoding pixels (or pulling
    in an imaging library) would cost far more than the estimate is worth.
    WebP is deliberately absent -- its three chunk variants need more parsing
    than the fallback is worth.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:3] == b"\xff\xd8\xff":
        # Walk JPEG segments to the first frame header; SOF carries the size.
        i = 2
        while i + 1 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            # 0xFF is a fill byte, legal in any run before a marker.
            if marker == 0xFF:
                i += 1
                continue
            # Standalone markers carry no length field, so the generic
            # "skip the segment" step below would read their *payload* as a
            # length and desync the walk. TEM (0x01) and RST0-7 (0xD0-0xD7)
            # are the ones that can precede SOF.
            if marker == 0x01 or 0xD0 <= marker <= 0xD9:
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 > len(data):
                    return None
                return (
                    int.from_bytes(data[i + 7 : i + 9], "big"),
                    int.from_bytes(data[i + 5 : i + 7], "big"),
                )
            if i + 4 > len(data):
                return None
            length = int.from_bytes(data[i + 2 : i + 4], "big")
            if length < 2:
                return None  # malformed: a segment length includes its own 2 bytes
            i += 2 + length
    return None


def estimate_image_tokens(width: int, height: int, cap: int = _IMAGE_TOKEN_CAP) -> int:
    """Image tokens for a ``width`` x ``height`` image, same order of magnitude
    across vendors and biased high.

    Anthropic's own formula (28x28 patches, capped at 1568 for the standard
    tier). Exact for Claude; ~10% high for OpenAI's 512px tiles; ~2.25x high for
    Doubao 2.x, which moved to 42x42 patches. Over-estimating is the safe
    direction for a budget guard -- under-estimating overflows the context.
    """
    if width <= 0 or height <= 0:
        return cap
    patches = math.ceil(width / _IMAGE_PATCH_PX) * math.ceil(height / _IMAGE_PATCH_PX)
    return min(patches, cap)


def estimate_content_part_tokens(part: Any) -> int | None:
    """Token estimate for a non-text multimodal content part, or None when the
    part carries no image and should fall through to text accounting."""
    if not is_image_part(part):
        return None
    if not is_inline_image(part):
        # A remote URL costs the model an image either way, but its dimensions
        # are unknowable without fetching it. Charge the ceiling.
        return _IMAGE_TOKEN_CAP
    _, _, payload = part["image_url"]["url"].partition(",")
    try:
        head = base64.b64decode(payload[:_IMAGE_HEADER_BYTES], validate=False)
    except (binascii.Error, ValueError):
        return _IMAGE_TOKEN_CAP
    size = _image_pixel_size(head)
    return estimate_image_tokens(*size) if size else _IMAGE_TOKEN_CAP


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    parts: list[str] = []
    extra_tokens = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part.get("text", "")
                    if txt:
                        parts.append(txt)
                elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                    extra_tokens += image_tokens
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
        elif content is not None:
            parts.append(json.dumps(content, ensure_ascii=False))

        for key in ("name", "tool_call_id"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        if msg.get("tool_calls"):
            parts.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            parts.append(reasoning)
        if msg.get("thinking_blocks"):
            parts.append(json.dumps(msg["thinking_blocks"], ensure_ascii=False))

    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return extra_tokens
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        text_tokens = len(enc.encode(payload))
    except Exception:
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    extra_tokens = 0
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            elif (image_tokens := estimate_content_part_tokens(part)) is not None:
                extra_tokens += image_tokens
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append(reasoning)
    if message.get("thinking_blocks"):
        parts.append(json.dumps(message["thinking_blocks"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return max(1, extra_tokens)
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        text_tokens = len(enc.encode(payload))
    except Exception:
        text_tokens = len(payload) // 4
    return max(1, text_tokens + extra_tokens)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("raven") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []
    existed = 0

    def _write(src, dest: Path):
        nonlocal existed
        if dest.exists():
            existed += 1
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    def _migrate(src: Path, dest: Path):
        """One-shot copy of legacy content to the L4 path. No-op when the
        source is missing or the destination already exists — safe to
        re-run on every workspace sync.  Reads as binary then decodes
        with UTF-8 (replace) so legacy files written under a non-UTF-8
        Windows code page still migrate without crashing."""
        if not src.is_file() or dest.exists():
            return
        try:
            raw = src.read_bytes()
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        added.append(f"{dest.relative_to(workspace)} (migrated from {src.relative_to(workspace)})")

    # Step 1 — migrate legacy workspace files into the L4 layout. Each
    # rule fires only when the legacy file exists and the L4 target is
    # still missing, so user edits made directly to L4 paths win.
    _migrate(workspace / "memory" / "MEMORY.md", workspace / "user_memory" / "profile" / "user.md")
    _migrate(workspace / "memory" / "HISTORY.md", workspace / "user_memory" / "episodic" / "episodes.md")
    _migrate(workspace / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
    _migrate(workspace / "AGENTS.md", workspace / "agent_memory" / "profile" / "agent.md")
    _migrate(workspace / "USER.md", workspace / "user_memory" / "profile" / "user.md")
    # feat/auto attention + behaviors content lived at workspace root.
    # Sentinel rewrites attention.md from its own producers each tick, so
    # the migrated file mostly serves as a head-start for the next refresh.
    _migrate(workspace / "ATTENTION.md", workspace / "user_memory" / "attention.md")
    _migrate(workspace / "BEHAVIORS.md", workspace / "user_memory" / "behaviors.md")
    _migrate(workspace / "BEHAVIOR.md", workspace / "user_memory" / "behaviors.md")

    # Step 2 — fall back to bundled templates for anything still missing.
    # L4 pillar files first; root-level files (TOOLS / HEARTBEAT) stay put.
    _write(tpl / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
    _write(tpl / "AGENTS.md", workspace / "agent_memory" / "profile" / "agent.md")
    _write(tpl / "USER.md", workspace / "user_memory" / "profile" / "user.md")
    _write(None, workspace / "user_memory" / "episodic" / "episodes.md")
    # Files L4 specifies but the legacy layout had no source for —
    # empty stubs; populated later by Sentinel / eval engine.
    _write(None, workspace / "agent_memory" / "procedural" / "skills.md")
    _write(None, workspace / "agent_memory" / "procedural" / "case.md")
    _write(None, workspace / "user_memory" / "attention.md")
    _write(None, workspace / "user_memory" / "behaviors.md")
    _write(tpl / "TOOLS.md", workspace / "TOOLS.md")
    _write(tpl / "HEARTBEAT.md", workspace / "HEARTBEAT.md")
    (workspace / "skills").mkdir(exist_ok=True)

    if added:
        for name in added:
            logger.debug("workspace sync: created {}", name)
    if added and not silent:
        from rich.console import Console

        _c = Console(stderr=True)
        label = "Initialized workspace" if existed == 0 else "Updated workspace templates"
        _c.print(f"  [dim]{label} ({len(added)} files)[/dim]")
    return added
