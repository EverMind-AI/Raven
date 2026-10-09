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
    ("args", "code"),
    [
        ("5 sh -c 'exit 7'", "7"),
        ("0.2 sleep 3", "124"),
        # GNU reports a child it had to KILL as 128+9, not 124.
        ("-s KILL 0.2s sleep 3", "137"),
        ("--preserve-status 0.2 sleep 3", "143"),
        ("2 no-such-command-here", "127"),
        ("nonsense true", "125"),
    ],
)
def test_the_supplied_timeout_answers_the_way_gnu_timeout_does(no_host_timeout: Path, args: str, code: str) -> None:
    """Called by its own path: a Linux host's real `timeout` sits earlier on the
    PATH, and the shim is what these codes are about."""
    shim = Path(compat_bin.compat_bin_dir() or "") / "timeout"
    assert shim.is_file()
    result = asyncio.run(DirectExecutor().exec(f"'{shim}' {args}; echo code=$?", cwd=str(no_host_timeout), timeout=20))
    assert f"code={code}" in result.as_text(2000)


@pytest.mark.parametrize("duration", ["5", "5s", "0.5", ".5", "5.", "1e3", "+5", " 5 ", "inf", "-k .5 1"])
def test_every_duration_the_shim_runs_is_one_the_gate_reads_past(
    no_host_timeout: Path, monkeypatch: pytest.MonkeyPatch, duration: str
) -> None:
    """A spelling the shim ran and the gate could not parse left the gate taking
    the duration for the program, so `timeout .5 curl ...` ran past a deny rule
    on `curl` that `curl ...` itself hit."""
    import io
    import shlex
    import sys

    from raven.permissions.shell_policy import _runner_inner_command

    # The written shim's own code, run in this process: one interpreter start per
    # spelling is seconds of idle, and the parse is what is under test.
    shim = Path(compat_bin.compat_bin_dir() or "") / "timeout"
    words = shlex.split(duration) if duration.startswith("-") else [duration]
    monkeypatch.setattr(sys, "argv", ["timeout", *words, "true"])
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with pytest.raises(SystemExit) as exited:
        exec(compile(shim.read_text(encoding="utf-8"), str(shim), "exec"), {"__name__": "__main__"})
    ran = exited.value.code == 0
    gate_sees = _runner_inner_command(["timeout", *words, "curl", "https://example.invalid/x"])
    assert not ran or gate_sees == "curl https://example.invalid/x", (duration, gate_sees)
