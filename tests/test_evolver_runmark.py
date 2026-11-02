"""Unit tests for the evolver trial-identity markers (runmark).

A trial is only considered finished when the runner that observed it records a
completion marker, and the marker carries a digest of the harness tree that
produced it.
"""

from __future__ import annotations

from pathlib import Path

from evolver.runmark import MARK, VERSION, fingerprint, is_complete, mark_complete, read_mark


def _tree(tmp_path: Path, *, mixed_case: bool = False) -> Path:
    d = tmp_path / "harness"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.py").write_text("x = 1\n")
    if mixed_case:
        (d / "README.md").write_text("r\n")
        (d / "agent.py").write_text("a\n")
    return d


def test_fingerprint_none_is_base():
    assert fingerprint(None) == "base"


def test_fingerprint_absent_dir(tmp_path: Path):
    assert fingerprint(tmp_path / "absent") == "absent"


def test_fingerprint_stable_and_ignores_byproducts(tmp_path: Path):
    d = _tree(tmp_path)
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (d / ".DS_Store").write_text("noise")
    (d / f"{MARK}.tmp").write_text("leftover\n")
    h1 = fingerprint(d)
    # byproducts must not affect the digest
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"changed")
    (d / ".DS_Store").write_text("changed")
    (d / f"{MARK}.tmp").write_text("changed\n")
    assert fingerprint(d) == h1
    # a real source change does change it
    (d / "sub" / "a.py").write_text("x = 2\n")
    assert fingerprint(d) != h1


def test_fingerprint_ignores_git_metadata(tmp_path: Path):
    d = _tree(tmp_path)
    h1 = fingerprint(d)
    # a worktree's .git is a file pointing at a per-worktree path
    (d / ".git").write_text("gitdir: /some/worktree/path\n")
    assert fingerprint(d) == h1
    # a normal checkout's .git is a directory holding mutable metadata
    (d / ".git").unlink()
    (d / ".git").mkdir()
    (d / ".git" / "index").write_text("noise\n")
    assert fingerprint(d) == h1


def test_fingerprint_orders_names_platform_independently(tmp_path: Path):
    # Pinned digest for README.md, agent.py, sub/x.py hashed in POSIX-name
    # order. A case-folding sort (Windows Path ordering) puts agent.py first
    # and produces a different digest. Write bytes explicitly: Path.write_text
    # translates "\n" to "\r\n" on Windows, and fingerprint hashes raw bytes.
    d = tmp_path / "harness"
    d.mkdir()
    (d / "README.md").write_bytes(b"r\n")
    (d / "agent.py").write_bytes(b"a\n")
    (d / "sub").mkdir()
    (d / "sub" / "x.py").write_bytes(b"x\n")
    assert fingerprint(d) == "1d0448a1475ad04c"


def test_mark_complete_writes_marker(tmp_path: Path):
    where = tmp_path / "trial"
    path = mark_complete(where, run_id="run-1", extra={"foo": "bar"})
    assert path == where / MARK
    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "run-1"
    assert data["version"] == VERSION
    assert data["foo"] == "bar"
    assert data["harness"] == "base"


def test_is_complete_requires_matching_run_id(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    assert is_complete(where, run_id="run-1") is True
    assert is_complete(where, run_id="run-2") is False
    assert is_complete(where) is True


def test_is_complete_requires_expected_metadata(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1", extra={"split": "train", "k": 3, "n_tasks": 2})
    assert is_complete(where, run_id="run-1", expected={"k": 3, "n_tasks": 2}) is True
    assert is_complete(where, run_id="run-1", expected={"k": 1}) is False
    assert is_complete(where, run_id="run-1", expected={"split": "test"}) is False
    assert is_complete(where, run_id="run-1", expected={"n_tasks": 3}) is False


def test_is_complete_none_harness_is_base_not_wildcard(tmp_path: Path):
    where = tmp_path / "trial"
    harness = _tree(tmp_path)
    mark_complete(where, run_id="run-1", harness_dir=harness)
    # a cwd-less check is for the inherited harness, not any harness
    assert is_complete(where, run_id="run-1") is False
    assert is_complete(where, run_id="run-1", harness_dir=harness) is True


def test_is_complete_rejects_stale_harness(tmp_path: Path):
    where = tmp_path / "trial"
    harness = _tree(tmp_path)
    mark_complete(where, run_id="run-1", harness_dir=harness)
    (harness / "sub" / "a.py").write_text("x = 2\n")
    assert is_complete(where, run_id="run-1", harness_dir=harness) is False


def test_read_mark_invalid(tmp_path: Path):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_text("not json")
    assert read_mark(where) is None
    assert is_complete(where) is False


def test_read_mark_invalid_utf8(tmp_path: Path):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_bytes(b"\xff\xfe")
    assert read_mark(where) is None
    assert is_complete(where) is False


def test_extra_cannot_override_reserved_fields(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(
        where,
        run_id="expected",
        extra={"run_id": "other", "version": 999, "at": 0, "harness": "fake"},
    )
    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "expected"
    assert data["version"] == VERSION
    assert data["harness"] == "base"
    assert data["at"] != 0
    assert is_complete(where, run_id="expected") is True
    assert is_complete(where, run_id="other") is False
