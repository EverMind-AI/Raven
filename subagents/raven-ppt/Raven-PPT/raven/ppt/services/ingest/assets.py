"""Describing an extracted asset, and dropping the ones that are fragments.

Two jobs, both replacing a single label. The predecessor graded every figure
"good" / "crop" / "avoid" from the measurements below and printed the grade
into the catalogue the model reads. It did not survive contact with real
material:

* nothing enforced it -- the one gate that read the grade only counted how many
  entries were not "avoid", so a figure graded "avoid" could still be placed,
  and a run that ignored the grade entirely was never stopped;
* it was often wrong about *why*. Of five assets graded "avoid" on one paper,
  four were pieces of a figure that had already been extracted whole -- each
  one's bbox sits inside its -- so the honest answer was not "avoid this" but
  "this is not a separate figure";
* "crop" is the wrong instruction for a table. Half a table is a misquote.

So a fragment is dropped here, by the geometry that proves it is one, and what
survives carries sentences instead of a grade: what was measured, and what that
costs on a slide. A model can act on those, argue with them, or place the
figure anyway -- which it could always do, only now the reasoning is visible.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from raven.ppt.contracts.sources import AssetKind, SourceAsset
from raven.ppt.services.ingest.geometry import as_tuple, contained_in, rect
from raven.ppt.services.ingest.images import (
    DETAIL_INK_MAX,
    contain_scale,
    estimate_panels,
    ink_ratio,
    interior_ink_ratio,
)

# Below either bound a figure cannot carry a slide at any size.
_MIN_W_PX = 200
_MIN_H_PX = 140
_MIN_PAGE_COVERAGE = 0.01
_MIN_INTERIOR_INK = 0.004
# Beyond this a figure no longer fits a slot beside text, though a page of its
# own still carries it: observed on four paper decks whose 4-5:1 figures the
# planner placed anyway -- and was right to.
_MAX_ASPECT = 3.5
_MIN_ASPECT = 0.28
# Past this there is no layout at all: the 6.1:1 bands a web-print export
# slices a page into sit above it, a wide architecture diagram below.
_STRIP_ASPECT = 6.0
_PAGE_SCREENSHOT_COVERAGE = 0.6
_COMPOSITE_PANELS = 4
# A half-page figure area on a 1280x720 board, less its padding: what a figure
# has to survive when it shares a page with text. Nominal on purpose -- no
# backend's slot geometry is visible from here, and the point is the order of
# magnitude at which in-figure text stops being readable.
_FIGURE_AREA = (508.0, 360.0)
# A paper figure rendered at 150dpi shows an 8pt axis label at roughly
# 1.56*scale pt on the slide, so anything under ~0.8 puts it below 10pt -- the
# point where a projector loses it. A photograph has no labels to lose and
# survives much further down.
_DETAIL_MIN_SCALE = 0.8
_PHOTO_MIN_SCALE = 0.45

SCHEMA = "raven.ppt.assets.v1"


@dataclass(frozen=True)
class Measurements:
    """What was measured about one asset's pixels."""

    width_px: int
    height_px: int
    page_coverage: float
    panel_count: int
    ink_ratio: float = 1.0
    interior_ink_ratio: float = 1.0

    @property
    def aspect(self) -> float:
        return self.width_px / self.height_px if self.height_px else 0.0


def measure(path: Path, size: tuple[int, int], coverage: float, *, panel_count: int | None = None) -> Measurements:
    """Read the pixels of one extracted asset.

    ``panel_count`` is given for an asset whose panels are already known -- a
    table has one region however many rows it prints, and counting ink bands in
    one would report its rows as panels.
    """
    width, height = size
    return Measurements(
        width_px=int(width),
        height_px=int(height),
        page_coverage=coverage,
        panel_count=estimate_panels(path) if panel_count is None else panel_count,
        ink_ratio=ink_ratio(path),
        interior_ink_ratio=interior_ink_ratio(path),
    )


