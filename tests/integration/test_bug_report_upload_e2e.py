"""A receiver, a report-bug run and an upload retry, each in a real process.

The receiver is `raven trajectory receiver` on an ephemeral port, the client
side is `raven trajectory report-bug --yes` with `RAVEN_BUGREPORT_URL` pointing
at it, and the retry is `raven trajectory upload-bug-report`. What this proves
that the in-process tests cannot: the command line, the environment variable,
the printed address and the signal-driven shutdown all line up end to end.
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


async def _start_receiver(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(
        _raven("receiver", "--save-dir", str(tmp_path / "inbox"), "--port", "0"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    line = ""
    while "listening on" not in line:
        raw = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 60)
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace")
    if "listening on" not in line:
        stderr = proc.stderr.read().decode("utf-8", errors="replace")[-3000:]
        if "Cannot prepare" in stderr or "is not a directory" in stderr:
            raise AssertionError(f"the receiver refused its save dir: {stderr}")
        if any(word in stderr for word in LISTEN_DENIED) and any(word in stderr for word in LISTEN_WORDS):
            pytest.skip(f"this environment forbids listening: {stderr.strip().splitlines()[-1]}")
        raise AssertionError(f"the receiver did not start: {stderr}")
    return proc, int(line.strip().rsplit(":", 1)[1])


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)


async def test_report_bug_uploads_through_a_real_receiver(tmp_path):
    env = _env(tmp_path)
    _write_log(tmp_path / "traces" / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    proc, port = await _start_receiver(tmp_path, env)
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
        assert await asyncio.wait_for(asyncio.to_thread(proc.wait), 15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
