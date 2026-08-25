"""Trajectory report packing + pluggable upload backend.

A report is the shippable form of a trajectory: the redacted copy of a bundle
(:mod:`raven.trajectory.redact`) packed into a single ``.tar.gz`` whose root
directory is the attempt id. Where the tarball then goes is behind the
:class:`Uploader` protocol so a future HTTP backend slots in without touching
callers; v1 ships only the ``local`` backend, which delivers nothing — the
tarball on disk is the deliverable, handed over by the user.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any, Protocol


class Uploader(Protocol):
    """Delivers a finished report tarball somewhere.

    ``upload`` returns a human-readable destination (a path, later a URL).
    ``metadata`` is the redaction metadata dict, so a remote backend can send
    it alongside the tarball without re-reading the archive.
    """

    name: str

    def upload(self, tarball: Path, *, metadata: dict[str, Any]) -> str: ...


class LocalTarballUploader:
    """v1 backend: the tarball itself is the deliverable; nothing is sent."""

    name = "local"

    def upload(self, tarball: Path, *, metadata: dict[str, Any]) -> str:
        return str(tarball)


_UPLOADERS: dict[str, type] = {"local": LocalTarballUploader}


def get_uploader(name: str = "local") -> Uploader:
    """The upload backend registered under ``name``."""
    cls = _UPLOADERS.get(name)
    if cls is None:
        raise ValueError(f"unknown uploader {name!r}; available: {', '.join(sorted(_UPLOADERS))}")
    return cls()


def pack_report(redacted_dir: Path, out_file: Path) -> Path:
    """Pack a redacted bundle directory into ``out_file`` (``.tar.gz``).

    The archive root is the directory's name (the attempt id), so extracting
    reproduces the bundle layout.
    """
    redacted_dir = redacted_dir.resolve()
    if not redacted_dir.is_dir():
        raise ValueError(f"redacted directory not found: {redacted_dir}")
    out_file = out_file.resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_file, "w:gz") as tar:
        tar.add(redacted_dir, arcname=redacted_dir.name)
    return out_file


__all__ = ["LocalTarballUploader", "Uploader", "get_uploader", "pack_report"]
