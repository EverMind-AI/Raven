"""Which files count as material, how they become text, and whether they changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from raven.ppt.services.ingest.documents import (
    TEXT_LAYER_CHARS_PER_PAGE,
    discover,
    image_asset_id,
    read_text_source,
    source_urls,
    text_density,
    unreadable,
)


def test_discovery_walks_subdirectories_and_names_what_it_cannot_read(tmp_path: Path) -> None:
    """An office document is a source -- it is the likeliest thing anyone hands over
    for a deck, and it used to be skipped in silence. What genuinely cannot be read is
    listed rather than dropped, because a user's file missing from the evidence with
    nothing said about it is how one went missing."""
    (tmp_path / "deep").mkdir()
    for name in ("a.pdf", "b.md", "deep/c.txt", "deep/d.png", "notes.docx", "sheet.xlsx", "archive.zip"):
        (tmp_path / name).write_bytes(b"x")

    assert [path.name for path in discover(tmp_path)] == [
        "a.pdf",
        "b.md",
        "c.txt",
        "d.png",
        "notes.docx",
        "sheet.xlsx",
    ]
    assert [path.name for path in unreadable(tmp_path)] == ["archive.zip"]


def test_html_is_reduced_to_what_a_reader_would_see(tmp_path: Path) -> None:
    """Script and style bodies are not material: indexing them would put
    numbers into the material that no reader of the page ever saw."""
    path = tmp_path / "report.html"
    path.write_text(
        "<html><style>.x{width:999px}</style><script>bad=999</script>"
        "<body><h1>Market update</h1><p>Revenue reached 42 million in 2026.</p></body></html>",
        encoding="utf-8",
    )

    text = read_text_source(path)

    assert "Revenue reached 42 million in 2026." in text
    assert "Market update" in text
    assert "999" not in text


def test_html_headings_survive_as_headings(tmp_path: Path) -> None:
    """HTML is the one source format that states its own structure, and flattening it
    threw that away: `sections` saw a single entry for the whole document, the same
    blindness a .txt has, self-inflicted. h1 and h2 land beside the "## [f.pdf] page N"
    the PDF path writes, one level under the "# Source:" line.
    """
    path = tmp_path / "report.html"
    path.write_text(
        "<html><body><h1>2026 report</h1><p>Opening.</p>"
        "<h2>Revenue</h2><p>42 million.</p>"
        "<h3>North America</h3><p>18 million.</p></body></html>",
        encoding="utf-8",
    )

    lines = read_text_source(path).splitlines()

    assert "## 2026 report" in lines
    assert "## Revenue" in lines
    assert "### North America" in lines
    assert "42 million." in lines


def test_a_plain_text_source_is_passed_through(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("Launch year 2024.\n", encoding="utf-8")
    assert read_text_source(path) == "Launch year 2024.\n"


def test_text_density_excludes_the_headings_the_ingest_wrote() -> None:
    markdown = "# Source: scan.pdf\n\n## [scan.pdf] page 1\n\n\n## [scan.pdf] page 2\n\n\n"
    chars, pages = text_density(markdown)
    assert (chars, pages) == (0, 2)
    assert chars < TEXT_LAYER_CHARS_PER_PAGE * pages

    body = "## [report.pdf] page 1\n\n" + "x" * 200
    assert text_density(body) == (200, 1)


def test_a_downloaded_source_keeps_its_url_when_the_bytes_still_match(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"payload")
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "url": "https://example.com/source.png",
                "final_url": "https://cdn.example.com/source.png",
                "path": str(source),
                "content_sha256": hashlib.sha256(b"payload").hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # The redirect target wins: it is where the bytes actually came from.
    assert source_urls(manifest, [source]) == {source.resolve(): "https://cdn.example.com/source.png"}


def test_a_source_that_changed_since_it_was_fetched_fails_the_ingest(tmp_path: Path) -> None:
    """Attribution is the only claim a web asset carries; a slide crediting a
    source is crediting this record."""
    source = tmp_path / "source.png"
    source.write_bytes(b"replaced")
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(
        json.dumps({"url": "https://example.com/x.png", "path": str(source), "content_sha256": "0" * 64}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        source_urls(manifest, [source])


def test_manifest_noise_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"payload")
    manifest = tmp_path / "sources.jsonl"
    manifest.write_text(
        "not json\n"
        + json.dumps(["not", "a", "record"])
        + "\n"
        + json.dumps({"url": "https://example.com/other.png", "path": str(tmp_path / "absent.png")})
        + "\n",
        encoding="utf-8",
    )

    assert source_urls(manifest, [source]) == {}
    assert source_urls(tmp_path / "missing.jsonl", [source]) == {}


def test_two_images_with_one_stem_get_distinct_ids(tmp_path: Path) -> None:
    """A collision means the second image silently replaces the first."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = tmp_path / "a" / "same.png"
    second = tmp_path / "b" / "same.png"
    for path in (first, second):
        path.write_bytes(b"x")

    assert image_asset_id(first, tmp_path) != image_asset_id(second, tmp_path)
    assert image_asset_id(first, tmp_path).startswith("same-")
    # Stable across runs, so a re-ingest does not rename every figure.
    assert image_asset_id(first, tmp_path) == image_asset_id(first, tmp_path)


def test_an_unusable_stem_still_yields_a_referenceable_id(tmp_path: Path) -> None:
    path = tmp_path / "图 表 ①.png"
    path.write_bytes(b"x")
    asset_id = image_asset_id(path, tmp_path)
    assert asset_id.startswith("image-")
    assert asset_id.replace("-", "").isalnum()
