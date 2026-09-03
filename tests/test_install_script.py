"""Tripwire for the root installer's uv argv discipline.

install.sh builds uv option pairs in scalar variables and expands them
unquoted (POSIX sh has no arrays), so a requirement carried that way must
stay a single word: word splitting hands uv each space-separated piece as
its own argument, uv exits 2, and the retry ladder silently degrades to a
bare install with the memory plugin and channel extras dropped. The quoted
positional requirement legitimately keeps the spaced PEP 508 form, so the
spaced spelling looks natural and keeps getting reintroduced -- this pin
turns that red in CI instead.
"""

from __future__ import annotations

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"


def test_the_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_SH.is_file()


def test_the_everos_with_requirement_stays_one_word() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "everos-memory@$everos_url" in text
    assert "everos-memory @ " not in text
