"""Generate a slide image and make it a figure of this deck immediately."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from raven.contracts.tool import Tool
from raven_ppt.contracts import Project
from raven_ppt.services.ingest import ingest_materials, sources
from raven_ppt.tools import _return

if TYPE_CHECKING:
    from raven.config.schema import MediaToolConfig

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_MODEL = "openai/gpt-image-2"
_COMPATIBLE_MODEL = "gpt-image-2"
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


class PptGenerateImageTool(Tool):
    name = "ppt_generate_image"
    description = (
        "Generate a new raster visual for a page with GPT Image 2. Use this for an illustration or visual "
        "that does not already exist; use ppt_image_search and ppt_fetch for a real logo, product "
        "screen, published plot or other existing evidence. The generated PNG is added to this deck's "
        "sources, ingested immediately, and returned with its figure id so the build can place it."
    )
    timeout_seconds = 360.0

    def __init__(
        self,
        workspace: Path,
        config: "MediaToolConfig | None" = None,
        *,
        proxy: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.proxy = proxy

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project"},
                "prompt": {
                    "type": "string",
                    "description": (
                        "a background or decorative visual for the page, including composition and style. "
                        "Do not use this for a product screenshot, logo, published chart, paper figure or "
                        "factual architecture. Do not put prose, labels or numbers into the image; slide "
                        "text stays editable in PowerPoint"
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": "short file name for the generated PNG, without a directory",
                },
                "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "default": "high"},
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "4:3", "3:2", "1:1", "3:4", "9:16"],
                    "default": "16:9",
                },
            },
            "required": ["project", "prompt", "filename"],
        }

    @property
    def api_key(self) -> str:
        import os

        configured = getattr(self.config, "api_key", "") if self.config else ""
        return configured or os.environ.get("OPENROUTER_API_KEY", "")

    @property
    def api_base(self) -> str:
        configured = getattr(self.config, "api_base", "") if self.config else ""
        return (configured or _OPENROUTER_BASE).rstrip("/")

    @property
    def model(self) -> str:
        configured = getattr(self.config, "model", "") if self.config else ""
        if configured:
            return configured
        return _OPENROUTER_MODEL if "openrouter.ai" in self.api_base else _COMPATIBLE_MODEL

    async def execute(
        self,
        project: str,
        prompt: str,
        filename: str,
        quality: str = "high",
        aspect_ratio: str = "16:9",
        **kwargs: Any,
    ) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        if not self.api_key:
            return _return.failed(
                "GPT Image 2 is not configured",
                hint="set tools.media.image.apiKey and optionally apiBase/model, or export OPENROUTER_API_KEY",
            )
        digest = hashlib.sha256(f"{self.model}\x00{quality}\x00{aspect_ratio}\x00{prompt}".encode("utf-8")).hexdigest()[
            :12
        ]
        name = _filename(filename, digest)
        existing = deck.sources_dir / name
        if existing.is_file() and existing.stat().st_size > 0:
            outcome = await asyncio.to_thread(ingest_materials, deck.sources_dir, deck.ingest_dir)
            asset = next((entry for entry in outcome.assets if entry.source_file == name), None)
            return _return.done(
                project=project,
                model=self.model,
                path=str(existing),
                figure_id=getattr(asset, "asset_id", None),
                bytes=existing.stat().st_size,
                cached=True,
                asks=["inspect the returned figure id with ppt_figure_inspect, then place it with picture_fit"],
            )
        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "quality": quality,
            "aspect_ratio": aspect_ratio,
            "output_format": "png",
        }
        try:
            response = await self._generate(body)
            payload = _image_bytes(response)
        except httpx.HTTPError as exc:
            return _return.failed(f"image generation failed: {exc}")
        except (KeyError, ValueError, TypeError) as exc:
            return _return.failed(f"image generation returned no usable PNG: {exc}")

        source = sources.receive(deck, name, payload, f"generated:{self.model}")
        if source is None:
            return _return.failed("the generated image could not be added to this deck")
        try:
            outcome = await asyncio.to_thread(ingest_materials, deck.sources_dir, deck.ingest_dir)
        except (OSError, ValueError, FileNotFoundError) as exc:
            return _return.failed(f"the image was generated but ingest failed: {exc}", path=str(source.path))
        asset = next((entry for entry in outcome.assets if entry.source_file == source.name), None)
        return _return.done(
            project=project,
            model=self.model,
            path=str(source.path),
            figure_id=getattr(asset, "asset_id", None),
            bytes=len(payload),
            asks=["inspect the returned figure id with ppt_figure_inspect, then place it with picture_fit"],
        )

    async def _generate(self, body: dict[str, Any]) -> dict[str, Any]:
        openrouter = "openrouter.ai" in self.api_base
        endpoints = ["/images"] if openrouter else ["/images/generations", "/images"]
        request = dict(body)
        if not openrouter:
            ratio = request.pop("aspect_ratio", "16:9")
            request["size"] = {
                "16:9": "1536x864",
                "4:3": "1536x1152",
                "3:2": "1536x1024",
                "1:1": "1024x1024",
                "3:4": "1152x1536",
                "9:16": "864x1536",
            }[ratio]
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            proxy=self.proxy,
            timeout=300.0,
            trust_env=openrouter and self.proxy is None,
        ) as client:
            for index, endpoint in enumerate(endpoints):
                response = await client.post(f"{self.api_base}{endpoint}", headers=headers, json=request)
                if response.status_code == 404 and index + 1 < len(endpoints):
                    continue
                response.raise_for_status()
                return response.json()
        raise ValueError("no image endpoint accepted the request")


def _filename(value: str, digest: str) -> str:
    name = Path(value).name
    stem = _SAFE_NAME.sub("-", Path(name).stem).strip("-._") or "generated-visual"
    return f"{stem}-{digest}.png"


def _image_bytes(response: dict[str, Any]) -> bytes:
    item = (response.get("data") or [])[0]
    encoded = item.get("b64_json")
    if not encoded:
        raise ValueError("missing data[0].b64_json")
    return base64.b64decode(encoded, validate=True)
