"""Copy an agent folder (and its engine project) into the run directory before checking it.

Every layer reads the copy, so what is checked is what runs. Build output
(``__pycache__`` directories and ``*.pyc`` files) is never part of a package; it is
left out of the copy and listed, because a working checkout always carries some.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    source: Path
    root: Path
    engine: Path | None
    digest: str
    build_output: tuple[str, ...]


def _is_build_output(name: str) -> bool:
    return name == "__pycache__" or name.endswith(".pyc")


def _build_output(folder: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        base = Path(dirpath)
        for name in [*dirnames, *filenames]:
            if _is_build_output(name):
                path = base / name
                if path.is_dir() and not path.is_symlink():
                    found.extend(
                        (Path(d) / f).relative_to(folder).as_posix() for d, _, files in os.walk(path) for f in files
                    )
                else:
                    found.append(path.relative_to(folder).as_posix())
        dirnames[:] = [d for d in dirnames if not _is_build_output(d)]
    return sorted(found)


def _copy(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        symlinks=True,
        ignore=lambda _dir, names: [n for n in names if _is_build_output(n)],
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    entries: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        entries.extend(base / name for name in filenames)
        entries.extend(base / name for name in dirnames if (base / name).is_symlink())
    for path in sorted(entries, key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"->" + os.readlink(path).encode("utf-8"))
        else:
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def take_snapshot(folder: Path, run_dir: Path, *, engine: Path | None) -> Snapshot:
    source = Path(folder).resolve()
    root = Path(run_dir) / "package" / source.name
    _copy(source, root)
    engine_copy = None
    if engine is not None:
        engine_source = Path(engine).resolve()
        engine_copy = Path(run_dir) / "engine" / engine_source.name
        _copy(engine_source, engine_copy)
    return Snapshot(
        source=source,
        root=root,
        engine=engine_copy,
        digest=tree_digest(root),
        build_output=tuple(_build_output(source)),
    )
