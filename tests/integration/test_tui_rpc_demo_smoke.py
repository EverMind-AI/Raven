"""v0.0.2 integration smoke.

Each test spawns ``scripts/run_v001_demo.py`` for a single v0.0.2 scenario,
asserts the demo runner exits clean (``rc == 0`` ⇒ handshake latched +
scenario produced at least one RPC frame), and greps the demo log for the
expected RPC method + response/error shape produced by the P5-aligned
handler.

Why pytest integration (subprocess) rather than direct dispatcher unit
tests: v0.0.2 proves the **end-to-end** wire path
``Ink → unix socket → Python handler → JSON-RPC response → Ink render`` still
works for every P5-aligned method. Unit tests under ``tests/test_tui_rpc_*.py``
already cover handler correctness in isolation; this file guarantees the
*wiring* doesn't regress.

Pre-requisites: the demo's ``dist/entry.js`` must exist (the runner can build
it on first call via ``--build`` but the in-repo build is faster to keep
fresh in development). The first test in this module triggers a build if
the artifact is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_RUNNER = _REPO_ROOT / "scripts" / "run_v001_demo.py"
_DEMO_DIST = _REPO_ROOT / "ui-tui" / "demos" / "v001-cli-dispatch" / "dist" / "entry.js"
_LOG_PREFIX = "/tmp/tui-rpc-demo-smoke-"

# Per-test timeout: each scenario runs one Ink child + Python dispatcher, with
# a post-handshake grace of ~6 s in the runner. 60 s is a generous bound that
# still surfaces a wedge.
_PER_SCENARIO_TIMEOUT_S = 60


@pytest.fixture(scope="module", autouse=True)
def _ensure_demo_built() -> None:
    """Build the Ink mini-app once per module if dist/entry.js is missing."""
    if _DEMO_DIST.exists():
        return
    demo_dir = _DEMO_DIST.parent.parent
    proc = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(demo_dir),
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(
            "v0.0.2 smoke requires demo build artifact; "
            f"npm run build failed in {demo_dir}: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def _run_scenario(scenario: str, tmp_log: Path) -> tuple[int, str]:
    """Run a single scenario via the demo runner; return (rc, log_text).

    Always passes ``RAVEN_NODE`` through and DEVNULL's stdout/stderr of
    the runner itself to keep pytest output clean.
    """
    env = {**os.environ}
    cmd = [
        sys.executable,
        str(_DEMO_RUNNER),
        "--scenario",
        scenario,
        "--log",
        str(tmp_log),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=_PER_SCENARIO_TIMEOUT_S,
        env=env,
    )
    return proc.returncode, tmp_log.read_text(encoding="utf-8")


@pytest.fixture
def log_path(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Per-test log file under /tmp; auto-removed after the test."""
    name = request.node.name.replace("/", "_")
    p = Path(f"{_LOG_PREFIX}{name}.log")
    yield p
    # Cleanup: remove any /tmp/tui-rpc-demo-smoke-* the test (or the runner) created.
    for stale in Path("/tmp").glob(f"tui-rpc-demo-smoke-{name}*"):
        try:
            stale.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-scenario tests
# ---------------------------------------------------------------------------


