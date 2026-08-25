"""The citation gate: a page cites the figure it actually shows.

A page saying "Fig. 4" beside Figure 5 is a provenance error a reader checks in
a second and nothing else here looks for. It went unnoticed until ingest started
reading captions --
before that nothing knew which figure was which, and the author could only guess
from the picture.

Which figure is on a page is decided by hashing the placed image against the
ingested files, so a page placing something this deck never ingested, or an
image a build program rebuilt on the way, simply goes unchecked. That is the
right failure: the gate refuses a deck, and it may only refuse on evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import iter_shapes, page_paragraphs, pages

# `Fig. 4`, `Figure 4`, `Table 1`, `图 3` as a page's own text writes them.
_CITATION_RE = re.compile(r"(?i)\b(fig(?:ure)?s?\.?|tables?|图|表)\s*([A-Z]?\d+(?:[.-]\d+)?|[IVXLC]+)\b")

# What ingest writes figures out as. Declared here rather than imported so the
# gate can be run against a directory of images without the ingest service.
FIGURE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})


def load_figure_catalog(path: Path) -> dict[str, dict[str, object]]:
    """The `figures.json` ingest wrote, or {} when there is none to read.

    The key is `assets`, which is what the writer uses. This read `figures` and so
    came back empty for every deck ever built, which took the citation gate with it:
    an empty catalogue yields no labels, no labels means no page shows a figure, and
    a gate that sees no figures anywhere reports nothing. Declared here rather than
    imported so the gate can run against a directory of images with no ingest
    service, and that independence is exactly how the two drifted -- so the shape is
    asserted against the writer in tests/ppt/test_gates_citations.py.

    Absent, unreadable and malformed all mean the same thing here: nothing is
    known about which figure is which, so the gate has nothing to check and says
    nothing.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    figures = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(figures, dict):
        return {}
    return {
        figure_id: dict(metadata)
        for figure_id, metadata in figures.items()
        if isinstance(figure_id, str) and isinstance(metadata, dict)
    }


def figure_labels(figures_dir: Path, catalog: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """sha256 of each ingested figure file -> the label its caption gave it.

    Hashed rather than matched by filename because what lands on a page is
    bytes: the build program may copy, rename or re-save a figure, and the only
    thing that survives all three is the content.
    """
    if not figures_dir.is_dir():
        return {}
    labels: dict[str, str] = {}
    for path in sorted(figures_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in FIGURE_SUFFIXES:
            continue
        label = (catalog.get(path.stem) or {}).get("source_label")
        if not isinstance(label, str) or not label:
            continue
        try:
            labels[hashlib.sha256(path.read_bytes()).hexdigest()] = label
        except OSError:
            continue
    return labels


def cited_labels(text: str) -> set[str]:
    """The figures and tables a piece of copy names."""
    labels: set[str] = set()
    for word, number in _CITATION_RE.findall(text):
        kind = "Table" if word.lower().startswith(("table", "表")) else "Figure"
        labels.add(f"{kind} {number}")
    return labels


def shown_labels(slide: Any, labels: Mapping[str, str]) -> set[str]:
    """The figures a page actually places, by the bytes of the placed image."""
    shown: set[str] = set()
    for shape in iter_shapes(slide.shapes):
        image = getattr(shape, "image", None)
        if image is None:
            continue
        try:
            digest = hashlib.sha256(image.blob).hexdigest()
        except (AttributeError, OSError, ValueError):
            continue
        label = labels.get(digest)
        if label:
            shown.add(label)
    return shown


def citation_findings(pptx_path: Path, labels: Mapping[str, str]) -> list[Finding]:
    """Pages whose figure citations do not match the figure they show."""
    if not labels:
        return []
    findings: list[Finding] = []
    for number, slide in pages(pptx_path):
        shown = shown_labels(slide, labels)
        if not shown:
            continue
        cited: set[str] = set()
        for text in page_paragraphs(slide):
            cited |= cited_labels(text)
        for kind in ("Figure", "Table"):
            on_page = {label for label in shown if label.startswith(kind)}
            named = {label for label in cited if label.startswith(kind)}
            if not on_page or not named:
                # A page that shows a figure without naming it is a design
                # choice; a page naming one it does not show may be prose about
                # the paper. Only a page doing both can contradict itself.
                continue
            if on_page == named:
                continue
            # One contradiction, one finding, per page and per kind: naming it
            # from both sides at once reads as two problems and sends the author
            # looking for two fixes.
            findings.append(
                Finding(
                    kind="citation",
                    severity=Severity.BLOCKING,
                    page=number,
                    audience=Audience.AUTHOR,
                    message=(
                        f"the page shows {', '.join(sorted(on_page))} but cites {', '.join(sorted(named))}. "
                        "Cite what is on the page, or place what the page cites"
                    ),
                    detail={
                        "shown": tuple(sorted(on_page)),
                        "cited": tuple(sorted(named)),
                        "disagreement": tuple(sorted(on_page ^ named)),
                    },
                )
            )
    return findings
