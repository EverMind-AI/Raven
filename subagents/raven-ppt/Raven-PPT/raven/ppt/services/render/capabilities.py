"""What this machine can actually do, asked before anything is promised.

The chain has one required link and two that come in pairs, and the difference
matters to a caller: without LibreOffice there is no PDF and therefore nothing to
look at or measure, while a missing PDFium only means the fallback binary does
the work. A tool that reports "previews unavailable" for the first case and says
nothing for the second is the goal, and it needs one answer object to do it.

The probe imports PDFium rather than only looking for it on the module path,
because a wheel whose bundled library cannot load is exactly the failure this
report exists to catch, and `find_spec` says yes to it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import cache
from types import ModuleType


@dataclass(frozen=True)
class RenderCapabilities:
    """Which parts of the pptx -> pdf -> png chain are installed."""

    soffice: str | None = None
    pdfium: bool = False
    pdftoppm: str | None = None
    pdftotext: str | None = None
    pillow: bool = False

    @property
    def can_convert(self) -> bool:
        """pptx -> pdf. Nothing downstream happens without this."""
        return self.soffice is not None

    @property
    def can_rasterise(self) -> bool:
        """pdf -> png, through PDFium or poppler."""
        return self.pdfium or self.pdftoppm is not None

    @property
    def can_measure_words(self) -> bool:
        """Word boxes off the rendered page, through PDFium or poppler."""
        return self.pdfium or self.pdftotext is not None

    @property
    def can_contact_sheet(self) -> bool:
        return self.pillow

    def missing(self) -> tuple[str, ...]:
        """What to install, named the way it is installed."""
        gaps: list[str] = []
        if not self.can_convert:
            gaps.append("libreoffice")
        if not self.can_rasterise:
            gaps.append("pypdfium2 (pip install 'raven[ppt]') or poppler-utils")
        if not self.can_measure_words:
            gaps.append("pypdfium2 (pip install 'raven[ppt]') or poppler-utils")
        if not self.can_contact_sheet:
            gaps.append("pillow")
        return tuple(dict.fromkeys(gaps))

    def explain(self) -> str:
        """One line, usable as a tool message or a test's skip reason."""
        gaps = self.missing()
        return "the render chain is complete" if not gaps else "missing: " + ", ".join(gaps)


def available(*, soffice: str | None = None) -> RenderCapabilities:
    """Probe the chain. `soffice` overrides discovery with a configured path."""
    return RenderCapabilities(
        soffice=soffice or find_soffice(),
        pdfium=pdfium() is not None,
        pdftoppm=shutil.which("pdftoppm"),
        pdftotext=shutil.which("pdftotext"),
        pillow=_pillow_present(),
    )


def find_soffice() -> str | None:
    """LibreOffice's launcher, under either of the two names it ships as."""
    return shutil.which("soffice") or shutil.which("libreoffice")


@cache
def pdfium() -> ModuleType | None:
    """The `pypdfium2` module, or None when it is absent or will not load.

    Cached because importing it maps a native library, and both rasterisation and
    word extraction ask on every call.
    """
    try:
        import pypdfium2
    except Exception:  # pragma: no cover - depends on the wheel, not on us
        return None
    return pypdfium2


def _pillow_present() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except Exception:  # pragma: no cover - Pillow is a hard dependency of raven
        return False
    return True