def test_system_hello_handshake_roundtrip(log_path: Path) -> None:
    rc, log = _run_scenario("system-hello-handshake", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    # system.hello request frame present
    assert '"method": "system.hello"' in log
    # Response carries server_version + session
    assert '"server_version": "0.1.0"' in log
    assert '"default_session_key": "tui:default"' in log
    assert '"default_channel": "tui"' in log


def test_setup_status_check(log_path: Path) -> None:
    rc, log = _run_scenario("setup-status-check", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    assert '"method": "setup.status"' in log
    # Response shape: {"provider_configured": <bool>}
    assert '"provider_configured"' in log


def test_reload_mcp_noop_idempotent(log_path: Path) -> None:
    rc, log = _run_scenario("reload-mcp-noop", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    # Should fire 5 concurrent calls (Promise.all in the Ink mini-app); the
    # demo summary records this as method_call_count.
    request_count = log.count('"method": "reload.mcp"')
    assert request_count >= 2, (
        f"expected ≥2 reload.mcp requests for rapid-fire idempotency proof, got {request_count}"
    )
    # Every response must be the canonical no-op shape.
    assert '"ok": true' in log
    assert '"reloaded": 0' in log
    assert '"tools_changed": false' in log


def test_config_get_defaults(log_path: Path) -> None:
    rc, log = _run_scenario("config-get-defaults", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    assert '"method": "config.get"' in log
    # All 4 whitelisted defaults must appear in the response. Values depend
    # on what's already in ~/.raven/config.json — we only assert the
    # *keys* are present, not specific values.
    for key in (
        "agent.thinking_budget",
        "agent.temperature",
        "tui.theme",
        "tui.show_token_usage",
    ):
        assert f'"{key}"' in log, f"config.get response missing key {key!r}"


def test_config_set_readonly_rejected(log_path: Path) -> None:
    rc, log = _run_scenario("config-set-readonly-rejected", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    assert '"method": "config.set"' in log
    # The dispatcher should reject the non-whitelisted key with -32010.
    assert '"code": -32010' in log
    assert '"config_field_readonly"' in log
    assert '"agent.maxTokens"' in log


def test_stub_voice_toggle_not_supported(log_path: Path) -> None:
    rc, log = _run_scenario("stub-not-supported", log_path)
    assert rc == 0, f"runner failed: rc={rc} log={log[-500:]}"
    assert '"method": "voice.toggle"' in log
    # -32012 not_supported_in_v01 structured error.
    assert '"code": -32012' in log
    assert '"not_supported_in_v01"' in log
    assert '"voice not supported in Raven v0.1"' in log


# ---------------------------------------------------------------------------
# Summary smoke — single test that runs the full 11-scenario sweep through
# the runner's default scenario list and asserts the runner self-reports
# success (rc 0 = every scenario got at least one frame back).
# ---------------------------------------------------------------------------


def test_full_11_scenario_sweep_rc_zero(tmp_path: Path) -> None:
    """End-to-end smoke: every default scenario succeeds.

    Slow (~70 s on CI). Kept as one test rather than one-per-scenario above
    because the 11-sweep is what we ship as the v0.0.2 acceptance artifact.
    """
    log = tmp_path / "tui-rpc-demo-smoke-fullsweep.log"
    proc = subprocess.run(
        [sys.executable, str(_DEMO_RUNNER), "--log", str(log)],
        capture_output=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"runner failed for full sweep: rc={proc.returncode} "
        f"stderr={proc.stderr.decode('utf-8', errors='replace')[-1000:]}"
    )
    log_text = log.read_text(encoding="utf-8")
    # Each of the 11 scenarios must have produced a summary block.
    for scenario in (
        "default-channels-status-w-default",
        "width-60",
        "width-100",
        "blacklist-reject",
        "click-usage-error",
        "system-hello-handshake",
        "setup-status-check",
        "reload-mcp-noop",
        "config-get-defaults",
        "config-set-readonly-rejected",
        "stub-not-supported",
    ):
        assert f"========== {scenario} ==========" in log_text
    # The runner appends an ALL SUMMARIES JSON block at the tail.
    assert "ALL SUMMARIES" in log_text
    summaries_start = log_text.rfind("ALL SUMMARIES")
    json_text = log_text[summaries_start:].split("\n", 1)[1]
    # Extract the JSON array (it's the first valid JSON object after the marker).
    decoder = json.JSONDecoder()
    summaries, _ = decoder.raw_decode(json_text)
    assert isinstance(summaries, list) and len(summaries) == 11
    assert all(s["handshake_ok"] for s in summaries), summaries
