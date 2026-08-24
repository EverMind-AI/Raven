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
    for cmd in ("save", "report", "replay", "minimize", "verdict", "pin", "unpin", "list"):
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


def test_save_workspace_option_finds_session(state, tmp_path) -> None:
    """--workspace points session lookup at a runtime-overridden workspace."""
    from raven.session.manager import SessionManager

    ws = tmp_path / "override-ws"
    manager = SessionManager(ws)
    session = manager.get_or_create("cli:chat1")
    session.add_message("user", "hello")
    manager.save(session)
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:chat1")])

    r = runner.invoke(trajectory_app, ["save", "trace-1", "--workspace", str(ws)])

    assert r.exit_code == 0
    assert "session: included" in r.stdout
    assert (state / "bundles" / "trace-1" / "session.jsonl").exists()


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


# ── report ────────────────────────────────────────────────────────────

_FAKE_CFG_KEY = "fk-cli-cfg-2b8e4a6c1d9f3b7aZ"


@pytest.fixture
def report_config(tmp_path, monkeypatch):
    """Point config loading at a throwaway file so no test reads the user's real config."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"providers": {"anthropic": {"apiKey": _FAKE_CFG_KEY}}}), encoding="utf-8")
    monkeypatch.setattr("raven.config.loader._current_config_path", cfg)
    return cfg


def _write_leaky_log(state):
    artifact = state / "logs" / "audit-artifacts" / "tool.output" / "2026-08-20" / "out.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"stdout": f"auth={_FAKE_CFG_KEY}"}), encoding="utf-8")
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a", attrs={"llm.input.preview": f"key is {_FAKE_CFG_KEY}"}),
            _span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(artifact)}),
        ],
    )


def test_report_yes_produces_redacted_tarball(state, report_config) -> None:
    import tarfile

    _write_leaky_log(state)

    r = runner.invoke(trajectory_app, ["report", "trace-1", "--yes"])

    assert r.exit_code == 0, r.output
    tarball = state / "reports" / "trace-1.tar.gz"
    assert tarball.exists()
    assert "Report ready" in r.stdout
    with tarfile.open(tarball, "r:gz") as tar:
        names = tar.getnames()
        assert "trace-1/redaction.json" in names
        assert "trace-1/manifest.json" in names
        for member in tar.getmembers():
            if member.isfile():
                data = tar.extractfile(member).read().decode("utf-8")
                assert _FAKE_CFG_KEY not in data, member.name
    # The original bundle keeps the unredacted data (local corpus).
    spans = (state / "bundles" / "trace-1" / "spans.jsonl").read_text(encoding="utf-8")
    assert _FAKE_CFG_KEY in spans


def test_report_confirm_accept(state, report_config) -> None:
    _write_leaky_log(state)

    r = runner.invoke(trajectory_app, ["report", "trace-1"], input="y\n")

    assert r.exit_code == 0, r.output
    assert (state / "reports" / "trace-1.tar.gz").exists()


def test_report_declined_produces_no_tarball(state, report_config) -> None:
    _write_leaky_log(state)

    r = runner.invoke(trajectory_app, ["report", "trace-1"], input="n\n")

    assert r.exit_code == 1
    assert "Aborted" in r.stdout
    assert not (state / "reports").exists()


def test_report_out_option(state, report_config, tmp_path) -> None:
    _write_leaky_log(state)
    out = tmp_path / "exports" / "bug.tar.gz"

    r = runner.invoke(trajectory_app, ["report", "trace-1", "--yes", "--out", str(out)])

    assert r.exit_code == 0, r.output
    assert out.exists()
    assert not (state / "reports").exists()


def test_report_config_option_covers_alternate_config(state, report_config, tmp_path) -> None:
    """A trajectory traced under --config must not leak that config's keys:
    report collects secrets from the named config AND the default one."""
    import tarfile

    alt_key = "fk-alt-cfg-7d3e9b1a5c8f2e4bZ"
    alt_cfg = tmp_path / "alt.json"
    alt_cfg.write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": alt_key}},
                "agents": {"defaults": {"workspace": str(tmp_path / "alt-ws")}},
            }
        ),
        encoding="utf-8",
    )
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a", attrs={"llm.input.preview": f"alt={alt_key} main={_FAKE_CFG_KEY}"})],
    )

    r = runner.invoke(trajectory_app, ["report", "trace-1", "--yes", "--config", str(alt_cfg)])

    assert r.exit_code == 0, r.output
    with tarfile.open(state / "reports" / "trace-1.tar.gz", "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                data = tar.extractfile(member).read().decode("utf-8")
                assert alt_key not in data, member.name
                assert _FAKE_CFG_KEY not in data, member.name


def test_report_config_supplies_workspace_for_session(state, report_config, tmp_path) -> None:
    """Without --workspace, the session is looked up in the --config file's
    workspace — not silently omitted."""
    import tarfile

    from raven.session.manager import SessionManager

    ws = tmp_path / "cfg-ws"
    manager = SessionManager(ws)
    session = manager.get_or_create("cli:a")
    session.add_message("user", "hello")
    manager.save(session)
    alt_cfg = tmp_path / "alt-ws.json"
    alt_cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}), encoding="utf-8")
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    r = runner.invoke(trajectory_app, ["report", "trace-1", "--yes", "--config", str(alt_cfg)])

    assert r.exit_code == 0, r.output
    with tarfile.open(state / "reports" / "trace-1.tar.gz", "r:gz") as tar:
        assert "trace-1/session.jsonl" in tar.getnames()


def test_report_unknown_id_errors(state, report_config) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["report", "no-such-id", "--yes"])
    assert r.exit_code == 1
    assert "no spans found" in r.stdout


# ── verdict ───────────────────────────────────────────────────────────


def test_verdict_records_user_source(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id="att-1")])

    r = runner.invoke(trajectory_app, ["verdict", "att-1", "--status", "fail", "--why", "wrong answer"])

    assert r.exit_code == 0
    got = tverdict.read_verdicts(state, attempt_id="att-1")
    assert len(got) == 1
    assert got[0].status == "fail" and got[0].source == "user" and got[0].why == "wrong answer"


def test_verdict_by_turn_trace_id_lands_on_attempt(state) -> None:
    """A verdict given by a turn's trace id must be readable by the attempt id
    (that is where save/list look for it)."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x"),
            _span("trace-2", attempt_id="att-x", name="tool.call"),
        ],
    )

    r = runner.invoke(trajectory_app, ["verdict", "trace-1", "--status", "fail"])

    assert r.exit_code == 0
    assert "att-x" in r.stdout
    got = tverdict.read_verdicts(state, attempt_id="att-x")
    assert len(got) == 1 and got[0].status == "fail"
    assert tverdict.read_verdicts(state, attempt_id="trace-1") == []


