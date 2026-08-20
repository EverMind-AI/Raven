"""Measurement fonts: the bundled ones are there, and an override takes.

Measurement is the reason these are bundled at all -- the same text has to come
out the same width on a dev machine and in an eval container, and a system font
that differs by a point release silently changes what fits on a page. So the
test that matters is that the packaged files exist and that resolution actually
reaches them rather than quietly landing on a system path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.assets.fonts import (
    _SYSTEM_CJK as SYSTEM_CJK,
)
from raven.ppt.services.assets.fonts import (
    BUNDLED_FONT_DIR,
    ENV_BOLD,
    ENV_CJK,
    ENV_REGULAR,
    MEASURED_SAFE_FONTS,
    measurement_fonts,
)


def test_the_measurement_faces_are_packaged_with_their_licence() -> None:
    for filename in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        packaged = BUNDLED_FONT_DIR / filename
        assert packaged.is_file(), f"{packaged} missing -- measurement would fall back to a system font"
        assert packaged.stat().st_size > 100_000
    licence = BUNDLED_FONT_DIR / "LICENSE-DejaVu.txt"
    assert licence.is_file()
    assert "Bitstream Vera" in licence.read_text(encoding="utf-8")


def test_resolution_prefers_the_packaged_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (ENV_REGULAR, ENV_BOLD, ENV_CJK):
        monkeypatch.delenv(variable, raising=False)
    resolved = measurement_fonts()
    assert Path(resolved.regular) == BUNDLED_FONT_DIR / "DejaVuSans.ttf"
    assert Path(resolved.bold) == BUNDLED_FONT_DIR / "DejaVuSans-Bold.ttf"
    # No CJK face is bundled -- one is 20MB and Han measures exactly one em, so the
    # full-em advance is correct without it. The system Noto CJK is used when the
    # renderer has it, because full-em is *not* correct for CJK punctuation, and a
    # deck in Chinese is most of this fork's traffic.
    assert resolved.cjk in ("", SYSTEM_CJK)
    assert (resolved.cjk == SYSTEM_CJK) is Path(SYSTEM_CJK).is_file()


def test_an_environment_override_wins_and_is_read_on_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolved per call, not at import, so a container can point it elsewhere."""
    monkeypatch.setenv(ENV_REGULAR, "/somewhere/Custom.ttf")
    monkeypatch.setenv(ENV_CJK, "/somewhere/Han.ttf")
    resolved = measurement_fonts()
    assert resolved.regular == "/somewhere/Custom.ttf"
    assert resolved.cjk == "/somewhere/Han.ttf"
    assert Path(resolved.bold) == BUNDLED_FONT_DIR / "DejaVuSans-Bold.ttf"
    # An explicit argument beats the environment.
    assert measurement_fonts(regular="/explicit.ttf").regular == "/explicit.ttf"


def test_the_nameable_faces_stay_a_short_measured_list() -> None:
    """Every entry has to resolve to a metric-compatible face both here and on
    the viewer's machine, or the review renders the model inspects lie."""
    assert set(MEASURED_SAFE_FONTS) == {
        "Arial",
        "Helvetica",
        "Times New Roman",
        "Cambria",
        "Century Schoolbook",
        "Bookman Old Style",
    }
    assert all(reason for reason in MEASURED_SAFE_FONTS.values())
    # DejaVu is what measurement loads and is deliberately not nameable: it is
    # absent from the viewer's machine, where the name resolves to a substitute.
    assert not any("DejaVu" in name for name in MEASURED_SAFE_FONTS)