def concerns(measured: Measurements, kind: AssetKind) -> tuple[str, ...]:
    """Every reason this asset may be hard to use, as sentences.

    Ordered worst first, and every applicable one is stated rather than the
    first: a 6:1 band that also covers its whole page has two problems, and the
    predecessor's chain reported one of them, which read as though the other
    had been checked and passed.
    """
    found: list[str] = []
    width, height = measured.width_px, measured.height_px
    aspect = measured.aspect
    coverage = measured.page_coverage
    if width < _MIN_W_PX or height < _MIN_H_PX:
        found.append(
            f"{width}x{height}px is small for a slide — at that size it is more likely an icon, a rule or a "
            "spacer than a figure"
        )
    # Coverage is 0.0 for anything that is not a region of a page: a supplied
    # image file, or a block cut out of a screenshot.
    if 0 < coverage < _MIN_PAGE_COVERAGE:
        found.append(f"covers only {coverage:.1%} of its source page — the footprint of an icon or a page decoration")
    if measured.interior_ink_ratio < _MIN_INTERIOR_INK:
        found.append("the interior is blank: only an outer frame or a background band was extracted")
    if aspect > _STRIP_ASPECT or (aspect and aspect < 1 / _STRIP_ASPECT):
        found.append(f"aspect {aspect:.1f}:1 — a banner or a narrow strip; no slide layout carries that shape")
    elif aspect > _MAX_ASPECT or (aspect and aspect < _MIN_ASPECT):
        shape = "wide" if aspect > _MAX_ASPECT else "tall"
        found.append(
            f"aspect {aspect:.1f}:1 — too {shape} for a slot beside text, though a page of its own carries it "
            "at full size"
        )
    if coverage >= _PAGE_SCREENSHOT_COVERAGE:
        found.append(
            f"covers {coverage:.0%} of its source page — a whole-page screenshot, so in a slot beside text its "
            "own words are unreadable"
        )
    if kind is not AssetKind.TABLE and measured.panel_count >= _COMPOSITE_PANELS:
        found.append(
            f"about {measured.panel_count} panels in one figure — the single panel a page cites reads far "
            "better than the composite"
        )
    found.extend(_scale_concerns(measured, kind))
    return tuple(found)


def _scale_concerns(measured: Measurements, kind: AssetKind) -> list[str]:
    scale = contain_scale((measured.width_px, measured.height_px), _FIGURE_AREA)
    if not 0.0 < scale < _DETAIL_MIN_SCALE:
        return []
    detail = kind is AssetKind.TABLE or measured.ink_ratio < DETAIL_INK_MAX
    if detail:
        subject = "a table" if kind is AssetKind.TABLE else "line art"
        return [
            f"{subject} at {measured.width_px}x{measured.height_px}px renders at {scale:.0%} in a figure area "
            "beside text, which puts its own 8pt type under 10pt"
        ]
    if scale < _PHOTO_MIN_SCALE:
        # Nothing here can tell a product photograph (fine shrunk) from a UI
        # screenshot (whose captions vanish), so state the scale and let
        # whoever can see the image decide.
        return [
            f"renders at {scale:.0%} in a figure area beside text — fine for a photograph, thin for anything "
            "carrying text or interface detail"
        ]
    return []


def build(
    asset_id: str,
    path: Path,
    measured: Measurements,
    *,
    kind: AssetKind,
    source_file: str | None = None,
    source_page: int | None = None,
    source_url: str | None = None,
    source_label: str | None = None,
    caption: str | None = None,
    source_bbox=None,
) -> SourceAsset:
    return SourceAsset(
        asset_id=asset_id,
        path=path,
        kind=kind,
        width_px=measured.width_px,
        height_px=measured.height_px,
        page_coverage=round(measured.page_coverage, 3),
        panel_count=measured.panel_count,
        concerns=concerns(measured, kind),
        source_file=source_file,
        source_page=source_page,
        source_url=source_url,
        source_label=source_label,
        caption=caption,
        source_bbox=None if source_bbox is None else as_tuple(source_bbox),
    )


def is_page_screenshot(asset: SourceAsset) -> bool:
    """A whole source page, extracted as one bitmap.

    Its own content blocks are what a slide can carry, so this is what triggers
    cutting them out.
    """
    return asset.page_coverage >= _PAGE_SCREENSHOT_COVERAGE


def noting(asset: SourceAsset, sentence: str) -> SourceAsset:
    """``asset`` with one more concern appended."""
    return replace(asset, concerns=(*asset.concerns, sentence))


