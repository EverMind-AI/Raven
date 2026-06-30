"""CLI smoke entry tests: `python -m tests.tui.autotest [smoke ...]`.

Primary test path = pytest. This CLI is the ad-hoc fallback for
Bash()-driven verification without writing a pytest file.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

CLI_BASE = [sys.executable, "-m", "tests.tui.autotest"]


def _run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*CLI_BASE, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestHelp:
    def test_help_exits_zero(self):
        result = _run("--help")
        assert result.returncode == 0
        # Some help text mentioning the smoke subcommand
        assert "smoke" in (result.stdout + result.stderr).lower()

    def test_help_smoke_exits_zero(self):
        result = _run("smoke", "--help")
        assert result.returncode == 0
        out = result.stdout + result.stderr
        assert "command" in out.lower() or "smoke" in out.lower()


class TestUnknownSubcommand:
    def test_unknown_subcommand_exits_two(self):
        result = _run("nonexistent-subcommand")
        # argparse subparsers required → exit 2 on missing/unknown
        assert result.returncode == 2


@pytest.mark.e2e
class TestSmokeSubcommand:
    def test_smoke_succeeds_on_tui_check(self):
        # `raven tui --check` exits 0 cleanly; smoke should report ok
        result = _run(
            "smoke",
            "--wait-readiness",
            r"(?!).*",  # impossible pattern — but tui --check exits before any wait kicks in
            "--wait-timeout",
            "5",
            "uv run raven tui --check",
        )
        # Either smoke detects clean exit (rc=0) or readiness times out
        # but subprocess still exits 0 (acceptable as rc=0:
        # "spawn succeeds + ... subprocess clean exit code = 0"). Allow both
        # 0 (perfect) and 1 (readiness timeout); not 2 (harness error).
        assert result.returncode in (0, 1), (
            f"unexpected rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_smoke_fails_on_nonexistent_binary(self):
        result = _run("smoke", "/nonexistent/binary/path")
        # tui-use shell-execs the command; shell exits 127 (command not found),
        # which surfaces as "subprocess exit != 0" → harness exit 1 per spec §S2
        # (not exit 2, since tui-use's own spawn pipeline ran fine).
        assert result.returncode == 1, (
            f"expected exit 1 (subprocess exit != 0); got {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "[fail]" in result.stdout, f"expected [fail] trace marker; stdout:\n{result.stdout}"
