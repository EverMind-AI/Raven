"""Tests for the ``raven trajectory`` CLI subapp."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.cli.trajectory_commands import trajectory_app
from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict

runner = CliRunner()


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, *, attempt_id=None, session_key=None, name="session.turn", attrs=None):
    attributes = {"attempt.id": attempt_id or trace_id, "session.key": session_key}
    if attrs:
        attributes.update(attrs)
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": f"span-{trace_id}-{name}",
        "name": name,
        "startTime": "2026-08-20T10:00:00+00:00",
        "endTime": "2026-08-20T10:00:01+00:00",
        "attributes": attributes,
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    return state


# ── registration + help ───────────────────────────────────────────────


def test_group_registered_on_app() -> None:
    r = runner.invoke(app, ["trajectory", "--help"])
    assert r.exit_code == 0
    assert "trajectories" in r.stdout


def test_subcommand_help() -> None:
    for cmd in ("save", "verdict", "pin", "unpin", "list"):
        r = runner.invoke(trajectory_app, [cmd, "--help"])
        assert r.exit_code == 0, cmd


# ── save ──────────────────────────────────────────────────────────────


def test_save_bundles_and_pins(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    r = runner.invoke(trajectory_app, ["save", "trace-1"])

    assert r.exit_code == 0
    bundle_dir = state / "bundles" / "trace-1"
    assert str(bundle_dir) in r.stdout.replace("\n", "")
    assert (bundle_dir / "manifest.json").exists()
    assert (bundle_dir / "spans.jsonl").exists()
    assert tstore.pins(state)["trace-1"]["reason"] == "bundled"
    assert "spans: 1" in r.stdout


def test_save_honors_out_option(state, tmp_path) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    out = tmp_path / "exports"

    r = runner.invoke(trajectory_app, ["save", "trace-1", "--out", str(out)])

    assert r.exit_code == 0
    assert (out / "trace-1" / "manifest.json").exists()


def test_save_by_turn_trace_id_bundles_whole_attempt(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x", session_key="cli:a"),
            _span("trace-2", attempt_id="att-x", session_key="cli:a", name="tool.call"),
        ],
    )

    r = runner.invoke(trajectory_app, ["save", "trace-1"])

    assert r.exit_code == 0
    assert (state / "bundles" / "att-x" / "manifest.json").exists()
    assert "att-x" in r.stdout and "whole attempt" in r.stdout
    assert "spans: 2" in r.stdout


def test_save_unknown_id_errors(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["save", "no-such-id"])
    assert r.exit_code == 1
    assert "no spans found" in r.stdout


def test_save_reports_missing_artifacts(state) -> None:
    gone = str(state / "purged.json")
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": gone})],
    )

    r = runner.invoke(trajectory_app, ["save", "trace-1"])

    assert r.exit_code == 0
    assert "missing" in r.stdout


# ── verdict ───────────────────────────────────────────────────────────


def test_verdict_records_user_source(state) -> None:
    r = runner.invoke(trajectory_app, ["verdict", "att-1", "--status", "fail", "--why", "wrong answer"])

    assert r.exit_code == 0
    got = tverdict.read_verdicts(state, attempt_id="att-1")
    assert len(got) == 1
    assert got[0].status == "fail" and got[0].source == "user" and got[0].why == "wrong answer"


def test_verdict_rejects_bad_status(state) -> None:
    r = runner.invoke(trajectory_app, ["verdict", "att-1", "--status", "maybe"])
    assert r.exit_code != 0
    assert tverdict.read_verdicts(state) == []


# ── pin / unpin ───────────────────────────────────────────────────────


def test_pin_unpin_roundtrip(state) -> None:
    r = runner.invoke(trajectory_app, ["pin", "att-1", "--reason", "bug #42"])
    assert r.exit_code == 0
    assert tstore.pins(state)["att-1"]["reason"] == "bug #42"

    r = runner.invoke(trajectory_app, ["unpin", "att-1"])
    assert r.exit_code == 0
    assert tstore.pins(state) == {}

    r = runner.invoke(trajectory_app, ["unpin", "att-1"])
    assert r.exit_code == 0
    assert "not pinned" in r.stdout


# ── list ──────────────────────────────────────────────────────────────


def test_list_aggregates_attempts(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x", session_key="cli:a"),
            _span("trace-2", attempt_id="att-x", session_key="cli:a", name="tool.call"),
            _span("trace-3", session_key="cli:b"),
        ],
    )
    tverdict.record_verdict("att-x", "pass", source="user", state_dir=state)

    r = runner.invoke(trajectory_app, ["list"])

    assert r.exit_code == 0
    assert "att-x" in r.stdout
    assert "trace-3" in r.stdout
    assert "pass" in r.stdout


def test_list_session_filter(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x", session_key="cli:a"),
            _span("trace-3", session_key="cli:b"),
        ],
    )

    r = runner.invoke(trajectory_app, ["list", "--session", "cli:b"])

    assert r.exit_code == 0
    assert "trace-3" in r.stdout
    assert "att-x" not in r.stdout


def test_list_empty(state) -> None:
    r = runner.invoke(trajectory_app, ["list"])
    assert r.exit_code == 0
    assert "No attempts found" in r.stdout