def drop_fragments(assets: list[SourceAsset]) -> tuple[list[SourceAsset], list[SourceAsset]]:
    """Split ``assets`` into the ones worth keeping and the pieces of them.

    Four readers look at the same page -- embedded bitmaps, multi-image
    clusters, sliced stacks, drawing clusters -- and they overlap: the axis
    labels a paper draws as vectors beside a printed figure come back as a
    drawing cluster of their own, sitting inside the region already rendered
    for that figure. Emitted separately they are junk with a caption's worth of
    ink in them, and the predecessor's answer was to grade them "avoid" and
    hand them over anyway.

    An asset is a fragment when another asset on the same page contains it and
    no caption named it. The label is the veto: a figure whose caption the page
    printed is citable evidence, so it survives even when it sits inside a
    bigger region, while an unlabelled piece of a labelled whole never was a
    figure. Files for dropped assets are deleted -- left behind they are
    orphans that a figure listing would still find.
    """
    kept: list[SourceAsset] = []
    dropped: list[SourceAsset] = []
    for page in _by_page(assets):
        # Labelled first, then largest: whichever survives a tie is the one a
        # page can cite, and failing that the one holding the whole subject.
        for asset in sorted(page, key=_precedence):
            if asset.source_label is not None or not _inside_one_of(asset, kept):
                kept.append(asset)
            else:
                dropped.append(asset)
                asset.path.unlink(missing_ok=True)
    # Assets with no page region -- supplied images, blocks cut out of a
    # screenshot -- cannot be fragments of anything.
    kept.extend(asset for asset in assets if asset.source_bbox is None)
    order = {asset.asset_id: index for index, asset in enumerate(assets)}
    kept.sort(key=lambda asset: order[asset.asset_id])
    return kept, dropped


def _by_page(assets: list[SourceAsset]) -> list[list[SourceAsset]]:
    pages: dict[tuple[str | None, int | None], list[SourceAsset]] = {}
    for asset in assets:
        if asset.source_bbox is not None:
            pages.setdefault((asset.source_file, asset.source_page), []).append(asset)
    return list(pages.values())


def _inside_one_of(asset: SourceAsset, kept: list[SourceAsset]) -> bool:
    box = rect(asset.source_bbox)
    return any(
        other.source_bbox is not None
        and (other.source_file, other.source_page) == (asset.source_file, asset.source_page)
        and contained_in(box, rect(other.source_bbox))
        for other in kept
    )


def _precedence(asset: SourceAsset) -> tuple[int, float]:
    return (0 if asset.source_label is not None else 1, -rect(asset.source_bbox).get_area())


def write_catalogue(assets: list[SourceAsset], path: Path) -> None:
    """Persist the catalogue, keyed by asset id.

    The file records the asset's own name rather than its path: a project can be
    moved, and the figures directory is the one place these files live.
    """
    payload = {}
    for asset in sorted(assets, key=lambda item: item.asset_id):
        entry = {key: value for key, value in asdict(asset).items() if key not in ("asset_id", "path", "kind")}
        entry["file"] = asset.path.name
        entry["kind"] = asset.kind.value
        entry["concerns"] = list(asset.concerns)
        payload[asset.asset_id] = entry
    path.write_text(
        json.dumps({"schema": SCHEMA, "assets": payload}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def load_catalogue(path: Path, *, figures_dir: Path) -> dict[str, SourceAsset]:
    """Rehydrate a catalogue written by :func:`write_catalogue`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("assets")
    if not isinstance(entries, dict):
        return {}
    assets: dict[str, SourceAsset] = {}
    for asset_id, entry in entries.items():
        if not isinstance(asset_id, str) or not isinstance(entry, dict):
            continue
        bbox = entry.get("source_bbox")
        assets[asset_id] = SourceAsset(
            asset_id=asset_id,
            path=figures_dir / str(entry.get("file") or asset_id),
            kind=AssetKind(entry.get("kind", AssetKind.FIGURE.value)),
            width_px=int(entry.get("width_px", 0)),
            height_px=int(entry.get("height_px", 0)),
            page_coverage=float(entry.get("page_coverage", 0.0)),
            panel_count=int(entry.get("panel_count", 1)),
            concerns=tuple(entry.get("concerns", ())),
            source_file=entry.get("source_file"),
            source_page=entry.get("source_page"),
            source_url=entry.get("source_url"),
            source_label=entry.get("source_label"),
            caption=entry.get("caption"),
            source_bbox=tuple(bbox) if isinstance(bbox, list) and len(bbox) == 4 else None,
        )
    return assets
