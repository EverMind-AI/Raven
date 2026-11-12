"""The evolver path guard rejects paths that can escape the subject tree."""

from __future__ import annotations

import pytest

from evolver.applier.path_guard import (
    UnsafePathError,
    assert_patch_allowed,
    assert_safe_relative_paths,
    is_immutable,
)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.txt",
        "raven/../../etc/passwd",
        "C:/Windows/System32/drivers/etc/hosts",
        r"C:\\Windows\\System32\\drivers\\etc\\hosts",
        "\\\\server\\share\\file.txt",
        ".",
        "",
        "bad\x00path",
        "NUL",
        "nul.txt",
        "CON",
        "COM1",
        "LPT9.log",
        "CONIN$",
        "CONOUT$",
        ".. ",
        ".. .",
        "foo/.. /bar",
        "NUL/foo",
        "NUL:stream",
        "NUL .txt",
        "CLOCK$",
        "COM¹",
        "a:b",
    ],
)
def test_unsafe_paths_are_rejected(path: str):
    with pytest.raises(UnsafePathError):
        assert_safe_relative_paths([path])
    with pytest.raises(UnsafePathError):
        assert_patch_allowed([path])


@pytest.mark.parametrize(
    "path",
    [
        "raven/agent/loop/main.py",
        "skills/foo/SKILL.md",
        "benchmarks/appworld/evolve/adapter.py",
        "a.txt",
        "./file.txt",
        "nulish.txt",
        "console.txt",
    ],
)
def test_relative_repo_paths_are_allowed(path: str):
    assert_safe_relative_paths([path])


@pytest.mark.parametrize(
    "path",
    [
        "EVOLVER/orchestrator/gates/pipeline.py",
        "Evolver/x.py",
        "Raven/agent/loop/main.py",
        "raven/agent/loop/MAIN.PY",
        "raven/eval_engine/ENGINE.py",
        "Benchmarks/AppWorld/Evolve/grade.py",
    ],
)
def test_a_case_varied_immutable_path_is_still_immutable(path: str):
    """Windows resolves these to the immutable file, so the guard must refuse them.

    `_match` used to compare exact strings, so the same patch was judged
    differently by the filesystem underneath it.
    """
    assert is_immutable(path)


@pytest.mark.parametrize(
    "path",
    [
        "skills/foo/SKILL.md",
        "raven/agent/loop/other.py",
        "docs/CONTRIBUTING.md",
    ],
)
def test_case_folding_does_not_make_a_mutable_path_immutable(path: str):
    assert not is_immutable(path)
