"""Generate a slide image and make it a figure of this deck immediately.

The generation itself is the host's: this tool rides ``image_generate``'s
transport (``raven.agent.tools.media_gen``), so whichever image model, base
and key the deployment configured under ``tools.media.image`` -- a gpt-image
deployment behind OpenRouter, a chat-routed model such as Nano Banana, an
OpenAI-compatible gateway -- serves the deck too, the spend is recorded where
the host records it, and a Settings edit lands on the next call. What is the
deck's own is everything around the call: the cut-out on a green screen and
its keying, the file the picture lands under, the figure that comes out the
other end.

The file name is the caller's, and every call generates into it, overwriting
what the last ask left there. Nothing is answered from disk: the ask this tool
exists for is "that picture is not what I wanted, do it again", and it arrives
carrying the same words as the ask before it, so a name-keyed cache handed back
the very picture that had just been rejected. Overwriting the name is also what
keeps the re-ask cheap for the page: the program already names that file, so
the second picture reaches the deck with no edit to the script.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.contracts.tool import Tool
from raven_ppt.contracts import Project
from raven_ppt.services.ingest import ingest_materials, sources
from raven_ppt.tools import _return

if TYPE_CHECKING:
    from raven.agent.tools.media_gen import ImageGenerateTool

# How many pictures are asked for at once when a call carries several.
_CONCURRENCY = 4

RATIOS = ("16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16", "21:9")
QUALITIES = ("low", "medium", "high")

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
# Where a generation lands before it is keyed and received into sources.
_SCRATCH_DIRNAME = ".generated"


class PptGenerateImageTool(Tool):
    name = "ppt_generate_image"
    description = (
        "Generate a new raster visual for a page with the image model this deployment configured. Use it for "
        "an illustration, a backdrop or a decorative picture that does not exist yet; use ppt_image_search and "
        "ppt_fetch for a real logo, product screen, published plot or other existing evidence. Every call "
        "generates: asking again for a picture you did not like gets a new one, written over the file the last "
        "ask made, so the page that names it needs no edit. Decide the manner "
        "from two things you can see, in this order. First the subject: a real place, street, market, crowd, "
        "product, building or meal is photographic (natural light, no illustration), with the template's palette "
        "only in the grade. Then the slot: an idea with no face takes the manner of the picture the template "
        "itself put where this one will go -- look at the template's render and say what you see (photograph, "
        "flat vector, line drawing, 3D render, watercolour; its palette; its outline weight) rather than naming a "
        "template or a house style. A slot that holds a photograph takes a photograph; a slot that holds a "
        "drawing takes a cut-out in that drawing's manner (`transparent=true`, the manner in the prompt). Keep one "
        "manner per kind across the deck. `references` carries pictures to match or vary: the template's own "
        "illustration for its manner, an earlier generation for consistency, a user's photograph to restyle. The "
        "generated PNG is added to this deck's sources, ingested immediately, and returned with its figure id so "
        "the build can place it."
    )
    timeout_seconds = 360.0

    def __init__(
        self,
        workspace: Path,
        media: "ImageGenerateTool | None" = None,
        *,
        config: Any = None,
        proxy: str | None = None,
        usage_recorder: Any = None,
    ) -> None:
        self.workspace = workspace
        if media is None:
            from raven.agent.tools.media_gen import ImageGenerateTool

            media = ImageGenerateTool(config, workspace=workspace, proxy=proxy, usage_recorder=usage_recorder)
        self.media = media

    @property
    def parameters(self) -> dict[str, Any]:
        picture = {
            "prompt": {
                "type": "string",
                "description": (
                    "subject, scene, composition, then the manner -- photographic for a real place, street, "
                    "market, crowd, product or building; for a concept, the manner of the picture the template "
                    "shows in that slot, described from the render. Do not use this for a product screenshot, "
                    "logo, published chart, paper figure or factual architecture. Do not put prose, labels or "
                    "numbers into the image; slide text stays editable in PowerPoint"
                ),
            },
            "filename": {
                "type": "string",
                "description": "short file name for the generated PNG, without a directory",
            },
            "quality": {
                "type": "string",
                "enum": list(QUALITIES),
                "description": (
                    "leave it unset and the host's own setting decides, which is what the same model gets "
                    "asked for outside a deck; name one only to overrule that for this picture"
                ),
            },
            "transparent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "a cut-out that sits on the template's own ground, in place of a drawing it put there: the "
                    "subject is generated on a green screen and the green is keyed out to alpha afterwards, so "
                    "the page's colour shows around it. Then `replace_picture(<that drawing>, path)` or "
                    "`adapt(pictures={n: path})` puts it where the template's drawing was. Not for a photograph "
                    "or a scene that fills its frame"
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(RATIOS),
                "default": "16:9",
                "description": (
                    "the shape of the frame the picture will fill: cover-fitting crops whatever does not match, "
                    "so a 16:9 picture in a portrait column keeps a sliver. Pick the nearest ratio to the frame "
                    "and put the subject where the crop keeps it. A model that only draws a few frames is asked "
                    "for the nearest one and the result is cropped to the box"
                ),
            },
            "references": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "pictures the generation should match or vary, as figure ids of this deck, file names under "
                    "its sources, or absolute paths: the template's own illustration to take its manner, an "
                    "earlier generation to stay consistent with, a user's photograph to restyle. Up to six"
                ),
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project"},
                **picture,
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
                            "quality": {"type": "string", "enum": list(QUALITIES)},
                            "transparent": {"type": "boolean"},
                            "aspect_ratio": {"type": "string", "enum": list(RATIOS)},
                            "references": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["prompt", "filename"],
                    },
                },
            },
            "required": ["project"],
        }

    @property
    def model(self) -> str:
        return self.media._model(None)

    @property
    def api_key(self) -> str:
        return self.media.api_key

    def configured(self) -> bool:
        """Offered on the host tool's own terms: its section names a key or a model.

        The loop's withheld axis asks this per assembly (the paper is in
        ``raven/contracts/plugin_surface.py``), so the deck's generator follows a
        section added or emptied in Settings the way ``image_generate`` does. An
        ambient ``OPENROUTER_API_KEY`` alone does not offer it, for the reason it
        does not offer the built-in: a key set for chat must not switch on a tool
        that bills per call.
        """
        return self.media.configured()

    async def execute(
        self,
        project: str,
        prompt: str = "",
        filename: str = "",
        quality: str | None = None,
        aspect_ratio: str = "16:9",
        transparent: bool = False,
        references: list[str] | None = None,
        prompts: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        if not self.media.api_key:
            return _return.failed(
                "no image API key is configured",
                hint="set tools.media.image.apiKey (or providers.openrouter.apiKey) in the host config",
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
                    "references": references or [],
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
            ratio = str(spec.get("aspect_ratio") or "16:9")
            if ratio not in RATIOS:
                return _return.failed(f"aspect_ratio {ratio!r} is not one of {', '.join(RATIOS)}")
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
        model = next((item["model"] for item in made if item.get("model")), self.model)
        if len(made) == 1:
            item = made[0]
            if item.get("error"):
                return _return.failed(item["error"])
            return _return.done(
                project=project,
                model=model,
                asks=asks,
                **{k: v for k, v in item.items() if k not in ("error", "model")},
            )
        failed = [item for item in made if item.get("error")]
        if failed:
            asks.insert(0, f"{len(failed)} of {len(made)} pictures failed; the error is on each")
        return _return.done(project=project, model=model, results=made, asks=asks)

    async def _one(self, deck: Project, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate one picture into the deck's sources; the figure id is filled in after ingest."""
        prompt = str(spec.get("prompt") or "")
        # None rather than a quality of this tool's own, because the host resolves an
        # unset one: Settings' value where the operator set one, otherwise whatever the
        # model's provider defaults to. A default here stood in for "unset" and sent
        # `high` where the same model asked for outside a deck was sent nothing.
        quality = str(spec["quality"]) if spec.get("quality") else None
        aspect_ratio = str(spec.get("aspect_ratio") or "16:9")
        transparent = bool(spec.get("transparent"))
        try:
            refs = self._references(deck, spec.get("references") or [])
        except FileNotFoundError as exc:
            return {"filename": str(spec.get("filename") or ""), "error": str(exc)}
        if transparent:
            # The image endpoints take no `background` parameter (OpenRouter's gpt-image-2
            # answers "Accepted: auto, opaque", and asked in words it paints a checkerboard),
            # so a cut-out is asked for on a green screen and keyed.
            prompt = f"{prompt.rstrip()}\n\n{_CUT_OUT_PROMPT}"
        model = self.model
        # The name is keyed on what the host will send, not on what was asked --
        # Settings' quality replaces the request's -- so the same ask lands on the
        # same file and a re-ask overwrites it. It is a name, not a cache: a call
        # generates. The author asks for a picture again when the one it got is not
        # the one it wanted, and answering that from disk hands back the picture it
        # just rejected. Overwriting is what makes the retry work without editing
        # the script, since the page already names this file.
        sent_quality = self.media.effective_quality(quality, model)
        ref_digest = "".join(hashlib.sha256(ref.read_bytes()).hexdigest()[:8] for ref in refs)
        digest = hashlib.sha256(
            f"{model}\x00{sent_quality}\x00{aspect_ratio}\x00{ref_digest}\x00{prompt}".encode("utf-8")
        ).hexdigest()[:12]
        name = _filename(str(spec.get("filename") or ""), digest)
        scratch = deck.build_dir / _SCRATCH_DIRNAME
        scratch.mkdir(parents=True, exist_ok=True)
        answer = await self.media.execute(
            prompt=prompt,
            images=[str(ref) for ref in refs] or None,
            aspect_ratio=aspect_ratio,
            quality=quality,
            output_dir=str(scratch),
        )
        try:
            reply = json.loads(answer)
        except (TypeError, ValueError):
            return {"filename": name, "error": f"image generation returned no usable answer: {answer!r}"}
        if reply.get("error") or not reply.get("paths"):
            return {"filename": name, "error": f"image generation failed: {reply.get('error') or 'no image returned'}"}
        produced = Path(reply["paths"][0])
        try:
            payload = _as_png(produced.read_bytes())
        except (OSError, ValueError) as exc:
            return {"filename": name, "error": f"image generation returned no usable picture: {exc}"}
        finally:
            for extra in reply["paths"]:
                Path(extra).unlink(missing_ok=True)
        made: dict[str, Any] = {"filename": name, "model": str(reply.get("model") or model)}
        if transparent:
            payload, share = key_out_green(payload)
            made["transparent_share"] = round(share, 2)
            if share < _CUT_OUT_MIN_SHARE:
                made["note"] = (
                    f"only {share:.0%} of the picture became transparent: the model painted a scene rather than "
                    "a subject on the green screen, so this is not a cut-out. Ask again with one subject and "
                    "nothing behind it"
                )
        made["width"], made["height"] = _dimensions(payload)
        source = sources.receive(deck, name, payload, f"generated:{made['model']}")
        if source is None:
            return {"filename": name, "error": "the generated image could not be added to this deck"}
        return {**made, "path": str(source.path), "bytes": len(payload)}

    def _references(self, deck: Project, given: list[Any]) -> list[Path]:
        """Resolve the pictures a generation should match: figure ids, source names, or paths."""
        found: list[Path] = []
        for item in list(given)[:6]:
            text = str(item or "").strip()
            if not text:
                continue
            candidates = [Path(text)] if Path(text).is_absolute() else []
            candidates += [deck.sources_dir / text, deck.sources_dir / Path(text).name]
            figures = deck.ingest_dir / "figures"
            if figures.is_dir():
                candidates += list(figures.glob(f"{Path(text).stem}*"))
                candidates += list(figures.glob(f"*{text}*"))
            hit = next((c for c in candidates if c.is_file()), None)
            if hit is None:
                raise FileNotFoundError(f"reference {text!r} is not a figure id, a source file or a readable path")
            found.append(hit)
        return found


def _filename(value: str, digest: str) -> str:
    name = Path(value).name
    stem = _SAFE_NAME.sub("-", Path(name).stem).strip("-._") or "generated-visual"
    return f"{stem}-{digest}.png"


def _as_png(payload: bytes) -> bytes:
    """A generation as PNG bytes, whatever the model answered with (jpeg, webp)."""
    from PIL import Image

    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return payload
    image = Image.open(io.BytesIO(payload))
    out = io.BytesIO()
    image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(out, format="PNG")
    return out.getvalue()


def _dimensions(png: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        return image.size


def key_out_green(png: bytes) -> tuple[bytes, float]:
    """Key a generated picture's green screen to alpha; returns the PNG and the share keyed.

    Any pixel that reads as screen green goes -- the same test on every pixel, so the
    pockets a flood from the border cannot reach (between an arm and a body) go too.
    The rim a few pixels wide around what was keyed is despilled: an edge pixel that
    blended with the screen has more green than either of its other channels, and
    taking that excess off leaves the subject's own colour instead of a green fringe.
    """
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


__all__ = ["PptGenerateImageTool", "key_out_green", "RATIOS", "QUALITIES"]
