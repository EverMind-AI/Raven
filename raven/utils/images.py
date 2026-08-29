"""Image content parts: sniffing, building, recognising and pricing them.

Vision models bill an image by patch area, not by the size of its transport
encoding; the estimators here keep a data URI from being counted as text.
"""

import base64
import binascii
import math
from typing import Any

from raven.contracts.tool import ImagePart, TextPart


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


def image_pixel_size(data: bytes) -> tuple[int, int] | None:
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
    size = image_pixel_size(head)
    return estimate_image_tokens(*size) if size else _IMAGE_TOKEN_CAP


__all__ = [
    "detect_image_mime",
    "estimate_content_part_tokens",
    "estimate_image_tokens",
    "image_block",
    "is_image_part",
    "is_inline_image",
    "text_block",
]
