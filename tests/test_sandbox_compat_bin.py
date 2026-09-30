"""The commands Raven supplies on a command's PATH when the host lacks them."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from raven.sandbox import compat_bin
from raven.sandbox.direct_executor import DirectExecutor, baseline_env


@pytest.fixture
def no_host_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    monkeypatch.setattr(compat_bin, "_checked", False)
    monkeypatch.setattr(compat_bin, "_dir", None)
    monkeypatch.setattr(compat_bin.shutil, "which", lambda name: None)
    return tmp_path


def test_a_host_without_timeout_gets_one_after_its_own_path(no_host_timeout: Path) -> None:
    """Seen live on macOS: `timeout 90 qwen -p hi` failed with command not
    found and the turn ran the same command again without it."""
    path = baseline_env()["PATH"]
    assert path.split(os.pathsep)[-1] == str(no_host_timeout / "cache" / "compat-bin")
    assert compat_bin.with_compat(path) == path


def test_a_host_timeout_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compat_bin, "_checked", False)
    monkeypatch.setattr(compat_bin, "_dir", None)
    monkeypatch.setattr(compat_bin.shutil, "which", lambda name: "/usr/bin/timeout")
    assert compat_bin.with_compat("/usr/bin") == "/usr/bin"


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("timeout 5 sh -c 'exit 7'", "7"),
        ("timeout 0.2 sleep 3", "124"),
        ("timeout -s KILL 0.2s sleep 3", "124"),
        ("timeout --preserve-status 0.2 sleep 3", "143"),
        ("timeout 2 no-such-command-here", "127"),
        ("timeout nonsense true", "125"),
    ],
)
def test_the_supplied_timeout_answers_the_way_gnu_timeout_does(no_host_timeout: Path, command: str, code: str) -> None:
    result = asyncio.run(DirectExecutor().exec(f"{command}; echo code=$?", cwd=str(no_host_timeout), timeout=20))
    assert f"code={code}" in result.as_text(2000)
