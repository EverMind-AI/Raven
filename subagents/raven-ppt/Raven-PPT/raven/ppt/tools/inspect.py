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

And it looks, at every figure it is asked about. The look used to be the means to
one end -- a caption for a figure whose source printed none -- so a figure that
arrived captioned was handed over unseen, and the whole catalogue of a captioned
paper went to the author with nothing having read a single pixel of it. A source
caption is a claim about the figure the source drew, not about the file the
extraction produced: a render that lost its top edge arrives under the caption of
the whole one, which makes the caption the last field that should decide whether
anybody looks.

Two products, then, and each one owed on its own terms. A single field deciding a
call that produces two things has now been wrong twice -- the source caption
first, and then the stored review, which skipped the figure whose caption a
malformed reply had already cost it. Neither is a better condition than the other;
the mistake was having one at all.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
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

LOOK_BRIEF = """Look at this visual for presentation planning and answer in {language}. Use
only what is visible in the image and the source metadata supplied with it.
"""

REVIEW_ASK = """
Say what looking at this render shows, in one or two sentences: whether the whole
visual is there or an edge of it has been cut off, whether the smallest type in it
can still be read, whether it is one plot or several panels stitched together, and
whether it is a figure at all rather than a logo, a banner or a piece of page
decoration. Judge the pixels in front of you and not the label they arrived under:
the label names the figure the source drew, and a render that lost an edge on the
way out of that source still arrives under it. Say what you see either way --
"reads whole and legible" is an observation and silence is not.
"""

CAPTION_ASK = """
Write one concise caption, under {limit} characters, that says what the visual
shows; do not claim provenance, results or meaning the pixels do not establish.
Concretely: do not say who made it, which paper, product or company it belongs to,
or what it proves; do not number it ("Figure 3"); do not name a system, dataset or
organisation unless those words are printed in the image. Name the kind of thing it
is before you describe it -- a banner with a headline and a statistic on it is a
banner, not the architecture of whatever it advertises; a screenshot is a
screenshot; a logo is a logo.
"""

REPLY_ASK = """
Reply as JSON and nothing else:

{fields}
"""

