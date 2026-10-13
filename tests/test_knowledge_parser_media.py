"""The media types the parsers are routed by, on a host that knows none of them.

`mimetypes` answers from the machine. A developer's carries a full Apache
table; a slim container image carries no table at all, and Python's built-in
one predates OOXML. These tests are written against the second, because the
first is what hid the bug.
"""

from __future__ import annotations

import mimetypes

import pytest

from raven.knowledge.parser._media import MEDIA_TYPES, register


@pytest.fixture
def bare_host(monkeypatch: pytest.MonkeyPatch):
    """A process whose host supplies no mime table at all.

    What the Raven image is: `mimetypes.knownfiles` names half a dozen paths
    and a slim Debian carries none of them, so the built-in table is the whole
    of what `guess_type` knows.
    """
    monkeypatch.setattr(mimetypes, "knownfiles", [])
    monkeypatch.setattr(mimetypes, "_db", None, raising=False)
    mimetypes.init([])
    yield
    monkeypatch.setattr(mimetypes, "_db", None, raising=False)
    mimetypes.init()


#: The four the built-in table has no answer for. `.doc` and `.ppt` are in it
#: and were never the problem, which is why only some uploads failed.
_OOXML = (".docx", ".xlsx", ".pptx")


class TestWithoutRegistering:
    """The failure, stated so the fix below is not tautological."""

    def test_the_bare_host_cannot_name_an_ooxml_file(self, bare_host) -> None:
        assert [mimetypes.guess_type(f"x{suffix}")[0] for suffix in _OOXML] == [None, None, None]

    def test_but_it_can_name_their_predecessors(self, bare_host) -> None:
        """Which is why a .ppt indexed and a .pptx did not."""
        assert mimetypes.guess_type("x.ppt")[0] == "application/vnd.ms-powerpoint"
        assert mimetypes.guess_type("x.doc")[0] == "application/msword"


class TestRegistering:
    def test_every_format_this_package_parses_resolves(self, bare_host) -> None:
        register()

        for suffix, media_type in MEDIA_TYPES:
            assert mimetypes.guess_type(f"x{suffix}")[0] == media_type, suffix

    def test_a_word_file_is_routed_to_the_word_parser(self, bare_host) -> None:
        """The whole point. Unregistered it guessed None, the manager fell back
        to text/plain, and the text parser decoded a zip container as UTF-8 --
        which the reader saw as "codec can't decode byte 0x91" against a file
        nothing is wrong with."""
        register()

        from raven.knowledge.parser.docx_parser import DocxParser

        guessed = mimetypes.guess_type("report.docx")[0]
        assert guessed in DocxParser.supported_media_types

    def test_the_file_picker_offers_them_too(self, bare_host) -> None:
        """`supported_extensions` reverses the same table, so the formats
        missing from it were missing from the upload dialog's accept list."""
        register()

        from raven.knowledge.parser.docx_parser import DocxParser
        from raven.knowledge.parser.ppt_parser import PptParser

        assert ".docx" in DocxParser.supported_extensions()
        assert ".pptx" in PptParser.supported_extensions()

    def test_it_can_be_called_twice(self, bare_host) -> None:
        """It runs at import, and an import can happen more than once in a
        process that reloads a module."""
        register()
        register()

        assert mimetypes.guess_type("x.docx")[0] == dict(MEDIA_TYPES)[".docx"]

    def test_it_does_not_overrule_a_host_that_already_knows(self, bare_host) -> None:
        """Additive, not corrective: where the host has an answer it is the
        same answer, so this supplies rather than replaces."""
        mimetypes.add_type("application/pdf", ".pdf")

        register()

        assert mimetypes.guess_type("x.pdf")[0] == "application/pdf"
