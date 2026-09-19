"""Unit tests for the legacy Word parser: convert first, then read as docx."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from raven.knowledge.parser.doc_parser import LegacyDocParser


def _parse(payload: bytes | str, filename: str = "notice.doc"):
    return asyncio.run(LegacyDocParser().parse(payload, filename))


def test_the_parser_claims_only_the_legacy_word_type() -> None:
    assert LegacyDocParser.supported_media_types == ["application/msword"]
    assert LegacyDocParser.supported_extensions() == [".doc"]


def test_without_libreoffice_it_says_what_to_install(monkeypatch) -> None:
    """A document a reader can see in the panel and cannot search needs a
    reason they can act on, not 'no parser'."""
    monkeypatch.setattr("raven.utils.office.find_soffice", lambda: None)

    with pytest.raises(ValueError, match="LibreOffice"):
        _parse(b"\\xd0\\xcf\\x11\\xe0 legacy word")


def test_a_missing_path_is_reported_as_missing(monkeypatch) -> None:
    monkeypatch.setattr("raven.utils.office.find_soffice", lambda: "/usr/bin/soffice")

    with pytest.raises(FileNotFoundError):
        _parse("/nonexistent/notice.doc")


def test_a_conversion_that_produced_nothing_is_not_silent(monkeypatch) -> None:
    """LibreOffice exits 0 having written nothing often enough -- a corrupt
    file, a password-protected one -- that a caller trusting the exit code
    would index an empty document and call it ready."""
    from raven.utils import office

    monkeypatch.setattr(office, "find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        office,
        "convert",
        lambda *a, **k: office.Converted(produced=[], returncode=0, stdout="", stderr="no filter"),
    )

    with pytest.raises(ValueError, match="could not convert"):
        _parse(b"not really a doc")


def test_a_conversion_failure_is_reported_against_the_file(monkeypatch) -> None:
    from raven.utils import office

    monkeypatch.setattr(office, "find_soffice", lambda: "/usr/bin/soffice")

    def _boom(*a, **k):
        raise TimeoutError("soffice hung")

    monkeypatch.setattr(office, "convert", _boom)

    with pytest.raises(ValueError, match="notice.doc"):
        _parse(b"anything")


def test_the_converted_document_is_read_as_a_docx(monkeypatch, tmp_path) -> None:
    """The point of the whole arrangement: what comes back is what DocxParser
    makes of the conversion, positions and all."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_knowledge_parser_docx import _docx, _p

    from raven.utils import office

    converted = tmp_path / "source.docx"
    converted.write_bytes(_docx(_p("Terms", style="Heading1") + _p("The body of it.")))

    monkeypatch.setattr(office, "find_soffice", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(
        office,
        "convert",
        lambda *a, **k: office.Converted(produced=[converted], returncode=0, stdout="", stderr=""),
    )

    sections = _parse(b"a legacy doc")

    assert [s.metadata.get("heading") for s in sections] == ["Terms"]
    assert "The body of it." in sections[0].content.text
    assert sections[0].source == "notice.doc", "named for the file the reader uploaded"


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice is not installed here")
def test_a_real_conversion_round_trip(tmp_path) -> None:
    """Against the real converter where there is one: the stubs above prove the
    wiring, and only this proves the argv."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_knowledge_parser_docx import _docx, _p

    from raven.utils import office

    # LibreOffice reads a .docx happily, so the round trip does not need a real
    # legacy file to prove the conversion runs and its output parses.
    source = tmp_path / "source.docx"
    source.write_bytes(_docx(_p("Heading", style="Heading1") + _p("Body text here.")))
    staged = tmp_path / "out"
    staged.mkdir()

    done = office.convert(source, staged, executable=office.find_soffice(), timeout_s=120.0, target="docx")

    assert done.produced, f"soffice wrote nothing: {done.stderr[-200:]}"
