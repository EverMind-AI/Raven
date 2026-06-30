"""``understand_media`` — EverOS's multimodal understanding, as an agent tool.

Contributed via the manifest's ``[[plugin.contributes.tools]]`` slot and
registered into the agent's tool set at boot. The LLM calls it on demand to
read the contents of an attachment it can't natively consume — a PDF, an
audio clip, an Office doc, a scanned image — instead of Raven parsing
every attachment up front. The attachment paths are surfaced to the model in
the user message (see ``render.build_user_content``); the model passes them
back here.

The tool is deliberately thin: all parsing lives in
:func:`raven.plugin.memory.everos.multimodal.understand_files`, which reuses the exact
parser EverOS runs during memory ingest.
"""

from __future__ import annotations

import logging
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.memory.everos.multimodal import MultimodalUnavailable, understand_files

logger = logging.getLogger("raven.plugin.memory.everos")


class UnderstandMediaTool(Tool):
    """Read/understand attached files via EverOS's multimodal parser."""

    @property
    def name(self) -> str:
        return "understand_media"

    @property
    def description(self) -> str:
        return (
            "Read and understand the contents of one or more attached files "
            "or web pages that you cannot read directly — images (OCR / "
            "description), PDFs, audio (transcription), Office documents "
            "(docx/xlsx/pptx), and http(s) URLs (fetched and parsed by "
            "content type). Pass the file path(s) shown in the "
            "'[Attachment: ...]' notes of the user message, and/or http(s) "
            "URLs. Returns the extracted content as text. Video is not "
            "supported."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File path(s) to understand, exactly as shown in the "
                        "'[Attachment: <name> (path: <path>)]' notes, and/or "
                        "http(s) URL(s) to fetch and read."
                    ),
                },
            },
            "required": ["paths"],
        }

    async def execute(self, paths: Any = None, **_: Any) -> str:
        if isinstance(paths, str):
            paths = [paths]
        if not paths or not isinstance(paths, list):
            return "Error: 'paths' is required — a list of attachment file paths."

        try:
            results = await understand_files([str(p) for p in paths])
        except MultimodalUnavailable as e:
            return (
                "Error: multimodal understanding is unavailable. "
                f"{e}"
            )

        parts: list[str] = []
        for r in results:
            text = r.get("text")
            if text:
                parts.append(f"## {r['name']}\n{text}")
            else:
                parts.append(
                    f"## {r['name']}\n[could not understand: {r.get('error')}]"
                )
        return "\n\n".join(parts) if parts else "No files were provided."


def _multimodal_available() -> bool:
    """True if the optional ``everos[multimodal]`` parser extra is usable.

    Isolated as a seam so the registration gate is unit-testable without
    the heavy extra installed. Uses EverOS's own ``require_multimodal``
    availability check (raises when the parser extra is absent).
    """
    try:
        from everos.memory.extract.parser import require_multimodal

        require_multimodal()
    except Exception as e:
        logger.info(
            "understand_media not registered: multimodal parser extra "
            "unavailable (%s). Install with `pip install 'everos[multimodal]'`.",
            e,
        )
        return False
    return True


def make_understand_media_tool(ctx: Any) -> Tool | None:
    """Plugin tool-factory entry point (manifest ``contributes.tools``).

    Stateless — the tool reads its config (model/endpoint) from EverOS's
    own ``EVEROS_MULTIMODAL__*`` settings at call time, so ``ctx`` is
    accepted for signature symmetry but not used.

    Returns ``None`` when the optional ``everos[multimodal]`` parser extra
    isn't installed, so the host declines to register a tool that could
    never succeed. The LLM then never sees ``understand_media`` in
    environments without the extra, rather than discovering it's
    unavailable only after spending a tool call on it. Runtime LLM config
    (``EVEROS_MULTIMODAL__*``) is intentionally *not* gated here — that's a
    deploy-time setting handled by the tool's call-time graceful failure;
    only the static "is the parser installed" fact decides registration.
    """
    del ctx
    # Point EverOS at raven's ~/.everos/raven home before any everos import
    # resolves settings (the multimodal parser/LLM read EVEROS_* at call time).
    from raven.config.update_everos import configure_everos_env

    configure_everos_env()
    if not _multimodal_available():
        return None
    return UnderstandMediaTool()


__all__ = ["UnderstandMediaTool", "make_understand_media_tool"]