def test_verdict_unknown_id_errors(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["verdict", "no-such-id", "--status", "pass"])
    assert r.exit_code == 1
    assert tverdict.read_verdicts(state) == []


def test_verdict_rejects_bad_status(state) -> None:
    r = runner.invoke(trajectory_app, ["verdict", "att-1", "--status", "maybe"])
    assert r.exit_code != 0
    assert tverdict.read_verdicts(state) == []


# ── pin / unpin ───────────────────────────────────────────────────────


def test_pin_unpin_roundtrip(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id="att-1")])

    r = runner.invoke(trajectory_app, ["pin", "att-1", "--reason", "bug #42"])
    assert r.exit_code == 0
    assert tstore.pins(state)["att-1"]["reason"] == "bug #42"

    r = runner.invoke(trajectory_app, ["unpin", "att-1"])
    assert r.exit_code == 0
    assert tstore.pins(state) == {}

    r = runner.invoke(trajectory_app, ["unpin", "att-1"])
    assert r.exit_code == 0
    assert "not pinned" in r.stdout


def test_pin_unpin_resolve_turn_trace_id(state) -> None:
    """Pin/unpin by any turn's trace id must protect/release the whole attempt."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x"),
            _span("trace-2", attempt_id="att-x", name="tool.call"),
        ],
    )

    r = runner.invoke(trajectory_app, ["pin", "trace-1"])
    assert r.exit_code == 0
    assert set(tstore.pins(state)) == {"att-x"}

    r = runner.invoke(trajectory_app, ["unpin", "trace-2"])
    assert r.exit_code == 0
    assert tstore.pins(state) == {}


def test_pin_unknown_id_errors(state) -> None:
    r = runner.invoke(trajectory_app, ["pin", "no-such-id"])
    assert r.exit_code == 1
    assert tstore.pins(state) == {}


def test_unpin_clears_stale_literal_pin(state) -> None:
    """A pin recorded under a turn's trace id (pre-resolution data) still clears."""
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id="att-x")])
    tstore.pin("trace-1", state_dir=state)

    r = runner.invoke(trajectory_app, ["unpin", "trace-1"])

    assert r.exit_code == 0
    assert tstore.pins(state) == {}


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


# ── replay ────────────────────────────────────────────────────────────


def _replay_bundle(root, *, recorded_input=None):
    """A minimal replayable bundle: one turn, one content-only model reply."""
    bundle = root / "att-r"
    (bundle / "artifacts").mkdir(parents=True)
    (bundle / "artifacts" / "turn.json").write_text(
        json.dumps({"content": "go", "channel": "cli", "chat_id": "direct"}), encoding="utf-8"
    )
    (bundle / "artifacts" / "out.json").write_text(
        json.dumps({"content": "done", "finish_reason": "stop", "tool_calls": [], "usage": {}}), encoding="utf-8"
    )
    llm_attrs = {"llm.output.artifact_path": "artifacts/out.json"}
    if recorded_input is not None:
        (bundle / "artifacts" / "in.json").write_text(json.dumps(recorded_input), encoding="utf-8")
        llm_attrs["llm.input.artifact_path"] = "artifacts/in.json"
    spans = [
        {
            "traceId": "att-r",
            "spanId": "llm-0",
            "name": "llm.call",
            "attributes": {"attempt.id": "att-r", "session.key": "cli:direct", **llm_attrs},
        },
        {
            "traceId": "att-r",
            "spanId": "turn-0",
            "name": "session.turn",
            "attributes": {
                "attempt.id": "att-r",
                "session.key": "cli:direct",
                "turn.input.artifact_path": "artifacts/turn.json",
            },
        },
    ]
    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({"format_version": 1, "attempt_id": "att-r"}), encoding="utf-8")
    return bundle


