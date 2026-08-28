"""The bounded replacement for ``asyncio.run`` at the shutdown-bearing entry points.

Driven as a subprocess, deliberately: the defect is in the runner's own
teardown, which only happens when a runner owns the loop. A test that awaited
the stubborn task inside pytest's loop would report success against a shape
production never takes -- that is precisely the hole this file exists to close.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

_STUBBORN_TASK = """
import asyncio
{runner_import}

async def deaf():
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue

async def main():
    task = asyncio.create_task(deaf())
    await asyncio.sleep(0.05)
    task.cancel()
    _, pending = await asyncio.wait([task], timeout=0.05)
    print(f"teardown returned with {{len(pending)}} pending", flush=True)

{run_call}
print("runner returned", flush=True)
"""


def _run_script(body: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO),
    )


def test_stdlib_run_hangs_on_a_task_that_swallows_its_cancellation() -> None:
    """The defect being fixed, pinned so the fix cannot be quietly reverted.

    ``asyncio.runners._cancel_all_tasks`` gathers what it cancelled with no
    timeout, so a bounded teardown that leaves one such task pending has moved
    the hang rather than removed it.
    """
    body = _STUBBORN_TASK.format(runner_import="", run_call="asyncio.run(main())")
    with pytest.raises(subprocess.TimeoutExpired):
        _run_script(body, timeout=5)


def test_the_bounded_runner_exits_on_a_task_that_swallows_its_cancellation() -> None:
    body = _STUBBORN_TASK.format(
        runner_import="from raven.utils import asyncio_runner as bounded_asyncio",
        run_call="bounded_asyncio.run(main(), sweep_timeout=0.2)",
    )
    done = _run_script(body, timeout=20)

    assert done.returncode == 0
    assert "teardown returned with 1 pending" in done.stdout
    assert "runner returned" in done.stdout
    assert "ignored their cancellation" in done.stderr


def test_the_bounded_runner_returns_the_coroutines_value_and_propagates_errors() -> None:
    """The sweep is the only difference from `asyncio.run`; the contract holds."""
    import asyncio

    from raven.utils import asyncio_runner as bounded_asyncio

    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    async def boom() -> None:
        raise RuntimeError("from the coroutine")

    assert bounded_asyncio.run(answer()) == 42
    with pytest.raises(RuntimeError, match="from the coroutine"):
        bounded_asyncio.run(boom())


# Every entry point whose coroutine awaits a teardown of its own, and the exact
# call each one owns. Listed rather than globbed because the property being
# guarded is "this call site is the bounded runner", which a module-level
# substring check cannot tell apart from some other call in the same file.
_OWNING_ENTRY_POINTS = {
    "cli/tui_commands.py": "bounded_asyncio.run(_main())",
    "cli/gateway_commands.py": "bounded_asyncio.run(run())",
    "cli/serve_commands.py": "bounded_asyncio.run(_serve_main(",
    "cli/_tui_relay.py": "bounded_asyncio.run(_main())",
    "cli/acp_commands.py": "bounded_asyncio.run(_serve())",
}

# `asyncio.run` calls in those same modules that own no teardown, so the sweep
# has nothing to give up on and the stdlib runner is the right one.
_PROBES_EXEMPT_FROM_THE_MIGRATION = {
    "cli/_tui_relay.py": ["asyncio.run(_healthy(port))"],
    # `_attach` is a two-second HTTP round trip that closes its own session; it
    # starts no engine, so there is nothing for a bounded sweep to give up on.
    "cli/serve_commands.py": ["asyncio.run(_attach("],
}


def test_every_shutdown_bearing_entry_point_uses_the_bounded_runner() -> None:
    """A host whose coroutine tears down sub-agents and the ACP pool must not
    hand the leftovers back to a runner that waits on them without a bound."""
    for module, call in _OWNING_ENTRY_POINTS.items():
        src = (_REPO / "raven" / module).read_text(encoding="utf-8")
        assert call in src, module


def test_no_owning_entry_point_slips_back_to_the_stdlib_runner() -> None:
    """The guard above only proves the bounded call is present. This one proves
    no *other* `asyncio.run` survives in those modules except the probes named
    as exempt -- which is how the relay and `raven acp` were missed once."""
    for module in _OWNING_ENTRY_POINTS:
        src = (_REPO / "raven" / module).read_text(encoding="utf-8")
        stray = [
            line.strip()
            for line in src.splitlines()
            if "asyncio.run(" in line
            and "bounded_asyncio.run(" not in line
            and not any(ok in line for ok in _PROBES_EXEMPT_FROM_THE_MIGRATION.get(module, []))
        ]
        assert not stray, f"{module}: {stray}"


_CTRL_C_SHUTDOWN = """
import asyncio
import signal
from raven.utils import asyncio_runner as bounded_asyncio

# The disposition a terminal gives a foreground process, set explicitly because
# this one inherits it: a non-interactive CI shell hands its children SIG_IGN,
# and `Runner.run` only installs its handler when the default one is in place.
# Without this the child ignores the signal outright and the test times out
# having proved nothing about the teardown.
signal.signal(signal.SIGINT, signal.default_int_handler)

async def main():
    try:
        print("running", flush=True)
        await asyncio.Event().wait()
    finally:
        print("cleanup started", flush=True)
        await asyncio.sleep(0.3)
        print("cleanup finished", flush=True)

try:
    bounded_asyncio.run(main())
except KeyboardInterrupt:
    print("keyboard interrupt propagated", flush=True)
"""


def test_ctrl_c_still_lets_the_main_coroutine_run_its_teardown() -> None:
    """The whole shutdown this runner exists for lives in the main coroutine's
    `finally`. `Runner.run` reaches it by cancelling the main *task*; a raw
    `run_until_complete` would instead raise KeyboardInterrupt out of the loop
    and leave that teardown to the leftover sweep's much smaller budget."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CTRL_C_SHUTDOWN],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO),
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "running"
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=20)
    except BaseException:
        proc.kill()
        raise

    assert "cleanup started" in out
    assert "cleanup finished" in out, "the teardown was cut short by the sweep"


def test_the_bounded_close_caps_both_open_ended_steps_and_restores_the_seams() -> None:
    """Pins the two stdlib attributes the close swaps, so a rename upstream
    fails here rather than silently restoring an unbounded shutdown."""
    from asyncio import constants, runners

    from raven.utils import asyncio_runner

    seen: dict[str, object] = {}

    class _FakeRunner:
        def close(self) -> None:
            seen["cancel"] = runners._cancel_all_tasks
            seen["join"] = constants.THREAD_JOIN_TIMEOUT

    before_cancel = runners._cancel_all_tasks
    before_join = constants.THREAD_JOIN_TIMEOUT

    asyncio_runner._close_bounded(_FakeRunner(), 1.5, 0.5)

    assert seen["cancel"] is not before_cancel, "the unbounded gather was left in place"
    assert seen["join"] == 0.5, "the 300s executor join was left in place"
    assert runners._cancel_all_tasks is before_cancel
    assert constants.THREAD_JOIN_TIMEOUT == before_join
