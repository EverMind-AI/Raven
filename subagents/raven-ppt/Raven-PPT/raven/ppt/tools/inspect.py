"""`ppt_figure_inspect`: look at a figure before placing it.

The one stage route A declared and never had. `ppt_ingest` returns figure ids and
the caption each one carries in its source, which is enough to cite one correctly
and not enough to place one: an author choosing `fig_3` is choosing something it
has never seen. It cannot tell a legible plot from a scanned blur, a figure from a
logo that happened to survive extraction, or a two-panel composite from a single
chart -- and each of those is a page that has to be rebuilt once somebody looks.

So this hands over the pixels, with the three things the catalogue knows that a
render does not say: the label the *source* gave it, the concerns the ingest
recorded while extracting it, and its native size -- from which the width beyond
which it softens follows, which is the one number that decides whether a figure
can carry a page or has to sit beside the copy.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.ppt.contracts import Project, brief_path, load_brief
from raven.ppt.services.ingest import CATALOGUE_FILE, FIGURES_DIR
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_strings
from raven.utils.helpers import image_block, text_block

# How many figures come back in one call. Each is an image, so this is a request
# body rather than a preference; asking for the two or three a page will use is
# the working case anyway.
MAX_FIGURES = 6

# What a bitmap is placed at before it visibly softens, in pixels per inch. A
# figure 640px wide holds up to about 6.7in and not past it.
LEGIBLE_PPI = 96.0

CAPTION_BRIEF = """Describe this visual for presentation planning in {language}. Use only what is
visible in the image and the source metadata supplied with it. Write one concise
caption, under {limit} characters, that says what the visual shows; do not claim
provenance, results or meaning the pixels do not establish. Concretely: do not say
who made it, which paper, product or company it belongs to, or what it proves; do
not number it ("Figure 3"); do not name a system, dataset or organisation unless
those words are printed in the image. Name the kind of thing it is before you
describe it -- a banner with a headline and a statistic on it is a banner, not the
architecture of whatever it advertises; a screenshot is a screenshot; a logo is a
logo. Reply as JSON and nothing else:

