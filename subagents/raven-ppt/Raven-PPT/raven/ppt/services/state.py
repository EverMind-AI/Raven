"""What a deck has to work with, read off disk.

Counting, and only counting. Whether a set of materials is *enough* to make the
deck the user asked for is a judgement about content against intent, and nothing
here can make it -- that is the intake pass's question, and this is the input it
gets handed. The division is deliberate, because the two are not the same kind of
claim: a count cannot be argued with, so it is what a check binds to; a judgement
can, so it is recorded and acted on rather than enforced.

Cheap on purpose -- a few `is_file` calls and two small JSON reads -- because it
is asked on every build and again on every intake.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from raven.ppt.contracts import DeckBrief, Project, brief_path, load_brief
from raven.ppt.services.ingest import CATALOGUE_FILE, MATERIALS_FILE, READ_FILE
from raven.ppt.services.template import PREPARED_FILE, BoundTemplate, bound

# How much of the materials the intake pass is shown, and in how many pieces.
# Enough to tell what these documents are about; not so much that its question
# turns into "read this paper".
#
# In pieces because `materials.md` is a concatenation: the head of it is the head
# of the *first* document, so a deck built from twenty papers was judged on the
# opening of one of them. Evenly spaced windows are not a summary, but they are a
# sample, and the question being asked is "what is missing" rather than "what does
# this say".
EXCERPT_CHARS = 4500
EXCERPT_WINDOWS = 3

# How many figures are listed by name. Past this the list stops being read.
MAX_LISTED_FIGURES = 40

# How each of a figure's two captions is introduced, and the line that says which
# is which. Two claims of different kinds: the source's caption is what the author
# of the figure printed under it, and the inspected one is a sentence a model wrote
# by looking at the pixels. `caption or visual_caption` collapsed them into one
# string, so the second reached the page, and the checks that argue from a figure,
# wearing the first's authority -- a marketing banner arrived captioned as the
# architecture of the system it advertises, and the page credited it.
SOURCE_CAPTION_LEAD = "source caption"
INSPECTED_CAPTION_LEAD = "looks like"
CAPTION_LEGEND = (
    f'"{SOURCE_CAPTION_LEAD}" is what the source printed under the figure; '
    f'"{INSPECTED_CAPTION_LEAD}" is what inspection saw in the pixels, '
    "not the source's words -- describe it, never quote or credit it as one"
)


@dataclass(frozen=True)
class Figure:
    """One extracted visual, as the intake pass needs to judge it.

    The label and the caption are the load-bearing fields: "figure 7, 1200x800"
    says nothing about whether this deck has evidence for what it claims, and
    "Figure 3: ablation on memory length" says everything.

    `caption` and `visual_caption` are both captions and are not the same claim --
    see `CAPTION_LEGEND`. Nothing here presents one as the other.
    """

    figure_id: str
    kind: str
    label: str = ""
    caption: str = ""
    visual_caption: str = ""
    file: str = ""
    width_px: int = 0
    height_px: int = 0
    concerns: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        said = self.label or self.kind
        size = f", {self.width_px}x{self.height_px}px" if self.width_px and self.height_px else ""
        concern = f"; {self.concerns[0][:100]}" if self.concerns else ""
        return f"{self.figure_id} ({said}{size}){self.captions()}{concern}"

    def captions(self) -> str:
        """Both captions this figure carries, each attributed to whoever wrote it.

        Kept apart in the text a reader is handed and not only in the fields they
        are stored in, which is where the distinction was being thrown away: a
        model composing a page reads this line, and one string cannot say which
        half of it a page may quote.
        """
        said = []
        if self.caption:
            said.append(f'{SOURCE_CAPTION_LEAD}: "{self.caption[:90]}"')
        if self.visual_caption:
            said.append(f"{INSPECTED_CAPTION_LEAD}: {self.visual_caption[:90]}")
        return "; " + "; ".join(said) if said else ""


@dataclass(frozen=True)
class DeckState:
    """Everything counted about one deck's inputs."""

    project: Project
    brief: DeckBrief | None = None
    template: BoundTemplate | None = None
    unbound_templates: tuple[str, ...] = field(default_factory=tuple)
    """`.pptx` files sitting in the workspace that no deck is built in.

    Worth counting because a user who drops their house style into the workspace
    and says "use this" has done everything they can, and a run that never calls
    `ppt_template` builds a white deck beside their template without either
    party noticing.
    """
    sources: tuple[str, ...] = field(default_factory=tuple)
    figures: tuple[Figure, ...] = field(default_factory=tuple)
    materials_chars: int = 0
    excerpt: str = ""
    has_index: bool = False
    script: bool = False
    deck: Path | None = None

    @property
    def ingested(self) -> bool:
        return self.has_index and bool(self.sources)

    def summary(self) -> str:
        """The state as a reader takes it in, for a prompt that has no schema."""
        lines = [
            f"Brief: {self.brief.summary() if self.brief else 'not recorded yet'}",
            f"Template: {self.template.source.name if self.template else 'none'}",
        ]
        if self.unbound_templates and not self.template:
            lines.append(f"Unbound .pptx in the workspace: {', '.join(self.unbound_templates)}")
        if self.sources:
            lines.append(f"Sources ingested ({len(self.sources)}): {', '.join(self.sources)}")
            lines.append(f"Materials: {self.materials_chars} characters of text")
        else:
            lines.append("Sources ingested: none")
        if self.figures:
            listed = self.figures[:MAX_LISTED_FIGURES]
            lines.append(f"Figures extracted ({len(self.figures)}):")
            if any(figure.visual_caption for figure in listed):
                lines.append(f"  ({CAPTION_LEGEND})")
            lines += [f"  {figure.summary()}" for figure in listed]
            if len(self.figures) > len(listed):
                lines.append(f"  ... and {len(self.figures) - len(listed)} more")
        else:
            lines.append("Figures extracted: none")
        return "\n".join(lines)

    def digest(self) -> str:
        """A fingerprint of the inputs, so a recorded judgement can go stale.

        What a reading of the task depends on, and nothing else: add a document or
        a figure and the reading has to be made again; rebuild the deck twenty times
        and it does not. Bound to the *inputs* rather than to a timestamp, so
        re-reading an unchanged project is free.

        The brief is deliberately not in it, though it is a fact about the deck. It
        is downstream of the reading rather than an input to it, and including it
        cost a live run a 140-second model call to learn nothing: the reading told
        the author to record the brief, recording it changed the digest, and the
        author did as it was told and paid for a second reading of the same request.
        """
        material = json.dumps(
            {
                "sources": sorted(self.sources),
                "figures": sorted(
                    (
                        figure.figure_id,
                        figure.label,
                        figure.caption,
                        figure.visual_caption,
                    )
                    for figure in self.figures
                ),
                "characters": self.materials_chars,
                "template": self.template.source.name if self.template else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def read(project: Project) -> DeckState:
    """Count what this deck has. Never raises: an absence is an answer."""
    read_record = _json(project.ingest_dir / READ_FILE)
    materials = _text(project.ingest_dir / MATERIALS_FILE)
    script = project.build_dir / "build.py"
    deck = project.build_dir / "deck.pptx"
    return DeckState(
        project=project,
        brief=load_brief(brief_path(project)),
        template=bound(project),
        unbound_templates=_loose_templates(project),
        sources=tuple(str(name) for name in (read_record or {}).get("sources") or ()),
        figures=_figures(_json(project.ingest_dir / CATALOGUE_FILE)),
        materials_chars=len(materials),
        excerpt=_sampled(materials),
        has_index=read_record is not None,
        script=script.is_file(),
        deck=deck if deck.is_file() else None,
    )


def _sampled(materials: str) -> str:
    """The head of the materials and a window from further in, marked as excerpts."""
    if len(materials) <= EXCERPT_CHARS:
        return materials
    width = EXCERPT_CHARS // EXCERPT_WINDOWS
    step = len(materials) // EXCERPT_WINDOWS
    pieces = []
    for index in range(EXCERPT_WINDOWS):
        at = index * step
        pieces.append(f"[characters {at}-{at + width} of {len(materials)}]\n{materials[at : at + width]}")
    return "\n\n…\n\n".join(pieces)


def _figures(catalogue: dict | None) -> tuple[Figure, ...]:
    entries = (catalogue or {}).get("assets") or {}
    return tuple(
        Figure(
            figure_id=str(figure_id),
            kind=str(entry.get("kind") or "figure"),
            label=str(entry.get("source_label") or ""),
            caption=str(entry.get("caption") or ""),
            visual_caption=str(entry.get("visual_caption") or ""),
            file=str(entry.get("file") or ""),
            width_px=int(entry.get("width_px") or 0),
            height_px=int(entry.get("height_px") or 0),
            concerns=tuple(str(item) for item in entry.get("concerns") or ()),
        )
        for figure_id, entry in sorted(entries.items())
        if isinstance(entry, dict)
    )


def _loose_templates(project: Project) -> tuple[str, ...]:
    """`.pptx` in the workspace that is not a deck this pipeline made.

    Depth-limited rather than a full walk: this is read on every build, and a
    workspace with a checkout in it would make `rglob` the expensive part of an
    otherwise free call. Two levels is where a user drops a file.
    """
    workspace = project.workspace
    skip = {"deck", "out"}
    found: list[str] = []
    for pattern in ("*.pptx", "*/*.pptx"):
        for path in sorted(workspace.glob(pattern)):
            if path.name.startswith("~$") or set(path.relative_to(workspace).parts) & skip:
                continue
            if path.name == PREPARED_FILE:
                continue
            found.append(str(path.relative_to(workspace)))
    return tuple(found)


def _json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
