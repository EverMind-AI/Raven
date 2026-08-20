"""`.pptx` -> `.pdf` through LibreOffice headless.

This is the only link in the chain that cannot be done in-process, and it is the
one the rest of the capability leans on: the PDF is what the author looks at, what
the design pass looks at, and what measurement reads word boxes from. So the
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
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from raven.ppt.services.render import process
from raven.ppt.services.render.capabilities import find_soffice
from raven.ppt.services.render.errors import RenderError, RenderUnavailableError

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
        profile = Path(scratch) / "profile"
        staged = Path(scratch) / "out"
        profile.mkdir()
        staged.mkdir()
        command = [
            executable,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            # A profile with nothing in it cannot offer to recover a document
            # from a previous crash, which is the other way headless startup
            # blocks forever.
            "--norestore",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(staged),
            str(source.resolve()),
        ]
        completed = process.run(command, timeout_s=timeout_s, what="LibreOffice")
        produced = sorted(staged.glob("*.pdf"))
        if len(produced) != 1 or not produced[0].is_file():
            raise RenderError(
                f"LibreOffice did not produce a PDF for {source.name}",
                detail={"deck": str(source), "produced": [p.name for p in produced], **completed.log_tails},
            )
        target = destination / f"{source.stem}.pdf"
        shutil.move(str(produced[0]), str(target))
    return target
