"""The evolver path guard rejects paths that can escape the subject tree."""

from __future__ import annotations

import pytest

from evolver.applier.path_guard import (
    UnsafePathError,
    assert_patch_allowed,
    assert_safe_relative_paths,
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
