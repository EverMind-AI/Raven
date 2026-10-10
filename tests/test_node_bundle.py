"""raven.node.bundle: the code a Raven node runs, and the digest that names it.

The digest is how a host and a node tell they hold the same code, so these pin
what it covers -- every file's name and bytes, not the bytecode each Python
writes for itself -- and that what is sent unpacks to the same digest on the
other side.
"""

from __future__ import annotations

import io
import tarfile
from importlib.metadata import version
from pathlib import Path

import pytest

from raven.node import bundle


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    return _tree(
        tmp_path / "raven",
        {
            "__init__.py": b"x = 1\n",
            "sub/mod.py": b"y = 2\n",
            "sub/data.json": b"{}\n",
            "sub/__pycache__/mod.cpython-312.pyc": b"\x00bytecode",
            "stale.pyc": b"\x00",
            "old.pyo": b"\x00",
        },
    )


def test_the_files_are_the_source_in_a_fixed_order_without_bytecode(pkg):
    assert [p.as_posix() for p in bundle.files(pkg)] == ["__init__.py", "sub/data.json", "sub/mod.py"]


def test_the_digest_follows_the_code_and_nothing_else(pkg):
    first = bundle.tree_digest(pkg)
    assert len(first) == 12 and first == bundle.tree_digest(pkg)

    (pkg / "sub" / "__pycache__" / "other.cpython-313.pyc").write_bytes(b"another Python's bytecode")
    assert bundle.tree_digest(pkg) == first

    (pkg / "sub" / "mod.py").write_bytes(b"y = 3\n")
    edited = bundle.tree_digest(pkg)
    assert edited != first

    (pkg / "sub" / "mod.py").rename(pkg / "sub" / "moved.py")
    assert bundle.tree_digest(pkg) != edited, "a file's name is part of the code"


def test_two_trees_whose_bytes_only_line_up_differently_do_not_share_a_digest(tmp_path):
    # Each name and each file's bytes are closed off, so moving a boundary --
    # bytes from one file into the next, or from a name into its contents --
    # never reads as the same code.
    assert bundle.tree_digest(_tree(tmp_path / "a", {"a": b"x", "b": b"y"})) != bundle.tree_digest(
        _tree(tmp_path / "b", {"a": b"xb\x00y"})
    )
    assert bundle.tree_digest(_tree(tmp_path / "c", {"a": b"bc"})) != bundle.tree_digest(
        _tree(tmp_path / "d", {"ab": b"c"})
    )


def test_what_is_sent_unpacks_under_raven_to_the_same_digest(pkg, tmp_path):
    (pkg / "linked.py").symlink_to(pkg / "sub" / "mod.py")

    data = bundle.tarball(pkg)

    out = tmp_path / "unpacked"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = archive.getnames()
        archive.extractall(out, filter="data")
    assert sorted(names) == ["raven/__init__.py", "raven/linked.py", "raven/sub/data.json", "raven/sub/mod.py"]
    assert not (out / "raven" / "linked.py").is_symlink(), "the node gets the file a link points at"
    assert bundle.tree_digest(out / "raven") == bundle.tree_digest(pkg)


def test_this_raven_is_named_by_its_version_and_the_digest_of_its_own_package():
    from raven import __version__

    assert bundle.package_dir().joinpath("node", "bundle.py").is_file()
    assert bundle.digest() == bundle.tree_digest(bundle.package_dir())
    assert bundle.install_name() == f"{__version__}-{bundle.digest()}"
    names = {p.as_posix() for p in bundle.files()}
    assert {"__init__.py", "node/server.py", "agent/tools/filesystem.py"} <= names
    assert not any(name.endswith((".pyc", ".pyo")) or "__pycache__" in name for name in names)


def test_requirements_are_pinned_to_the_versions_this_raven_runs_with():
    assert bundle.pinned(("loguru", "raven-no-such-package")) == [
        f"loguru=={version('loguru')}",
        "raven-no-such-package",
    ]
