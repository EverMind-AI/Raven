"""What ingestion found in the source material.

Two shapes. ``SourceIndex`` is what every claim printed on a slide is checked
against; ``SourceAsset`` is one figure or table cut out of the material, with
the provenance a citation needs. They are separate because they have separate
lifetimes: the index is rebuilt whenever the materials change, while an
asset's file sits in the project until the deck ships.

The predecessor also carried a three-way ``usability`` label ("good" / "crop"
/ "avoid") derived from the same measurements. Nothing enforced it -- it was
printed into the catalogue the model reads, and the single gate that looked at
it only counted how many entries were not "avoid". It also misled: of five
assets labelled "avoid" on one paper, four were fragments of a figure that had
already been extracted whole (each fragment's bbox sits inside its), and
"crop" is the wrong instruction for a table, which must not be cropped. Both
halves are handled where they belong now -- a fragment is dropped during
extraction and never reaches the catalogue, and what remains is stated as
measured sentences in ``concerns`` that a model can read, act on, or overrule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from raven.ppt.contracts.findings import Finding

# (x0, y0, x1, y1) in PDF points on the source page, origin top-left -- the
# convention the PDF reader itself uses, kept unconverted so a bbox can be
# compared against the page it came from.
Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class SourceIndex:
    """Every number and spec-like name the materials state, normalised.

    The gate answers one question with this: did the material print this
    token? So both sides have to normalise identically, and the rules are
    part of the contract rather than an implementation detail:

    * numbers lose digit grouping and trailing zeros of a decimal, and gain
      their scale as an exponent: ``30,972`` -> ``30972``, ``31 billion`` ->
      ``31e9``, ``14%`` -> ``14%``, ``2.5x`` -> ``2.5x``, ``31.0`` -> ``31``.
      A scale stated once for a table ("in millions") applies to the bare
      numbers under it, and a scaled mention also registers its bare digits,
      so "31 billion" in the source anchors a slide that prints "31".
    * names are upper-cased: ``gpt-4o`` and ``GPT-4o`` are one entry.
    * ``caps_phrases`` holds every 2..6 word window of the material,
      lower-cased, which is how an all-caps phrase on a slide ("STATE OF THE
      ART") is recognised as ordinary language rather than a spec name.

    ``numbers`` holds everything the ingested text prints, including the page
    anchors the ingest itself minted, because a slide's provenance line
    ("Fig. 4, p.7") is a claim the gate sees too. ``stated_numbers`` excludes
    those structural headings, and is what a *data value* has to trace to: a
    chart bar of height 17 must not be justified by "page 17".
    """

    numbers: frozenset[str] = frozenset()
    stated_numbers: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    caps_phrases: frozenset[str] = frozenset()
    # How many characters the material stated, headings excluded -- the same
    # distinction ``stated_numbers`` draws. It answers "was anything read at
    # all?", which the sets cannot: a scan yields no text, and so does a
    # document of pure prose with no digits in it, and only one of those two
    # should stand the gate down. ``None`` means an index written before this
    # was recorded, and it leaves the gate armed.
    stated_chars: int | None = None


class AssetKind(Enum):
    """What kind of thing an asset is, which decides how it may be used.

    A table must never be cropped -- half a table is a misquote, not a detail
    shot -- while a figure often has to be, so the distinction has to survive
    ingestion rather than being re-guessed from the file name.
    """

    FIGURE = "figure"
    TABLE = "table"
    IMAGE = "image"


@dataclass(frozen=True)
class SourceAsset:
    """One extracted figure, table or supplied image.

    ``page_coverage`` is the fraction of its source page the asset covers, and
    ``0.0`` means the question does not apply: a supplied image file and a
    block cut out of a page screenshot are not regions of a page.
    """

    asset_id: str
    path: Path
    kind: AssetKind
    width_px: int
    height_px: int
    page_coverage: float = 0.0
    panel_count: int = 1
    concerns: tuple[str, ...] = ()
    source_file: str | None = None
    source_page: int | None = None
    source_url: str | None = None
    # The label its caption gave it ("Figure 4"), and the caption itself. A
    # deck whose figures arrive unlabelled has nothing tying one to the claim
    # it is evidence for: pages cited "Fig. 4" while showing Fig. 5, and that
    # is not catchable after the fact.
    source_label: str | None = None
    caption: str | None = None
    source_bbox: Rect | None = None

    @property
    def aspect(self) -> float:
        return self.width_px / self.height_px if self.height_px else 0.0


@dataclass(frozen=True)
class IngestOutcome:
    """Everything one ingestion run produced."""

    materials_path: Path
    index_path: Path
    catalogue_path: Path
    index: SourceIndex
    assets: tuple[SourceAsset, ...] = ()
    source_files: tuple[str, ...] = ()
    page_count: int = 0
    text_chars: int = 0
    findings: tuple[Finding, ...] = field(default_factory=tuple)
