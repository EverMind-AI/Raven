"""Reaching a format this package cannot read through one it can.

LibreOffice is already on the machine for the viewer, which renders an office
file to show it. The same run converts a format with no reader here into one
with a reader: a legacy ``.doc`` becomes ``.docx``, a legacy ``.xls`` becomes
``.xlsx``. Both parsers want the same four things -- a temp file to convert
from, a run off the event loop, a reason when the converter is missing, and a
refusal when it produced nothing -- so those are here rather than in each.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from loguru import logger

#: Long enough for a large document on a cold LibreOffice, short enough that a
#: conversion which will never finish does not hold an indexing run open.
TIMEOUT_S = 120.0


async def converted(
    file: bytes | str, filename: str, *, target: str, suffix: str, timeout_s: float = TIMEOUT_S
) -> bytes:
    """``file`` converted to ``target``, as bytes.

    Args:
        file (`bytes | str`): The payload, or a path to it.
        filename (`str`): What the reader uploaded, for the error messages.
        target (`str`): LibreOffice's own filter name -- ``docx``, ``xlsx``.
        suffix (`str`): What to call the source on disk. LibreOffice decides
            what it is reading from the extension, and a file named for the
            wrong format converts to nothing.
        timeout_s (`float`): How long one conversion may take.

    Raises:
        `FileNotFoundError`: If ``file`` is a path that does not exist.
        `ValueError`: If LibreOffice is missing, the run failed, or it produced
            nothing -- which it does, exiting 0, for a corrupt or
            password-protected file.
    """
    from raven.utils.office import find_soffice, install_hint, to_pdf

    executable = find_soffice()
    if executable is None:
        raise ValueError(f"{filename!r} needs LibreOffice to read: {install_hint()}")

    with tempfile.TemporaryDirectory(prefix="raven-office-") as scratch:
        room = Path(scratch)
        source = room / f"source{suffix}"
        if isinstance(file, str):
            if not os.path.isfile(file):
                raise FileNotFoundError(f"{filename!r}: no such file: {file!r}")
            source.write_bytes(Path(file).read_bytes())
        else:
            source.write_bytes(file)

        staged = room / "out"
        staged.mkdir()
        # Off the event loop: LibreOffice is a subprocess that takes seconds,
        # and indexing runs in the gateway process, which is also answering the
        # page that is watching the document's status.
        try:
            # `to_pdf` is the general converter despite its name: `fmt` is the
            # LibreOffice export filter, and this asks it for `.docx` and
            # `.xlsx` rather than PDF.
            done = await asyncio.to_thread(
                to_pdf, source, staged, executable=executable, timeout_s=timeout_s, fmt=target
            )
        except (OSError, TimeoutError) as exc:
            raise ValueError(f"Failed to convert {filename!r} with LibreOffice: {exc}") from exc

        if not done.produced:
            logger.debug("knowledge: soffice said {!r} / {!r}", done.stdout[-400:], done.stderr[-400:])
            raise ValueError(
                f"LibreOffice could not convert {filename!r} (exit {done.returncode}); "
                "the file may be corrupt or password-protected"
            )
        return done.produced[0].read_bytes()
