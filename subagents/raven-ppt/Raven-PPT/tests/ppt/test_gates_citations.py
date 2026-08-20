"""A page citing one figure while showing another is a provenance error.

Every number on such a page is real and every entity is whitelisted, so no other
gate can see it -- and a reader checks it in a second.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from raven.ppt.contracts.findings import Audience, Severity
from raven.ppt.services.gates.citations import (
    citation_findings,
    cited_labels,
    figure_labels,
    load_figure_catalog,
)
from tests.ppt.conftest import DeckBuilder

pytest.importorskip("pptx")


@pytest.fixture
def figures(tmp_path: Path, image) -> Path:
    """An ingest directory holding two captioned figures."""
    directory = tmp_path / "figures"
    directory.mkdir()
    for name, colour in (("fig_one.png", (200, 40, 40)), ("fig_two.png", (40, 60, 200))):
        (directory / name).write_bytes(image(name, colour).read_bytes())
    (tmp_path / "figures.json").write_text(
        json.dumps(
            {
                "schema": "raven.ppt.assets.v1",
                "assets": {
                    "fig_one": {"source_label": "Figure 4", "caption": "Figure 4. Qualitative results."},
                    "fig_two": {"source_label": "Figure 5", "caption": "Figure 5. Two tasks in one pass."},
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def labels(figures: Path) -> dict[str, str]:
    return figure_labels(figures, load_figure_catalog(figures.parent / "figures.json"))


def _deck(deck: DeckBuilder, text: str, figure: Path | None, name: str = "deck.pptx") -> Path:
    page = deck.page()
    deck.text(page, (text, 18.0), height=1.0)
    if figure is not None:
        deck.picture(page, figure)
    return deck.save(name)


def test_showing_five_while_citing_four_is_refused(deck: DeckBuilder, figures: Path, labels) -> None:
    built = _deck(deck, "Qualitative evidence — Fig. 4, p.7", figures / "fig_two.png")

    findings = citation_findings(built, labels)

    assert len(findings) == 1
    assert findings[0].page == 1
    assert findings[0].kind == "citation"
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].audience is Audience.AUTHOR
    assert "shows Figure 5 but cites Figure 4" in findings[0].message
    assert findings[0].detail["disagreement"] == ("Figure 4", "Figure 5")


def test_the_right_pairing_reports_nothing(deck: DeckBuilder, figures: Path, labels) -> None:
    built = _deck(deck, "Qualitative evidence — Fig. 4, p.7", figures / "fig_one.png")

    assert citation_findings(built, labels) == []


def test_a_figure_the_page_never_names_is_left_alone(deck: DeckBuilder, figures: Path, labels) -> None:
    """Showing a figure without numbering it is a design choice, not an error."""
    built = _deck(deck, "One model, four tasks", figures / "fig_two.png")

    assert citation_findings(built, labels) == []


def test_prose_naming_a_figure_the_page_does_not_show_is_left_alone(deck: DeckBuilder, labels) -> None:
    """It may be prose about the paper. Only a page doing both can contradict
    itself."""
    built = _deck(deck, "As Fig. 4 showed, targets become queries", None)

    assert citation_findings(built, labels) == []


def test_a_page_citing_from_two_boxes_is_reported_once(deck: DeckBuilder, figures: Path, labels) -> None:
    """One contradiction, one finding, per page and per kind.

    Naming it from every text box that mentions a figure sends the author looking
    for several fixes to one mistake.
    """
    page = deck.page()
    deck.text(page, ("Qualitative evidence — Fig. 4", 18.0), top=0.4, height=0.8)
    deck.text(page, ("Fig. 4 shows the failure case", 18.0), top=6.4, height=0.8)
    deck.picture(page, figures / "fig_two.png")

    assert len(citation_findings(deck.save(), labels)) == 1


def test_an_image_this_deck_never_ingested_goes_unchecked(deck: DeckBuilder, image, labels) -> None:
    """The gate refuses a deck, so it may only refuse on evidence: an image whose
    bytes match nothing ingested says nothing about which figure it is."""
    built = _deck(deck, "Qualitative evidence — Fig. 4", image("stranger.png", (7, 7, 7)))

    assert citation_findings(built, labels) == []


def test_a_deck_with_no_captioned_figures_is_not_gated(deck: DeckBuilder, figures: Path) -> None:
    built = _deck(deck, "Qualitative evidence — Fig. 4", figures / "fig_two.png")

    assert citation_findings(built, {}) == []


def test_figures_and_tables_are_gated_apart(deck: DeckBuilder, figures: Path, labels) -> None:
    """A page citing a table while showing a figure is not a contradiction: the
    table it names is not something the figure catalogue knows about."""
    built = _deck(deck, "Results — Table 2 summarises", figures / "fig_two.png")

    assert citation_findings(built, labels) == []


def test_the_citation_pattern_reads_how_a_page_writes_it() -> None:
    assert cited_labels("Qualitative evidence — Fig. 4, p.7") == {"Figure 4"}
    assert cited_labels("see Figure 5 and Figures 6") == {"Figure 5", "Figure 6"}
    assert cited_labels("Table 1 and 表 2 and 图 3") == {"Table 1", "Table 2", "Figure 3"}
    assert cited_labels("Fig. A1 and Fig. 4.2 and Table IV") == {"Figure A1", "Figure 4.2", "Table IV"}
    assert cited_labels("configure the transfiguration") == set()


def test_labels_are_keyed_by_the_bytes_of_the_file(figures: Path, labels) -> None:
    """What lands on a page is bytes: a build program may copy or rename a
    figure, and only the content survives all of it."""
    assert set(labels.values()) == {"Figure 4", "Figure 5"}
    copied = figures / "renamed.png"
    copied.write_bytes((figures / "fig_one.png").read_bytes())

    assert set(figure_labels(figures, load_figure_catalog(figures.parent / "figures.json")).values()) == {
        "Figure 4",
        "Figure 5",
    }


def test_an_absent_or_broken_catalogue_is_no_catalogue(tmp_path: Path) -> None:
    """Absent, unreadable and malformed all mean the same thing: nothing known."""
    assert load_figure_catalog(tmp_path / "missing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_figure_catalog(broken) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"assets": []}), encoding="utf-8")
    assert load_figure_catalog(wrong) == {}


def test_a_figure_without_a_caption_label_is_not_indexed(tmp_path: Path, image) -> None:
    directory = tmp_path / "figures"
    directory.mkdir()
    (directory / "fig_one.png").write_bytes(image("fig_one.png", (1, 2, 3)).read_bytes())

    assert figure_labels(directory, {"fig_one": {"caption": "no label here"}}) == {}
    assert figure_labels(tmp_path / "absent", {}) == {}


def test_the_gate_reads_the_catalogue_the_ingest_writes(tmp_path: Path, image) -> None:
    """Through the real writer, because the two drifted apart on a key name.

    The gate read `figures` while the ingest wrote `assets`, so every catalogue came
    back empty and the citation gate reported nothing on any deck ever built. Both
    sides had tests; the gate's fixtures hand-wrote the shape the gate expected.
    """
    from raven.ppt.contracts.sources import AssetKind, SourceAsset
    from raven.ppt.services.ingest.assets import write_catalogue

    figures = tmp_path / "figures"
    figures.mkdir()
    path = figures / "fig_one.png"
    path.write_bytes(image("fig_one.png", (200, 40, 40)).read_bytes())
    catalogue = tmp_path / "figures.json"
    write_catalogue(
        [
            SourceAsset(
                asset_id="fig_one",
                path=path,
                kind=AssetKind.FIGURE,
                width_px=800,
                height_px=600,
                source_label="Figure 4",
            )
        ],
        catalogue,
    )

    loaded = load_figure_catalog(catalogue)
    assert list(loaded) == ["fig_one"]
    assert figure_labels(figures, loaded) == {hashlib.sha256(path.read_bytes()).hexdigest(): "Figure 4"}
