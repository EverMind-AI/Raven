"""What a Raven node runs: this Raven's own source, and the digest that names it.

A node runs the host's code exactly, so the two answer a file call alike. The
code travels as the ``raven`` package directory itself -- the same files
whether this Raven came from a release or a checkout -- and the digest of those
files is how both ends tell they hold the same code: the host computes it
before installing, the node computes it again over what arrived, and
``node/hello`` compares the two. A version number alone could not: two
checkouts of one version can differ.

Standard library only; the node imports this too.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from functools import lru_cache
from pathlib import Path

_SKIPPED_DIRS = frozenset({"__pycache__"})
_SKIPPED_SUFFIXES = (".pyc", ".pyo")


def package_dir() -> Path:
    """The ``raven`` package directory this process imports from."""
    import raven

    return Path(raven.__file__).resolve().parent


def files(root: Path | None = None) -> list[Path]:
    """Every file a node needs, relative to the package directory, in a fixed order.

    Bytecode is left out: each Python writes its own, so it would make two
    copies of one source differ.
    """
    base = root or package_dir()
    out: list[Path] = []
    for path in base.rglob("*"):
        rel = path.relative_to(base)
        if any(part in _SKIPPED_DIRS for part in rel.parts) or path.suffix in _SKIPPED_SUFFIXES:
            continue
        if path.is_file():
            out.append(rel)
    return sorted(out, key=lambda p: p.as_posix())


def tree_digest(root: Path) -> str:
    """A short digest of the files' names and contents under ``root``; equal on two machines iff the code is."""
    hasher = hashlib.sha256()
    for rel in files(root):
        hasher.update(rel.as_posix().encode("utf-8") + b"\0")
        hasher.update((root / rel).read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()[:12]


@lru_cache(maxsize=1)
def digest() -> str:
    """:func:`tree_digest` of this process's own package, worked out once: the code it runs does not change."""
    return tree_digest(package_dir())


def install_name() -> str:
    """The directory name a node of this code is installed under: version, then digest.

    Not a "node id": that names a delegated task's address (``node_id`` on
    ``spawn`` and DAG nodes), and this names an install.
    """
    from raven import __version__

    return f"{__version__}-{digest()}"


def tarball(root: Path | None = None) -> bytes:
    """The package as a gzipped tar, every file under ``raven/``, ready to unpack into a node's ``lib``."""
    base = root or package_dir()
    buffer = io.BytesIO()
    # Dereferenced, so a link in the checkout arrives as the file it points at
    # and the node, which has only what was sent, digests the same bytes.
    with tarfile.open(fileobj=buffer, mode="w:gz", dereference=True) as archive:
        for rel in files(base):
            archive.add(base / rel, arcname=f"raven/{rel.as_posix()}", recursive=False)
    return buffer.getvalue()


def pinned(names: tuple[str, ...]) -> list[str]:
    """``name==version`` for each package as installed here, or the bare name when it is not."""
    from importlib.metadata import PackageNotFoundError, version

    out = []
    for name in names:
        try:
            out.append(f"{name}=={version(name)}")
        except PackageNotFoundError:
            out.append(name)
    return out


__all__ = ["digest", "files", "install_name", "package_dir", "pinned", "tarball", "tree_digest"]