def test_replay_unknown_target_exits_1(state) -> None:
    r = runner.invoke(trajectory_app, ["replay", "no-such-id"])
    assert r.exit_code == 1
    assert "raven trajectory save" in r.stdout


def test_replay_bundle_path_runs_to_completion(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path)

    r = runner.invoke(trajectory_app, ["replay", str(bundle)])

    assert r.exit_code == 0, r.output
    assert "model calls: 1/1" in r.stdout
    assert "turns: 1/1" in r.stdout
    assert "No divergence" in r.stdout


def test_replay_by_attempt_id_finds_bundle_under_state(state) -> None:
    _replay_bundle(state / "bundles")

    r = runner.invoke(trajectory_app, ["replay", "att-r"])

    assert r.exit_code == 0, r.output
    assert "Replay ran to the end" in r.stdout


def test_replay_strict_divergence_exits_2(state, tmp_path) -> None:
    """A recorded request that cannot match the live one (wrong message count)
    halts a strict replay: exit code 2, divergence details printed."""
    bundle = _replay_bundle(
        tmp_path,
        recorded_input={"model": "m", "messages": [{"role": "user", "content": "never"}], "tools": []},
    )

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--strict"])

    assert r.exit_code == 2, r.output
    assert "divergence" in r.stdout
    assert "llm call #1" in r.stdout
    assert "Replay halted" in r.stdout


def test_replay_warn_reports_divergence_but_completes(state, tmp_path) -> None:
    bundle = _replay_bundle(
        tmp_path,
        recorded_input={"model": "m", "messages": [{"role": "user", "content": "never"}], "tools": []},
    )

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--warn"])

    assert r.exit_code == 0, r.output
    assert "divergence(s)" in r.stdout
    assert "Replay ran to the end" in r.stdout


# ── minimize ──────────────────────────────────────────────────────────

_MINIMIZE_INPUT = {
    "model": "m",
    "messages": [{"role": "system", "content": "machine specific"}, {"role": "user", "content": "go"}],
    "tools": [],
    "temperature": 0.1,
}


def test_minimize_bundle_path_writes_cassette(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path, recorded_input=_MINIMIZE_INPUT)
    out = tmp_path / "cassette"

    r = runner.invoke(trajectory_app, ["minimize", str(bundle), "--out", str(out)])

    assert r.exit_code == 0, r.output
    assert "Cassette written" in r.stdout
    assert "size:" in r.stdout and "->" in r.stdout
    assert (out / "manifest.json").is_file()
    assert (out / "redaction.json").is_file()
    llm_in = json.loads((out / "artifacts" / "in.json").read_text(encoding="utf-8"))
    assert set(llm_in) == {"model", "messages", "tools"}


def test_minimize_by_id_defaults_out_under_state(state) -> None:
    _replay_bundle(state / "bundles", recorded_input=_MINIMIZE_INPUT)

    r = runner.invoke(trajectory_app, ["minimize", "att-r"])

    assert r.exit_code == 0, r.output
    assert (state / "cassettes" / "att-r" / "manifest.json").is_file()


def test_minimize_unknown_target_exits_1(state) -> None:
    r = runner.invoke(trajectory_app, ["minimize", "no-such-id"])
    assert r.exit_code == 1
    assert "no bundle found" in r.stdout


def test_minimize_rejects_escaping_attempt_id_in_default_out(state, tmp_path) -> None:
    """A crafted manifest attempt id must not name a default output path
    outside the cassettes directory."""
    bundle = _replay_bundle(tmp_path, recorded_input=_MINIMIZE_INPUT)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["attempt_id"] = "../escape"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    r = runner.invoke(trajectory_app, ["minimize", str(bundle)])

    assert r.exit_code == 1
    assert "cannot be used as a cassette directory name" in r.stdout
    assert not (state / "escape").exists()


def test_minimize_rejects_unreplayable_bundle(state, tmp_path) -> None:
    """A bundle whose model call lacks the recorded request cannot guard a
    regression; minimize refuses instead of producing a weaker cassette."""
    bundle = _replay_bundle(tmp_path)

    r = runner.invoke(trajectory_app, ["minimize", str(bundle), "--out", str(tmp_path / "c")])

    assert r.exit_code == 1
    assert "not fully replayable" in r.stdout
