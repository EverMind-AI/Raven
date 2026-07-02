"""`raven` commands that build the agent loop must not segfault on exit.

The agent loop opens a lancedb-backed store; lancedb starts a process-global
Rust/tokio background thread (``LanceDBBackgroundEventLoop``) with no public
shutdown hook. A normal CPython interpreter finalization races that live native
runtime and segfaults (exit 139), masking the command's real exit code and
failing ``expect_exit(0)`` for the whole TUI e2e suite.

Fix: the CLI exit chokepoint ``raven.cli.commands.run`` hard-exits past
finalization (flush stdio + loguru, then ``os._exit``) when
``raven.cli._exit.lancedb_finalization_hazard`` reports the thread live. These
tests build the real agent loop in a subprocess so the native thread is
genuinely live.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Exit code the child uses to signal "agent loop could not be built in this
# environment" (e.g. no provider configured) so the test skips instead of
# reporting a false regression.
_BUILD_FAILED = 42

_BUILD_LOOP = """
import sys
try:
    from raven.cli.tui_commands import _build_tui_agent_loop
    loop = _build_tui_agent_loop()
except BaseException as e:
    print(f"BUILD_FAILED: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(42)
"""


def _run(child_src: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(child_src)],
        capture_output=True,
        text=True,
        timeout=120,
    )


# Exit code the child uses to signal "the hazard gate failed to detect the
# live lancedb thread" — distinct from a clean 0 or the build-failed sentinel.
_HAZARD_ABSENT = 43


def test_hazard_gate_fires_and_hard_exit_is_clean():
    """After building the loop the hazard gate reports the native thread live
    (so the CLI chokepoint knows to guard), and exiting via the hard-exit
    helper returns cleanly (exit 0) instead of a SIGSEGV."""
    src = (
        _BUILD_LOOP
        + "from raven.cli._exit import flush_and_hard_exit, lancedb_finalization_hazard\n"
        + "if not lancedb_finalization_hazard():\n"
        + "    sys.exit(43)\n"
        + "flush_and_hard_exit(0)\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    assert result.returncode != _HAZARD_ABSENT, (
        "hazard gate did not detect the live lancedb thread after building the loop"
    )
    assert result.returncode == 0, (
        f"hard-exit path did not exit 0 (rc={result.returncode}); "
        f"stderr tail:\n{result.stderr[-1500:]}"
    )


def test_normal_finalization_still_reproduces_the_crash():
    """Guard that the hard-exit is load-bearing: with the native memory stack
    live, a normal interpreter finalization crashes. Skips when the current
    environment cannot reproduce the crash (so the suite stays green on hosts
    without the offending native runtime)."""
    src = _BUILD_LOOP + "sys.exit(0)\n"
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    if result.returncode == 0:
        pytest.skip("native finalization crash not reproducible in this environment")
    # subprocess.run reports a signal-killed child as a negative return code
    # (-11 for SIGSEGV, -6 for a Rust abort, etc.); the native runtime tearing
    # down mid-finalization is a fatal signal, never a clean nonzero exit.
    assert result.returncode < 0, (
        f"expected a fatal-signal crash on finalization, got rc={result.returncode}"
    )
