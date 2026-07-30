"""Image preprocessing for tool results that carry pictures.

Vision endpoints reject or silently degrade images that are too large in any of
three independent ways — pixel dimensions, encoded byte size, and billed patch
count — so a picture has to clear all three before it can ride in a tool result.
The ceilings here are the strictest across the providers Raven targets, because
one payload gets built per turn and may be sent to any of them:

* 2000px per side — Anthropic downscales above 1568px and multi-image requests
  are observed to fail past ~2000px.
* 4.5MB of base64 — under the 5MB per-image cap that Bedrock and Vertex AI
  enforce (the direct Anthropic API allows 10MB, but the payload must satisfy
  the lowest common denominator).
* 1568 patch tokens — the standard-tier per-image ceiling. Anthropic's docs are
  explicit that *both* limits apply ("Images larger than either limit are
  downscaled"), so checking pixels alone lets an image through that still gets
  resized server-side, which makes any local token estimate wrong.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from raven.utils.helpers import estimate_image_tokens

# Formats every target accepts inline. Anything else (BMP, TIFF, HEIC, SVG) is
# converted to JPEG rather than rejected.
INLINE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

MAX_DIMENSION_PX = 2000
MAX_BASE64_BYTES = int(4.5 * 1024 * 1024)
MAX_IMAGE_TOKENS = 1568
# Below this a further downscale destroys the content instead of shrinking it,
# so we stop and let the caller report failure rather than send mush.
MIN_DIMENSION_PX = 200
# First value is the quality used for a plain format conversion; the rest are
# the ladder walked when the encoded result is still too large.
JPEG_QUALITY_LADDER = (85, 80, 70, 55, 40, 30)


class ImageTooLargeError(RuntimeError):
    """Raised when an image cannot be squeezed under the limits."""


def _target_size(width: int, height: int) -> tuple[int, int]:
    """Largest size within both the pixel cap and the patch-token cap.

    Two constraints, so take whichever bites harder — checking only the long
    edge leaves images that the server would still downscale.
    """
    scale = min(1.0, MAX_DIMENSION_PX / max(width, height))
    while True:
        w = max(1, int(width * scale))
        h = max(1, int(height * scale))
        if estimate_image_tokens(w, h, cap=10**9) <= MAX_IMAGE_TOKENS or min(w, h) <= MIN_DIMENSION_PX:
            return w, h
        scale *= 0.9


def prepare_image(data: bytes, mime: str) -> tuple[bytes, str, dict[str, Any]]:
    """Return ``(payload, mime, metadata)`` ready to inline as a data URI.

    ``metadata`` always reports the original and final geometry plus whether the
    image was altered, so the caller can tell the model what it is looking at —
    a resized screenshot with unreadable small text is worse than a resized
    screenshot the model *knows* was resized.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as probe:
        original_size = (probe.width, probe.height)

    meta: dict[str, Any] = {
        "original_width": original_size[0],
        "original_height": original_size[1],
        "width": original_size[0],
        "height": original_size[1],
        "resized": False,
        "recompressed": False,
        "source_mime": mime,
    }

    target = _target_size(*original_size)
    passthrough_ok = (
        mime in INLINE_MIME_TYPES and target == original_size and len(base64.b64encode(data)) <= MAX_BASE64_BYTES
    )
    if passthrough_ok:
        meta["tokens"] = estimate_image_tokens(*original_size)
        return data, mime, meta

    # Animated GIFs lose their frames on re-encode; only the first frame can be
    # shown, which is what the model would attend to anyway.
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        size = target
        for quality in JPEG_QUALITY_LADDER:
            frame = img if size == original_size else img.resize(size, Image.LANCZOS)
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=quality, optimize=True)
            payload = buf.getvalue()
            if len(base64.b64encode(payload)) <= MAX_BASE64_BYTES:
                meta.update(
                    width=size[0],
                    height=size[1],
                    resized=size != original_size,
                    recompressed=True,
                    quality=quality,
                    tokens=estimate_image_tokens(*size),
                )
                return payload, "image/jpeg", meta
            if min(size) <= MIN_DIMENSION_PX:
                break
            size = (max(1, int(size[0] * 0.8)), max(1, int(size[1] * 0.8)))

    raise ImageTooLargeError(
        f"image cannot be reduced below {MAX_BASE64_BYTES} bytes without dropping under {MIN_DIMENSION_PX}px"
    )


def to_data_uri(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def describe_image(path: Path, meta: dict[str, Any]) -> str:
    """Model-facing metadata line.

    Code-built, no model call. The path is included on purpose: it is the only
    thing that survives into session history (the base64 never does), so it is
    what lets the model ask to see the image again in a later turn.
    """
    bits = [f"[image: {path}]", f"{meta['width']}x{meta['height']}px", f"~{meta['tokens']} tokens"]
    if meta.get("resized"):
        bits.append(f"downscaled from {meta['original_width']}x{meta['original_height']}")
    if meta.get("recompressed") and meta.get("source_mime") not in (None, "image/jpeg"):
        bits.append(f"converted from {meta['source_mime']} to JPEG")
    return " | ".join(bits)