{{"visual_caption": "one sentence"}}
"""

# What an inspected caption may be, checked rather than only asked for. The brief
# above already said all of this and a live run disregarded it, so what is worth
# having here is whatever can be decided from the reply alone: its length, and
# whether it numbered the figure. Whether the sentence is *true* of the picture is
# not decidable here -- that is `inferred_caption` in services/measure/captions.py,
# which can compare the caption against the deck's materials.
MAX_CAPTION_CHARS = 220

# `Figure 3`, `Fig. 3`, `Table 1`, `图 3` in a caption the source did not write. A
# number is the source's own, `source_label` is the field that holds one, and the
# citation gate reads that field -- so a number invented here lets a page cite
# "Fig. 5" with nothing to check it against.
#
# Deliberately narrower than case-insensitive: a citation is capitalised and a
# description is not ("a table 3 rows tall" describes, "Table 3" cites), and the
# CJK half must not fire inside a compound, where the character is the name of a
# chart kind rather than a reference -- the digit after the compound in "柱状图 3
# 个分组" counts columns. Missing a shouty or mid-sentence reference is the right
# way to be wrong here, because this drops the model's reply on the floor.
_NUMBERED_RE = re.compile(r"\b(?:Fig|Figure|Table)s?\.?\s*\d|(?<![一-鿿])[图表]\s*\d")


class PptFigureInspectTool(Tool):
    name = "ppt_figure_inspect"
    description = (
        "Look at figures ppt_ingest extracted, before you place them. Pass the figure ids and it returns "
        "each one as an image, with the label its own source gave it, anything the extraction was unsure "
        "about, and the width past which it will look soft. Worth doing for every figure a page depends "
        "on: the catalogue tells you what a figure is called, not whether it is legible, whether it is "
        "really a figure rather than a logo, or whether it is a composite you should be cropping. When "
        "the source supplied no caption, visual inspection writes a separate visual_caption back to the "
        "catalogue; it never overwrites or impersonates a source-authored caption."
    )
    timeout_seconds = 120.0

    def __init__(self, workspace: Path, views: Any, composer: Any | None = None) -> None:
        self.workspace = workspace
        self.views = views
        self.composer = composer

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "figures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_FIGURES,
                    "description": (
                        "the figure ids to look at, as ppt_ingest listed them. Omit to see the first few "
                        "the catalogue holds"
                    ),
                },
            },
            "required": ["project"],
        }

    async def execute(self, project: str, figures: list[str] | None = None, **kwargs: Any) -> str | ToolResult:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        try:
            figures = as_strings(figures, "figures")
        except ArgumentError as exc:
            return _return.failed(str(exc), hint='figures: ["tarvis_p003_fig12", "tarvis_p004_fig01"]')

        catalogue = _catalogue(deck)
        if not catalogue:
            return _return.failed(
                "nothing has been ingested for this deck, so there are no figures to look at",
                hint="run ppt_ingest on the materials first",
            )

        wanted = [name for name in (figures or list(catalogue))[:MAX_FIGURES]]
        unknown = [name for name in wanted if name not in catalogue]
        seen = [(name, catalogue[name]) for name in wanted if name in catalogue]
        if not seen:
            return _return.failed(
                f"none of {wanted} is in this deck's figure catalogue",
                hint=f"the ids it holds are {', '.join(sorted(catalogue)[:20])}",
            )

        declined = await self._enrich_captions(deck, catalogue, seen)
        payload: dict[str, Any] = {"project": project, "figures": [_described(name, e) for name, e in seen]}
        if unknown:
            payload["not_in_the_catalogue"] = unknown
        if declined:
            payload["no_caption_written_for"] = declined
        asks = ["place only what you have looked at, and crop or drop what does not read at the size the page gives it"]
        blocks: list[Any] = []
        for name, entry in seen:
            path = deck.ingest_dir / FIGURES_DIR / str(entry.get("file") or "")
            shown = self._shown(path, deck.review_dir / "figures")
            if shown is None:
                continue
            blocks.append(text_block(_label(name, entry)))
            blocks.append(image_block(self.views.data_uri(shown)))
        if not blocks:
            payload["renders"] = "the figure files could not be read from the project"
        return _return.with_images(_return.done(asks=asks, **payload), blocks)

    async def _enrich_captions(
        self,
        deck: Project,
        catalogue: dict[str, dict[str, Any]],
        seen: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, str]:
        """Write a caption for each figure whose source printed none.

        Returns the figures it declined to caption and why, so a reply that wrote
        nothing says so rather than looking like a figure nobody looked at.
        """
        if self.composer is None:
            return {}
        brief = load_brief(brief_path(deck))
        language = brief.language if brief is not None else "the deck's language"

        async def one(name: str, entry: dict[str, Any]) -> tuple[str, str, str] | None:
            if entry.get("caption") or entry.get("visual_caption"):
                return None
            path = deck.ingest_dir / FIGURES_DIR / str(entry.get("file") or "")
            shown = self._shown(path, deck.review_dir / "figures")
            if shown is None:
                return None
            reply = await self.composer.ask(
                CAPTION_BRIEF.format(language=language, limit=MAX_CAPTION_CHARS),
                [text_block(_label(name, entry)), image_block(self.views.data_uri(shown))],
                max_tokens=1200,
            )
            try:
                payload = json.loads(reply[reply.index("{") : reply.rindex("}") + 1])
            except (ValueError, AttributeError):
                return None
            caption, refused = _vetted(str(payload.get("visual_caption") or ""))
            return (name, caption, refused) if caption or refused else None

        described = await asyncio.gather(*(one(name, entry) for name, entry in seen))
        declined: dict[str, str] = {}
        changed = False
        for result in described:
            if result is None:
                continue
            name, caption, refused = result
            if caption:
                catalogue[name]["visual_caption"] = caption
                changed = True
            else:
                declined[name] = refused
        if changed:
            _write_catalogue(deck, catalogue)
        return declined

    def _shown(self, path: Path, out_dir: Path) -> Path | None:
        """The figure as a PNG this tool can hand over.

        Converted rather than passed through: the encoder labels whatever it sends
        as `image/png`, and a JPEG announced as a PNG is the kind of thing that
        works until it does not.
        """
        if not path.is_file():
            return None
        if path.suffix.lower() == ".png":
            return path
        try:
            from PIL import Image

            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{path.stem}.png"
            with Image.open(path) as image:
                image.convert("RGB").save(target, format="PNG")
        except Exception:  # noqa: BLE001 -- a figure that will not decode is skipped, not fatal
            return None
        return target


def _vetted(caption: str) -> tuple[str, str]:
    """The caption to keep, or "" and the reason it was not kept.

    Both objections are about the shape of the reply and neither needs the picture,
    which is the point: the brief asks for these in prose and a live run answered
    with a caption that broke one of them anyway. Declined rather than trimmed,
    because a caption edited into shape by string surgery is a third claim nobody
    made, and no caption is the state a figure inspection never ran on is already in.
    """
    collapsed = " ".join(caption.split())
    if not collapsed:
        return "", ""
    if len(collapsed) > MAX_CAPTION_CHARS:
        return "", (
            f"inspection replied with {len(collapsed)} characters where the brief asks for one caption under "
            f"{MAX_CAPTION_CHARS}; nothing was written to the catalogue"
        )
    if _NUMBERED_RE.search(collapsed):
        return "", (
            "inspection numbered the figure, which is the source's own label and not something the pixels "
            "establish; nothing was written to the catalogue"
        )
    return collapsed, ""


def _catalogue(deck: Project) -> dict[str, dict[str, Any]]:
    try:
        loaded = json.loads((deck.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    assets = loaded.get("assets") if isinstance(loaded, dict) else None
    return {str(k): v for k, v in (assets or {}).items() if isinstance(v, dict)}


def _write_catalogue(deck: Project, catalogue: dict[str, dict[str, Any]]) -> None:
    path = deck.ingest_dir / CATALOGUE_FILE
    path.write_text(
        json.dumps({"schema": "raven.ppt.assets.v1", "assets": catalogue}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _described(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    width, height = int(entry.get("width_px") or 0), int(entry.get("height_px") or 0)
    described: dict[str, Any] = {"figure_id": name, "kind": entry.get("kind") or "figure"}
    for field in ("source_label", "caption", "visual_caption", "source_file", "source_page"):
        if entry.get(field):
            described[field] = entry[field]
    if width and height:
        described["pixels"] = f"{width}x{height}"
        described["holds_up_to_in"] = round(width / LEGIBLE_PPI, 1)
    if int(entry.get("panel_count") or 1) > 1:
        described["panels"] = entry["panel_count"]
    if entry.get("concerns"):
        described["concerns"] = list(entry["concerns"])
    return described


def _label(name: str, entry: dict[str, Any]) -> str:
    said = entry.get("source_label") or entry.get("kind") or "figure"
    width = int(entry.get("width_px") or 0)
    lines = [f"{name} — {said}" + (f", holds up to {width / LEGIBLE_PPI:.1f}in wide" if width else "")]
    if entry.get("caption"):
        lines.append(f"  its source's caption, quotable and creditable: {str(entry['caption'])[:200]}")
    if entry.get("visual_caption"):
        lines.append(
            "  what inspection saw in the pixels, nobody's caption -- describe it, never quote or credit it: "
            + str(entry["visual_caption"])[:200]
        )
    if entry.get("concerns"):
        lines.append(f"  the extraction was unsure about: {', '.join(entry['concerns'])}")
    return "\n".join(lines)
