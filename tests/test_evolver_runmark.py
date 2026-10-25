"""Unit tests for the evolver trial-identity markers (runmark).

Mirrors Harness²'s ``harness2/core/runmark.py`` discipline: a rollout is only
considered finished when the runner that observed it records a completion
marker, and the marker carries a digest of the harness tree that produced it.
"""

from __future__ import annotations

from pathlib import Path

from evolver.runmark import MARK, VERSION, fingerprint, is_complete, mark_complete, read_mark


def test_fingerprint_none_is_base():
    assert fingerprint(None) == "base"


def test_fingerprint_absent_dir():
    assert fingerprint(Path("/no/such/dir")) == "absent"


def test_fingerprint_stable_and_ignores_byproducts(tmp_path: Path):
    d = tmp_path / "harness"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (d / ".DS_Store").write_text("noise")
    h1 = fingerprint(d)
    # byproducts must not affect the digest
    (d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"changed")
    (d / ".DS_Store").write_text("changed")
    assert fingerprint(d) == h1
    # a real source change does change it
    (d / "a.py").write_text("x = 2\n")
    assert fingerprint(d) != h1


def test_mark_complete_writes_marker(tmp_path: Path):
    where = tmp_path / "trial"
    path = mark_complete(where, run_id="run-1", extra={"foo": "bar"})
    assert path == where / MARK
    data = read_mark(where)
    assert data is not None
    assert data["run_id"] == "run-1"
    assert data["version"] == VERSION
    assert data["foo"] == "bar"
    assert data["harness"] == "base"  # harness_dir None


def test_is_complete_requires_matching_run_id(tmp_path: Path):
    where = tmp_path / "trial"
    mark_complete(where, run_id="run-1")
    assert is_complete(where, run_id="run-1") is True
    assert is_complete(where, run_id="run-2") is False
    assert is_complete(where) is True


def test_is_complete_rejects_stale_harness(tmp_path: Path):
    where = tmp_path / "trial"
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "x.py").write_text("x = 1\n")
    mark_complete(where, run_id="run-1", harness_dir=harness)
    # harness changed -> not complete for reuse
    (harness / "x.py").write_text("x = 2\n")
    assert is_complete(where, run_id="run-1", harness_dir=harness) is False
    # without harness constraint, still complete
    assert is_complete(where, run_id="run-1") is True


def test_read_mark_invalid(tmp_path: Path):
    where = tmp_path / "trial"
    where.mkdir()
    (where / MARK).write_text("not json")
    assert read_mark(where) is None
    assert is_complete(where) is False
