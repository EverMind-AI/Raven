"""`.pptx` -> `.pdf` through LibreOffice headless.

This is the only link in the chain that cannot be done in-process, and it is the
one the rest of the capability leans on: the PDF is what the author looks at and
what measurement reads word boxes from. So the
failure modes are worth spelling out, because every one of them was observed
rather than guessed.

**A shared user profile silently loses a conversion.** LibreOffice keeps its
settings in a profile directory and allows one instance per profile; a second
invocation against the same profile hands its request to the running instance and
exits 0, having written nothing. Measured on this machine: two concurrent
`--convert-to pdf` calls sharing one profile produced one PDF, no error text, and
an empty output directory for the loser. Two decks rendering at once is not exotic
-- the review stage renders while the author builds -- so every call gets a fresh
profile of its own. It also costs nothing to throw away afterwards: a cold profile
adds well under a second to a conversion that takes seconds anyway.

**Exit 0 is not proof of output.** Follows from the above, and from LibreOffice
reporting success for documents it could not load. The only reliable check is
whether the PDF appeared, so that is what this does.

**stderr is not proof of failure.** A stock container prints `failed to launch
javaldx - java may not function correctly` on every single run. Treating a
non-empty stderr as an error would fail every conversion.

**The output cannot simply be renamed into place.** `--convert-to` names its
output after the input's stem, so it is produced in a private directory and moved
-- with `shutil.move`, not `Path.replace`, because the private directory is under
the system temp (tmpfs here) and the destination is on the project's disk, and a
rename across devices raises EXDEV.

The spawn itself is not here any more. Building the argv, giving the run a
profile of its own and killing the tree on a timeout are the same three things
the host's viewer needs to show a deck, and they were written twice; they live in
`raven.utils.office` now and this module calls them. What stayed is what is the
engine's: the budget above, the four failure modes below, the rule that only the
file appearing counts as success, and the three errors the rest of the package
catches. The engine gains the Windows teardown that copy had and this one did not.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from raven.utils import office as soffice_run
from raven_ppt.services.render.capabilities import find_soffice
from raven_ppt.services.render.errors import (
    LOG_TAIL_CHARS,
    RenderError,
    RenderTimeoutError,
    RenderUnavailableError,
)

# A deck of forty image-heavy pages converts in about half a minute on a cold
# profile; three minutes is a hang, not a slow deck.
DEFAULT_CONVERT_TIMEOUT_S = 180.0


def to_pdf(
    pptx: Path,
    out_dir: Path,
    *,
    soffice: str | None = None,
    timeout_s: float = DEFAULT_CONVERT_TIMEOUT_S,
) -> Path:
    """Convert `pptx` and return the PDF, written as `out_dir/<stem>.pdf`.

    Raises `RenderUnavailableError` when LibreOffice is not installed, `RenderTimeoutError`
    when it hangs, and `RenderError` when it ran but produced no single PDF.
    """
    source = Path(pptx)
    if not source.is_file():
        raise RenderError(f"there is no deck to convert at {source}")
    executable = soffice or find_soffice()
    if executable is None:
        raise RenderUnavailableError(
            "LibreOffice is not installed, so a deck cannot be turned into a PDF; "
            "without it the deck can still be built, but not viewed or measured"
        )
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="raven-ppt-soffice-") as scratch:
        staged = Path(scratch) / "out"
        staged.mkdir()
        try:
            done = soffice_run.to_pdf(source, staged, executable=executable, timeout_s=timeout_s)
        except TimeoutError as exc:
            raise RenderTimeoutError(f"LibreOffice exceeded its {timeout_s:g}s budget and was killed") from exc
        except FileNotFoundError as exc:
            raise RenderUnavailableError(f"LibreOffice is not installed: {executable!r} could not be executed") from exc
        except OSError as exc:
            raise RenderError(f"LibreOffice could not be started: {exc}") from exc
        if len(done.produced) != 1 or not done.produced[0].is_file():
            raise RenderError(
                f"LibreOffice did not produce a PDF for {source.name}",
                detail={
                    "deck": str(source),
                    "produced": [p.name for p in done.produced],
                    "exit_code": done.returncode,
                    "stdout": done.stdout.strip()[-LOG_TAIL_CHARS:],
                    "stderr": done.stderr.strip()[-LOG_TAIL_CHARS:],
                },
            )
        target = destination / f"{source.stem}.pdf"
        shutil.move(str(done.produced[0]), str(target))
    return target
