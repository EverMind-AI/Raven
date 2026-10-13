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

from pathlib import Path
from typing import Any

from raven.contracts.tool import ContentPart

# The preparation half moved to `raven.utils.images`, beside the content-part
# builders it was always calling: a knowledge base has to prepare an image too,
# and reaching into `raven.agent` for it would have put the two packages in an
# import cycle. Re-exported because the tool modules and the context engine
# address them here.
from raven.utils.images import (
    INLINE_MIME_TYPES,
    JPEG_QUALITY_LADDER,
    MAX_BASE64_BYTES,
    MAX_DIMENSION_PX,
    MAX_IMAGE_TOKENS,
    MIN_DIMENSION_PX,
    ImageTooLargeError,
    image_block,
    is_image_part,
    prepare_image,
    text_block,
    to_data_uri,
)

__all__ = [
    "INLINE_MIME_TYPES",
    "JPEG_QUALITY_LADDER",
    "MAX_BASE64_BYTES",
    "MAX_DIMENSION_PX",
    "MAX_IMAGE_TOKENS",
    "MIN_DIMENSION_PX",
    "ImageTooLargeError",
    "blocks_from_mcp_content",
    "describe_image",
    "image_block",
    "is_image_part",
    "prepare_image",
    "text_block",
    "to_data_uri",
    "to_data_uri_from_b64",
]


def blocks_from_mcp_content(content: list[Any]) -> tuple[str, list[ContentPart]]:
    """Convert MCP ``CallToolResult.content`` to ``(model_text, blocks)``.

    MCP is the one place a *typed* content model already exists in this codebase
    -- ``mcp.types`` ships runtime-validated pydantic models -- but its image
    shape (``{type:"image", data, mimeType}``) is not the OpenAI wire shape, so it
    has to be translated rather than passed through.

    ``blocks`` is empty when the result is text-only, so a text MCP tool keeps
    returning a plain string exactly as before.
    """
    from mcp import types

    texts: list[str] = []
    blocks: list[ContentPart] = []
    for block in content:
        if isinstance(block, types.TextContent):
            texts.append(block.text)
            blocks.append(text_block(block.text))
        elif isinstance(block, types.ImageContent):
            note = f"[image from MCP tool: {block.mimeType}]"
            texts.append(note)
            blocks.append(text_block(note))
            blocks.append(image_block(to_data_uri_from_b64(block.data, block.mimeType)))
        else:
            # Audio, resource links, embedded resources. Raven has no input path
            # for these, but they must never be str()'d -- a pydantic repr would
            # dump the whole base64 payload into the prompt as prose.
            label = getattr(block, "type", type(block).__name__)
            mime = getattr(block, "mimeType", None)
            note = f"[unsupported MCP content: {label}{f' ({mime})' if mime else ''}]"
            texts.append(note)
            blocks.append(text_block(note))

    has_image = any(is_image_part(b) for b in blocks)
    return "\n".join(texts), (blocks if has_image else [])


def to_data_uri_from_b64(b64: str, mime: str) -> str:
    """Wrap an already-base64 payload (MCP hands them over pre-encoded)."""
    return f"data:{mime};base64,{b64}"


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
