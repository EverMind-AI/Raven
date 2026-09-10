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
_OPENROUTER_MODEL = "openai/gpt-image-2.5-sunburst"
_COMPATIBLE_MODEL = "gpt-image-2.5-sunburst"
# How many pictures are asked for at once when a call carries several.
_CONCURRENCY = 4

# How a cut-out is asked for: the subject on a green screen, keyed out afterwards. White
# was tried first and is the wrong ground -- it is inside most subjects (a shirt, a
# plate, a highlight) and the flood from the border had to stop at every one of them,
# while a solid #00ff00 is in almost nothing and keys out wherever it is.
_GREEN_SCREEN = "solid pure green background, hex #00ff00"
_CUT_OUT_PROMPT = (
    "Transparent-background production constraint: generate the subject on a "
    + _GREEN_SCREEN
    + ". Keep the background flat, evenly lit and shadow-free; do not use green in the subject, its "
    "reflections, glow or edge details; nothing touches the edges of the image. The green will be removed "
    "by chroma keying into a real alpha channel."
)
# Below this share of keyed pixels the model painted a scene, not a cut-out.
_CUT_OUT_MIN_SHARE = 0.2
# How far from a keyed pixel the rim is despilled, in pixels either side.
_DESPILL_REACH = 5

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


class PptGenerateImageTool(Tool):
    name = "ppt_generate_image"
    description = (
        "Generate a new raster visual for a page with GPT Image 2. Use this for an illustration or visual "
        "that does not already exist; use ppt_image_search and ppt_fetch for a real logo, product "
        "screen, published plot or other existing evidence. Say the style in the prompt, and let the "
        "subject choose it before the template does: a real place, street, market, crowd, product or "
        "building is photographic (natural light, no illustration) with the template's palette only in "
        "the grade; a concept with no face takes the template's own manner. Keep one manner per kind "
        "across the deck. One exception: the illustration slot of an illustrated template's cover or "
        "closing page takes a cut-out in the template's own manner (`transparent=true`, its palette and "
        "outline named in the prompt), even for a real place -- the slot is part of the design, and a "
        "photograph in it is a hole in the page. The generated PNG is added to this deck's "
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
        self._section = config
        self.proxy = proxy

    @property
    def config(self) -> "MediaToolConfig | None":
        """The section as the host has it now, not as assembly saw it.

        ``SessionTool`` keeps this tool for the life of the process, so a
        boot-time snapshot serves a rotated key and a changed model until the
        product restarts. A section carrying ``selectionConfig`` is a
        host-owned selection, and the trunk's own media tools re-read one per
        call through their callable source; this is the same resolution, and
        it is deliberately the trunk's rather than a second policy -- a
        section that leaves the host file answers "no key", which is how a
        selection is revoked without a restart.
        """
        section = self._section
        if not getattr(section, "selection_config", ""):
            return section
        from raven.config.live import resolve_media_selection

        return resolve_media_selection(section, "image")

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
                        "a background or decorative visual for the page: subject, scene, composition, then the "
                        "style -- photographic for a real place, street, market, crowd, product or building, the "
                        "template's own illustration manner only for a concept with no face. "
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
                "transparent": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "an illustration that sits on the template's own ground, in place of its cartoon: the "
                        "subject is drawn on a green screen and the green is keyed out to alpha afterwards, so "
                        "the page's colour shows around it. Then `replace_picture(<that "
                        "drawing>, path)` or `adapt(pictures={n: path})` puts it where the template's drawing "
                        "was. Not for a photograph or a scene that fills its frame"
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "4:3", "3:2", "1:1", "3:4", "9:16"],
                    "default": "16:9",
                    "description": (
                        "the shape of the frame the picture will fill: cover-fitting crops whatever does not "
                        "match, so a 16:9 picture in a portrait column keeps a sliver. Pick the nearest ratio "
                        "to the frame and put the subject where the crop keeps it"
                    ),
                },
                "prompts": {
                    "type": "array",
                    "description": (
                        "several pictures in one call, generated at the same time and ingested once: plan every "
                        "picture the deck needs, then ask for them together instead of one call each"
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "prompt": {"type": "string"},
                            "filename": {"type": "string"},
                            "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"]},
                            "transparent": {"type": "boolean"},
                            "aspect_ratio": {"type": "string", "enum": ["16:9", "4:3", "3:2", "1:1", "3:4", "9:16"]},
                        },
                        "required": ["prompt", "filename"],
                    },
                },
            },
            "required": ["project"],
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
        prompt: str = "",
        filename: str = "",
        quality: str = "high",
        aspect_ratio: str = "16:9",
        transparent: bool = False,
        prompts: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        if not self.api_key:
            return _return.failed(
                "no image API key is configured",
                hint="set tools.media.image.apiKey and optionally apiBase/model, or export OPENROUTER_API_KEY",
            )
        wanted = list(prompts or [])
        if prompt or filename:
            wanted.insert(
                0,
                {
                    "prompt": prompt,
                    "filename": filename,
                    "quality": quality,
                    "aspect_ratio": aspect_ratio,
                    "transparent": transparent,
                },
            )
        if not wanted:
            return _return.failed(
                "nothing to generate", hint="give prompt and filename, or prompts=[{prompt, filename}, ...]"
            )
        for spec in wanted:
            if not str(spec.get("prompt") or "").strip() or not str(spec.get("filename") or "").strip():
                return _return.failed(
                    "every picture needs a prompt and a filename", hint='prompts=[{"prompt": ..., "filename": ...}]'
                )
        # Generated together and ingested once: nine pictures made one at a time cost a
        # measured deck 16 minutes of waiting, and the ingest that follows each is a
        # rewrite of the same catalogue, which is not something to run nine ways at once.
        gate = asyncio.Semaphore(_CONCURRENCY)

        async def one(spec: dict[str, Any]) -> dict[str, Any]:
            async with gate:
                return await self._one(deck, spec)

        made = await asyncio.gather(*(one(spec) for spec in wanted))
        if any(item.get("path") for item in made):
            try:
                outcome = await asyncio.to_thread(ingest_materials, deck.sources_dir, deck.ingest_dir)
            except (OSError, ValueError, FileNotFoundError) as exc:
                return _return.failed(f"the images were generated but ingest failed: {exc}", results=made)
            by_name = {entry.source_file: getattr(entry, "asset_id", None) for entry in outcome.assets}
            for item in made:
                if item.get("path"):
                    item["figure_id"] = by_name.get(Path(item["path"]).name)
        asks = ["inspect the returned figure id with ppt_figure_inspect, then place it with picture_fit"]
        if len(made) == 1:
            item = made[0]
            if item.get("error"):
                return _return.failed(item["error"])
            return _return.done(
                project=project, model=self.model, asks=asks, **{k: v for k, v in item.items() if k != "error"}
            )
        failed = [item for item in made if item.get("error")]
        if failed:
            asks.insert(0, f"{len(failed)} of {len(made)} pictures failed; the error is on each")
        return _return.done(project=project, model=self.model, results=made, asks=asks)

    async def _one(self, deck: Project, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate one picture into the deck's sources; the figure id is filled in after ingest."""
        prompt = str(spec.get("prompt") or "")
        config = self.config
        if "quality" in getattr(config, "model_fields_set", set()):
            # The host's own setting outranks an omitted-or-supplied call
            # argument, the shared media tool's precedence. Empty is a real
            # answer there and means the provider's default, so it travels as
            # no `quality` field at all rather than as an empty string.
            quality = str(config.quality or "")
        else:
            quality = str(spec.get("quality") or "high")
        aspect_ratio = str(spec.get("aspect_ratio") or "16:9")
        transparent = bool(spec.get("transparent"))
        if transparent:
            # The providers reached here take no `background` parameter (OpenRouter
            # answers "Accepted: auto, opaque" for gpt-image-2.5-sunburst and
            # gpt-image-2 alike, and asked in words the model paints a checkerboard),
            # so a cut-out is asked for on a green screen and keyed.
            prompt = f"{prompt.rstrip()}\n\n{_CUT_OUT_PROMPT}"
        digest = hashlib.sha256(f"{self.model}\x00{quality}\x00{aspect_ratio}\x00{prompt}".encode("utf-8")).hexdigest()[
            :12
        ]
        name = _filename(str(spec.get("filename") or ""), digest)
        existing = deck.sources_dir / name
        if existing.is_file() and existing.stat().st_size > 0:
            return {"filename": name, "path": str(existing), "bytes": existing.stat().st_size, "cached": True}
        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "aspect_ratio": aspect_ratio,
            "output_format": "png",
        }
        if quality:
            body["quality"] = quality
        try:
            response = await self._generate(body)
            payload = _image_bytes(response)
        except httpx.HTTPError as exc:
            return {"filename": name, "error": f"image generation failed: {exc}"}
        except (KeyError, ValueError, TypeError) as exc:
            return {"filename": name, "error": f"image generation returned no usable PNG: {exc}"}
        made: dict[str, Any] = {"filename": name}
        if transparent:
            payload, share = key_out_green(payload)
            made["transparent_share"] = round(share, 2)
            if share < _CUT_OUT_MIN_SHARE:
                made["note"] = (
                    f"only {share:.0%} of the picture became transparent: the model painted a scene rather than "
                    "a subject on the green screen, so this is not a cut-out. Ask again with one subject and "
                    "nothing behind it"
                )
        source = sources.receive(deck, name, payload, f"generated:{self.model}")
        if source is None:
            return {"filename": name, "error": "the generated image could not be added to this deck"}
        return {**made, "path": str(source.path), "bytes": len(payload)}

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


