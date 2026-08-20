"""`raven` commands that build the agent loop must not segfault on exit.

Raven runs native runtimes on daemon threads -- watchfiles' notify runtime under
``SkillFileWatcher``, lancedb's Rust/tokio ``LanceDBBackgroundEventLoop`` when
that store opens. A normal CPython finalization races a live one and segfaults
(exit 139), masking the command's real exit code and failing ``expect_exit(0)``
for the whole TUI e2e suite.

Fix: every exit path of the CLI chokepoint ``raven.cli.commands.run`` converges
on ``settle_native_runtimes`` -- stop the runtimes that have a shutdown hook, and
hard-exit past finalization (flush stdio + loguru, then ``os._exit``) only if
something unstoppable is still live. These tests build the real agent loop in a
subprocess so a native thread is genuinely live; which runtimes those are
depends on the host's memory backend, so each case checks liveness before
concluding anything from a clean run.
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


# Exit code the child uses to signal "no registered native-runtime thread is
# live here" -- distinct from a clean 0 or the build-failed sentinel. Every case
# that could otherwise pass vacuously checks this first: an arm with no live
# runtime is a process with nothing to crash, not evidence about the guard.
_HAZARD_ABSENT = 43


def test_hazard_gate_fires_and_the_exit_path_is_clean():
    """After building the loop the gate reports a live native runtime (so the
    CLI chokepoint knows to act), and going through the real exit path returns
    cleanly (exit 0) instead of a SIGSEGV."""
    src = (
        _BUILD_LOOP
        + "from raven.cli._exit import flush_and_hard_exit, settle_native_runtimes\n"
        + "from raven.utils.native_runtimes import native_finalization_hazard\n"
        + "if not native_finalization_hazard():\n"
        + "    sys.exit(43)\n"
        + "if settle_native_runtimes():\n"
        + "    flush_and_hard_exit(0)\n"
        + "sys.exit(0)\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    assert result.returncode != _HAZARD_ABSENT, (
        "hazard gate did not detect a live native-runtime thread after building the loop"
    )
    assert result.returncode == 0, (
        f"exit path did not exit 0 (rc={result.returncode}); stderr tail:\n{result.stderr[-1500:]}"
    )


def test_a_stoppable_runtime_exits_without_skipping_finalization():
    """Stopping beats skipping: when every live runtime has a shutdown hook the
    process must finalize normally, so ``atexit`` still runs. Unconditional
    ``os._exit`` would pass the exit-code assertion above and silently drop
    every deferred flush on essentially every CLI invocation."""
    src = (
        "import atexit, sys\n"
        "atexit.register(lambda: print('ATEXIT', file=sys.stderr))\n"
        + _BUILD_LOOP
        + "from raven.cli._exit import settle_native_runtimes\n"
        + "from raven.utils.native_runtimes import native_finalization_hazard\n"
        # Without a live runtime there is nothing to stop, and finalizing
        # normally proves nothing about stopping beating skipping.
        + "if not native_finalization_hazard():\n"
        + "    sys.exit(43)\n"
        + "if settle_native_runtimes():\n"
        + "    sys.exit(44)\n"  # an unstoppable runtime is live: not this test's case
        + "sys.exit(0)\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    if result.returncode == _HAZARD_ABSENT:
        pytest.skip("no native runtime is live here, so there is nothing to stop")
    if result.returncode == 44:
        pytest.skip("an unstoppable native runtime is live here (lancedb backend)")
    assert result.returncode == 0, (
        f"stoppable-only exit did not finalize cleanly (rc={result.returncode}); "
        f"stderr tail:\n{result.stderr[-1500:]}"
    )
    assert "ATEXIT" in result.stderr, "finalization was skipped even though every runtime was stoppable"


def test_the_real_exit_code_survives_an_unhandled_exception():
    """``run()``'s guard must cover every exit path, not just ``SystemExit``.

    A ``raise SystemExit`` from inside one of ``run()``'s own except blocks is
    not caught by their sibling handler, and an unhandled exception has no
    handler at all -- both used to finalize unguarded and die with a fatal
    signal, replacing the command's real exit code (measured: rc=-11 where the
    code was 1). The traceback has to survive too, since the guarded path may
    hard-exit past the interpreter's own printing."""
    src = (
        "import raven.cli.commands as C\n"
        + _BUILD_LOOP
        + "def fake_app():\n"
        + "    raise RuntimeError('boom after loop build')\n"
        + "C.app = fake_app\n"
        + "C.run()\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    assert result.returncode == 1, (
        f"unhandled exception did not exit 1 (rc={result.returncode}); stderr tail:\n{result.stderr[-1500:]}"
    )
    assert "boom after loop build" in result.stderr, "the traceback was lost on the guarded exit path"


@pytest.mark.parametrize(
    ("raise_stmt", "expected_code", "expected_stderr"),
    [
        # The interpreter's own codes for these, which the guard must not
        # flatten: `python -c "raise KeyboardInterrupt"` exits 130, and
        # `sys.exit("msg")` prints msg and exits 1.
        ("raise KeyboardInterrupt", 130, "KeyboardInterrupt"),
        ("sys.exit('payload text')", 1, "payload text"),
    ],
    ids=["interrupt", "string_payload"],
)
def test_the_interpreters_own_exit_conventions_survive_the_guard(raise_stmt, expected_code, expected_stderr):
    """The guard exists to preserve the real outcome, so it has to preserve the
    conventional outcomes too -- not just any nonzero code."""
    src = (
        "import raven.cli.commands as C\n"
        + _BUILD_LOOP
        + "def fake_app():\n"
        + f"    {raise_stmt}\n"
        + "C.app = fake_app\n"
        + "C.run()\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    assert result.returncode == expected_code, (
        f"expected rc={expected_code}, got {result.returncode}; stderr tail:\n{result.stderr[-1500:]}"
    )
    assert expected_stderr in result.stderr, "the reason for the exit was lost"


def test_normal_finalization_still_reproduces_the_crash():
    """Guard that settling is load-bearing: with a native runtime live, an
    unguarded finalization crashes.

    The liveness check is the point of the sentinel. A run that exits 0 because
    no runtime ever started would read as "the crash is gone" -- the exact
    mistake that put a wrong mechanism in this module's docstring once: a
    watcher whose roots do not exist starts no thread, so nothing can crash.
    """
    src = (
        _BUILD_LOOP
        + "from raven.utils.native_runtimes import native_finalization_hazard\n"
        + "if not native_finalization_hazard():\n"
        + "    sys.exit(43)\n"
        + "sys.exit(0)\n"
    )
    result = _run(src)
    if result.returncode == _BUILD_FAILED:
        pytest.skip(f"agent loop unbuildable here: {result.stderr.strip()[-200:]}")
    if result.returncode == _HAZARD_ABSENT:
        pytest.skip("no registered native runtime is live here: nothing to crash")
    if result.returncode == 0:
        pytest.skip("native finalization crash not reproducible in this environment")
    # subprocess.run reports a signal-killed child as a negative return code
    # (-11 for SIGSEGV, -6 for a Rust abort, etc.); the native runtime tearing
    # down mid-finalization is a fatal signal, never a clean nonzero exit.
    assert result.returncode < 0, f"expected a fatal-signal crash on finalization, got rc={result.returncode}"
