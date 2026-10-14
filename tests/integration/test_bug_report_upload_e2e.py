"""A receiver, a report-bug run and an upload retry, each in a real process.

The receiver is `raven trajectory receiver` on an ephemeral port, the client
side is `raven trajectory report-bug --yes` with `RAVEN_BUGREPORT_URL` pointing
at it, and the retry is `raven trajectory upload-bug-report`. What this proves
that the in-process tests cannot: the command line, the environment variable,
the printed address and the signal-driven shutdown all line up end to end.

The receiver child is owned by whoever started it until it listens: a child
that stays silent, exits early, refuses its directory, or is abandoned by a
cancelled test is killed and waited for before the failure is reported, so a
broken start never leaves a process or a blocked reader behind.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.bug_report_receiver_stub import PROXY_VARIABLES
from tests.test_trajectory_bugreport import _span, _write_log

pytestmark = pytest.mark.integration

LISTEN_DENIED = ("Permission denied", "Operation not permitted")
LISTEN_WORDS = ("bind", "listen", "socket", "TCPSite")
STARTUP_DEADLINE = 60.0


def _env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in PROXY_VARIABLES}
    env.update(
        NO_PROXY="*",
        no_proxy="*",
        PYTHONUNBUFFERED="1",
        RAVEN_HOME=str(tmp_path / "home"),
        RAVEN_TRACING_DIR=str(tmp_path / "traces"),
    )
    return env


def _raven(*args: str) -> list[str]:
    return [sys.executable, "-m", "raven", "trajectory", *args]


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Make sure a child is gone and waited for, whatever state it is in."""
    if proc.returncode is None:
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        pass


async def _drain(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    try:
        return (await asyncio.wait_for(stream.read(), 5)).decode("utf-8", errors="replace")
    except TimeoutError:
        return ""


async def _start_receiver(
    args: list[str], env: dict[str, str], *, deadline: float = STARTUP_DEADLINE, spawned: list | None = None
) -> tuple[asyncio.subprocess.Process, int]:
    """Start a receiver and return it with its port once it listens.

    One deadline covers the whole start. Every other exit -- EOF, the
    deadline, cancellation, a refusal -- kills and waits for the child before
    anything is raised; ownership passes to the caller only on success. The
    diagnostics combine what the child wrote to both streams, since the CLI
    reports a refused directory on stdout and a crash on stderr.
    """
    proc = await asyncio.create_subprocess_exec(*args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if spawned is not None:
        spawned.append(proc)
    seen: list[str] = []
    try:
        try:
            async with asyncio.timeout(deadline):
                assert proc.stdout is not None
                while raw := await proc.stdout.readline():
                    line = raw.decode("utf-8", errors="replace")
                    seen.append(line)
                    if "listening on" in line:
                        return proc, int(line.strip().rsplit(":", 1)[1])
            outcome = "the receiver exited before it listened"
        except TimeoutError:
            outcome = f"the receiver printed no listening line within {deadline}s"
    except BaseException:
        await _reap(proc)
        raise
    await _reap(proc)
    text = "".join(seen) + await _drain(proc.stderr)
    if "Cannot prepare" in text or "is not a directory" in text:
        raise AssertionError(f"the receiver refused its save dir: {text[-3000:]}")
    if any(word in text for word in LISTEN_DENIED) and any(word in text for word in LISTEN_WORDS):
        pytest.skip(f"this environment forbids listening: {text.strip().splitlines()[-1]}")
    raise AssertionError(f"{outcome}: {text[-3000:]}")


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)


async def test_report_bug_uploads_through_a_real_receiver(tmp_path):
    env = _env(tmp_path)
    _write_log(tmp_path / "traces" / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    proc, port = await _start_receiver(_raven("receiver", "--save-dir", str(tmp_path / "inbox"), "--port", "0"), env)
    try:
        env["RAVEN_BUGREPORT_URL"] = f"http://127.0.0.1:{port}"
        # --accept-risk only matters if this machine's missing config makes the
        # redaction classification needs_review; it grants nothing otherwise.
        report = _run(_raven("report-bug", "trace-1", "-d", "it broke", "--yes", "--accept-risk"), env)
        assert report.returncode == 0, report.stdout + report.stderr
        assert f"Will upload to http://127.0.0.1:{port} after the package is created." in report.stdout
        assert "Uploaded — receipt rc-" in report.stdout

        (record_dir,) = [p for p in (tmp_path / "traces" / "bugreports").iterdir() if p.name.startswith("br-")]
        record = json.loads((record_dir / "record.json").read_text(encoding="utf-8"))
        assert record["status"] == "local_ready" and record["upload"]["state"] == "uploaded"
        (landed,) = list((tmp_path / "inbox").glob(f"*/{record['report_id']}.tar.gz"))
        assert landed.read_bytes() == Path(record["package"]["path"]).read_bytes()
        sidecar = json.loads(landed.with_name(f"{record['report_id']}.json").read_text(encoding="utf-8"))
        assert sidecar["sha256"] == record["package"]["sha256"]
        assert sidecar["receipt_id"] == record["upload"]["receipt"]

        again = _run(_raven("upload-bug-report", record["report_id"]), env)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "Already uploaded — receipt " + record["upload"]["receipt"] in again.stdout

        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 15) == 0
    finally:
        await _reap(proc)


async def test_a_silent_receiver_is_killed_at_the_deadline(tmp_path):
    spawned: list[asyncio.subprocess.Process] = []
    silent = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(AssertionError, match="printed no listening line"):
        await _start_receiver(silent, _env(tmp_path), deadline=0.3, spawned=spawned)
    (proc,) = spawned
    assert proc.returncode is not None


async def test_a_receiver_that_refuses_its_save_dir_is_reaped_and_reported(tmp_path):
    spawned: list[asyncio.subprocess.Process] = []
    refusing = [sys.executable, "-c", "print('Cannot prepare the receiver (save dir /x): boom'); raise SystemExit(1)"]
    with pytest.raises(AssertionError, match="refused its save dir"):
        await _start_receiver(refusing, _env(tmp_path), spawned=spawned)
    (proc,) = spawned
    assert proc.returncode == 1