def key_out_green(png: bytes) -> tuple[bytes, float]:
    """Key a generated picture's green screen to alpha; returns the PNG and the share keyed.

    Any pixel that reads as screen green goes -- the same test on every pixel, so the
    pockets a flood from the border cannot reach (between an arm and a body) go too.
    The rim a few pixels wide around what was keyed is despilled: an edge pixel that
    blended with the screen has more green than either of its other channels, and
    taking that excess off leaves the subject's own colour instead of a green fringe.
    """
    import io

    from PIL import Image, ImageFilter

    image = Image.open(io.BytesIO(png)).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    keyed = Image.new("L", image.size, 0)
    marks = keyed.load()
    count = 0
    for y in range(height):
        for x in range(width):
            r, g, b, _ = pixels[x, y]
            if _is_screen_green(r, g, b):
                marks[x, y] = 255
                count += 1
    rim = keyed.filter(ImageFilter.MaxFilter(_DESPILL_REACH))
    near = rim.load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if marks[x, y]:
                pixels[x, y] = (r, g, b, 0)
            elif near[x, y] and g > max(r, b):
                pixels[x, y] = (r, max(r, b), b, a)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), count / float(width * height)


def _is_screen_green(r: int, g: int, b: int) -> bool:
    """Whether a pixel is the green screen: near #00ff00, or a green hue that is bright and saturated."""
    if g >= 145 and r <= 130 and b <= 130 and g - max(r, b) >= 35:
        return True
    top, low = max(r, g, b), min(r, g, b)
    if top == 0 or g <= r or g <= b:
        return False
    spread = top - low
    if spread == 0:
        return False
    hue = 60 * ((b - r) / spread + 2)
    return 80 <= hue <= 160 and spread / top >= 0.35 and top / 255 >= 0.45
