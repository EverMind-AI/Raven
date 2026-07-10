"""EverOS multimodal parsing — shared helper behind the ``understand_media`` tool.

Wraps ``everos.memory.extract.parser`` (the same parser EverOS runs during
memory ingest) so the tool layer stays thin and there's a single place that
turns a file into text via EverOS's independent vision/audio model.

The ``everos`` imports are deferred to call time — they pull the optional
``everos[multimodal]`` extra — so importing this module is cheap and safe even
when the extra isn't installed. Configuration (model / endpoint) comes from
EverOS's own ``EVEROS_MULTIMODAL__*`` settings, not from Raven config.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("raven.plugin.memory.everos")

# Extension → modality hint. Only used for the human-readable provenance
# tag; ``everalgo.parser`` itself dispatches by extension/MIME.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".svg"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".amr", ".aiff", ".aac", ".ogg", ".flac"}
_DOC_EXTS = {
    ".pdf",
    ".html",
    ".htm",
    ".eml",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".pages",
    ".key",
    ".numbers",
}


class MultimodalUnavailableError(RuntimeError):
    """EverOS's multimodal parser/LLM isn't installed or configured.

    Raised once for the whole call (not per file) so the tool can surface
    a single actionable message instead of repeating it per attachment.
    """


def _modality_for(ext: str) -> str:
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _DOC_EXTS:
        return "document"
    return "file"


def _content_item(path: Path) -> dict[str, Any]:
    """Build a base64-backed ContentItem dict for ``everalgo.parser``.

    base64 (not ``file://``) keeps the payload self-contained, sidestepping
    a deployment's ``EVEROS_MULTIMODAL__FILE_URI_ALLOW_DIRS`` allowlist.
    """
    raw = path.read_bytes()
    return {
        "type": _modality_for(path.suffix.lower()),
        "base64": base64.b64encode(raw).decode(),
        "ext": path.suffix.lower(),
        "mime": mimetypes.guess_type(str(path))[0] or "",
        "name": path.name,
    }


def _is_http_url(ref: str) -> bool:
    """True if ``ref`` is an ``http``/``https`` URL (vs a local file path)."""
    return urlparse(ref).scheme in ("http", "https")


def _content_item_for(ref: str) -> dict[str, Any]:
    """Build a ContentItem for a local file path or an ``http(s)`` URL.

    URLs become ``uri``-backed items — everalgo fetches them and dispatches
    by the response Content-Type (HTML / PDF / image / ...), so the parser
    stays filesystem-stateless. Local paths become base64-backed items keyed
    by extension. Isolated as a seam so input routing is unit-testable
    without a live LLM.

    Raises:
        FileNotFoundError: ``ref`` is a local path that isn't a regular file.
        OSError: the local file exists but can't be read.
    """
    if _is_http_url(ref):
        return {"type": "url", "uri": ref, "name": ref}
    path = Path(ref).expanduser()
    if not path.is_file():
        raise FileNotFoundError(ref)
    return _content_item(path)


async def understand_files(paths: list[str]) -> list[dict[str, Any]]:
    """Parse each input to text via EverOS. Returns one dict per input:
    ``{"path", "name", "text"}`` on success or ``{"path", "name", "error"}``.

    Each input is either a local file path or an ``http(s)`` URL — URLs are
    fetched by everalgo and dispatched by the response Content-Type. Inputs
    are parsed independently so one unsupported/failed item (e.g. a video,
    which EverOS doesn't parse yet) doesn't abort the rest.

    Raises:
        MultimodalUnavailableError: EverOS multimodal extra not installed, or the
            multimodal LLM isn't configured (``EVEROS_MULTIMODAL__*``).
    """
    try:
        from everos.component.llm.client import (
            LLMNotConfiguredError,
            get_multimodal_llm_client,
        )
        from everos.core.errors import MultimodalNotEnabledError, UnsupportedModalityError
        from everos.memory.extract.parser import (
            enrich_content_items,
            require_multimodal,
        )
    except ImportError as e:
        raise MultimodalUnavailableError(
            f"EverOS multimodal parser not installed (`pip install 'everos[multimodal]'`): {e}"
        ) from e

    try:
        require_multimodal()
        llm = get_multimodal_llm_client()
    except (MultimodalNotEnabledError, LLMNotConfiguredError) as e:
        raise MultimodalUnavailableError(str(e)) from e

    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            item = _content_item_for(p)
        except FileNotFoundError:
            out.append({"path": p, "name": Path(p).name or p, "error": "file not found"})
            continue
        except OSError as e:
            out.append(
                {
                    "path": p,
                    "name": Path(p).name or p,
                    "error": f"cannot read file: {e}",
                }
            )
            continue
        name = item.get("name") or p
        try:
            # Parse one at a time so any failure — an UnsupportedModalityError
            # (unsupported modality / missing system dep), a native-lib error
            # (e.g. SVG needing libcairo), or anything unexpected — is isolated
            # to this item and never aborts the rest of the batch. The broad
            # catch is intentional: this is a best-effort per-item enrichment,
            # not a place to surface bugs.
            await enrich_content_items([item], llm=llm)
        except UnsupportedModalityError as e:
            out.append({"path": p, "name": name, "error": str(e)})
            continue
        except Exception as e:  # noqa: BLE001 — per-item isolation is the contract
            logger.warning("understand_media: failed to parse %r (%s)", name, e)
            out.append(
                {
                    "path": p,
                    "name": name,
                    "error": str(e) or type(e).__name__,
                }
            )
            continue
        parsed = item.get("parsed_content")
        if parsed:
            out.append({"path": p, "name": name, "text": parsed})
        else:
            out.append(
                {
                    "path": p,
                    "name": name,
                    "error": item.get("parse_error") or "could not be understood",
                }
            )
    return out


__all__ = ["MultimodalUnavailableError", "understand_files"]
