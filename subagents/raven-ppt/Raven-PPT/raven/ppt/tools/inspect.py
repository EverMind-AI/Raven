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

from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.ppt.contracts import Project
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


class PptFigureInspectTool(Tool):
    name = "ppt_figure_inspect"
    description = (
        "Look at figures ppt_ingest extracted, before you place them. Pass the figure ids and it returns "
        "each one as an image, with the label its own source gave it, anything the extraction was unsure "
        "about, and the width past which it will look soft. Worth doing for every figure a page depends "
        "on: the catalogue tells you what a figure is called, not whether it is legible, whether it is "
        "really a figure rather than a logo, or whether it is a composite you should be cropping."
    )
    timeout_seconds = 120.0

    def __init__(self, workspace: Path, views: Any) -> None:
        self.workspace = workspace
        self.views = views

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_ingest"},
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
            return _return.failed(str(exc), hint='figures: ["tarvis_p003_fig12", …]')

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

        payload: dict[str, Any] = {"project": project, "figures": [_described(name, e) for name, e in seen]}
        if unknown:
            payload["not_in_the_catalogue"] = unknown
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


def _catalogue(deck: Project) -> dict[str, dict[str, Any]]:
    import json

    try:
        loaded = json.loads((deck.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    assets = loaded.get("assets") if isinstance(loaded, dict) else None
    return {str(k): v for k, v in (assets or {}).items() if isinstance(v, dict)}


def _described(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    width, height = int(entry.get("width_px") or 0), int(entry.get("height_px") or 0)
    described: dict[str, Any] = {"figure_id": name, "kind": entry.get("kind") or "figure"}
    for field in ("source_label", "caption", "source_file", "source_page"):
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
        lines.append(f"  its source's caption: {str(entry['caption'])[:200]}")
    if entry.get("concerns"):
        lines.append(f"  the extraction was unsure about: {', '.join(entry['concerns'])}")
    return "\n".join(lines)
