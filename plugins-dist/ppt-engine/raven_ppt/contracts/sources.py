"""What ingestion found in the source material.

``SourceAsset`` is one figure or table cut out of the material, with the
provenance a citation needs. Its file sits in the project until the deck
ships.

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

from raven_ppt.contracts.findings import Finding

# (x0, y0, x1, y1) in PDF points on the source page, origin top-left -- the
# convention the PDF reader itself uses, kept unconverted so a bbox can be
# compared against the page it came from.
Rect = tuple[float, float, float, float]


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
    catalogue_path: Path
    assets: tuple[SourceAsset, ...] = ()
    source_files: tuple[str, ...] = ()
    page_count: int = 0
    text_chars: int = 0
    findings: tuple[Finding, ...] = field(default_factory=tuple)