REVIEW_FIELD = '"visual_review": "one or two sentences"'
CAPTION_FIELD = '"visual_caption": "one sentence"'

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
        "about, the width past which it will look soft, and what looking at the render showed: whether "
        "the figure arrived whole or an edge of it is cut off, whether its type is legible, whether it is "
        "really a figure rather than a logo, whether it is a composite you should be cropping. Worth "
        "doing for every figure a page depends on, the captioned ones included -- a caption describes the "
        "figure its source drew, not the file the extraction produced, so a render missing an edge "
        "arrives under the caption of the whole one. When the source supplied no caption, inspection also "
        "writes a separate visual_caption back to the catalogue; it never overwrites or impersonates a "
        "source-authored caption. A figure it names under no_caption_written_for is still owed one -- ask "
        "again and it asks the model for the caption alone, without re-reading a render it has read."
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

        declined, unseen = await self._looked_at(deck, catalogue, seen)
        payload: dict[str, Any] = {"project": project, "figures": [_described(name, e) for name, e in seen]}
        if unknown:
            payload["not_in_the_catalogue"] = unknown
        if declined:
            payload["no_caption_written_for"] = declined
        if unseen:
            payload["not_looked_at"] = unseen
        asks = [
            "place only what you have looked at, and crop or drop what does not read at the size the page gives it",
            "act on visual_review rather than on the caption: a render it reports as cut off, unreadable or not a "
            "figure has to be cropped, replaced or left out, however well the caption beside it reads",
        ]
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

    @staticmethod
    def _debts(entry: dict[str, Any]) -> tuple[bool, bool]:
        """What this figure still owes: a look, and a caption of this tool's writing.

        One reader, because a second spelling of it is what went wrong three times.
        The condition started as the source caption, became the stored review, and
        then survived in the no-model branch while the model-backed one had moved
        on -- each time because the answer was written where it was needed rather
        than once.
        """
        return not entry.get("visual_review"), not (entry.get("caption") or entry.get("visual_caption"))

    async def _looked_at(
        self,
        deck: Project,
        catalogue: dict[str, dict[str, Any]],
        seen: list[tuple[str, dict[str, Any]]],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Look at every figure, and caption the ones whose source printed none.

        Two products, and the unit of work is the product rather than the call.
        Twice now a single condition has stood in for both: first the source
        caption, which returned a captioned figure unlooked-at, and then the stored
        review, which skipped a figure whose caption a bad reply had cost it and
        left it source-less for good -- the second call, holding the review, no
        longer even reported that a caption was owed. So each half is asked for
        only while it is missing and asked for again until it lands; what may skip
        a figure entirely is holding both.

        Returns the figures no caption was written for and the figures nothing
        looked at, each with the reason. A reply silent about a figure would read
        as one that was looked at and found fine.
        """
        if self.composer is None:
            # Per product here too. A figure whose review is already stored has been
            # looked at, and saying otherwise tells the author to re-run something
            # that is done; a source-less one still owes its caption, and dropping
            # that silently is what leaves it source-less for good.
            declined: dict[str, str] = {}
            unseen: dict[str, str] = {}
            for name, entry in seen:
                owes_review, owes_caption = self._debts(entry)
                if owes_review:
                    unseen[name] = "no model is configured for this deck, so nothing read the pixels"
                if owes_caption:
                    declined[name] = "no model is configured for this deck, so no caption could be written"
            return declined, unseen
        brief = load_brief(brief_path(deck))
        language = brief.language if brief is not None else "the deck's language"

        async def one(name: str, entry: dict[str, Any]) -> _Looked | None:
            owes_review, owes_caption = self._debts(entry)
            if not owes_review and not owes_caption:
                return None
            path = deck.ingest_dir / FIGURES_DIR / str(entry.get("file") or "")
            shown = self._shown(path, deck.review_dir / "figures")
            if shown is None:
                # Against the halves still owed, like the unparseable-reply branch
                # below: a figure whose review is stored has been looked at, and
                # saying otherwise sends the author to redo what is done.
                unreadable = "the figure file could not be read from the project"
                return _Looked(
                    name,
                    unseen=unreadable if owes_review else "",
                    refused=unreadable if owes_caption else "",
                )
            reply, failure = await self.composer.ask_with_failure(
                _brief(language, wants_review=owes_review, wants_caption=owes_caption),
                [text_block(_label(name, entry)), image_block(self.views.data_uri(shown))],
                max_tokens=1200,
            )
            try:
                payload = json.loads(reply[reply.index("{") : reply.rindex("}") + 1])
            except (ValueError, AttributeError):
                # Against every half that was owed, because every half is still
                # owed: a run reporting only the look leaves the author reading a
                # figure whose caption silently never arrived.
                nothing = _why_nothing_came_back(failure)
                return _Looked(
                    name,
                    unseen=nothing if owes_review else "",
                    refused=nothing if owes_caption else "",
                )
            review = " ".join(str(payload.get("visual_review") or "").split()) if owes_review else ""
            caption, refused = _vetted(str(payload.get("visual_caption") or "")) if owes_caption else ("", "")
            if owes_caption and not caption and not refused:
                refused = "inspection replied without a caption for a figure whose source printed none"
            unseen = "inspection replied without an observation about the render" if owes_review and not review else ""
            return _Looked(name, review=review, caption=caption, refused=refused, unseen=unseen)

        looked = await asyncio.gather(*(one(name, entry) for name, entry in seen))
        declined: dict[str, str] = {}
        unseen: dict[str, str] = {}
        changed = False
        for result in looked:
            if result is None:
                continue
            for field, value in (("visual_review", result.review), ("visual_caption", result.caption)):
                if value:
                    catalogue[result.figure_id][field] = value
                    changed = True
            if result.refused:
                declined[result.figure_id] = result.refused
            if result.unseen:
                unseen[result.figure_id] = result.unseen
        if changed:
            _write_catalogue(deck, catalogue)
        return declined, unseen

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


@dataclass(frozen=True)
class _Looked:
    """What one figure's inspection produced, and what it did not.

    `unseen` is the reason nothing was observed about the render, and it is a
    separate field from `refused` rather than the same one: a figure can be looked
    at and still have its caption turned down, a figure holding a review can be
    owed a caption and nothing else, and a figure nobody could look at is not a
    caption problem. `refused` is why no caption was written and not only why one
    was rejected -- a caption the reply omitted is as absent as a caption it got
    wrong.
    """

    figure_id: str
    review: str = ""
    caption: str = ""
    refused: str = ""
    unseen: str = ""


def _brief(language: str, *, wants_review: bool, wants_caption: bool) -> str:
    """What one figure is asked, which is whatever it still owes and nothing else.

    Two halves asked independently because they are owed independently. A figure
    holding a review and no caption is asked for the caption alone: re-asking for
    the review would overwrite a settled observation with a fresh one, and the
    predecessor's alternative -- skipping the call because the review was there --
    is what left a source-less figure with no caption for good.
    """
    asked = LOOK_BRIEF.format(language=language)
    fields = []
    if wants_review:
        asked += REVIEW_ASK
        fields.append(REVIEW_FIELD)
    if wants_caption:
        asked += CAPTION_ASK.format(limit=MAX_CAPTION_CHARS)
        fields.append(CAPTION_FIELD)
    return asked + REPLY_ASK.format(fields="{" + ", ".join(fields) + "}")


def _why_nothing_came_back(failure: str) -> str:
    """Whether the transport failed or the model answered with something unusable.

    Reported apart because they call for different next moves, and because a
    gateway that answered 503 must not be recorded as the model's fault. The
    failure arrives as this call's own rather than being read off the composer,
    which holds one field for every ask concurrently in flight.
    """
    return (
        f"inspection could not be reached: {failure}"
        if failure
        else "inspection replied with something that is not JSON"
    )


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
    for field in ("source_label", "caption", "visual_caption", "visual_review", "source_file", "source_page"):
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
    if entry.get("visual_review"):
        lines.append(f"  what looking at this render showed, which is not a caption: {entry['visual_review']}")
    if entry.get("concerns"):
        lines.append(f"  the extraction was unsure about: {', '.join(entry['concerns'])}")
    return "\n".join(lines)
