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
    """Span in the current format; pass ``attempt_id`` for a legacy-format span."""
    attributes = {"session.key": session_key}
    if attempt_id is not None:
        attributes["attempt.id"] = attempt_id
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


def test_bare_invocation_without_tty_exits_2(state) -> None:
    """The browser needs a terminal; CliRunner provides none."""
    r = runner.invoke(trajectory_app, [])
    assert r.exit_code == 2
    assert "Non-interactive" in r.stdout


def test_subcommands_unaffected_by_tty_gate(state) -> None:
    """The callback must pass subcommands through before the TTY check."""
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["list"])
    assert r.exit_code == 0
    assert "trace-1" in r.stdout


def test_subcommand_help() -> None:
    for cmd in ("save", "report", "replay", "minimize", "verdict", "pin", "unpin", "merge", "split", "list"):
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
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a", name="tool.call"),
        ],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    r = runner.invoke(trajectory_app, ["save", "trace-1"])

    assert r.exit_code == 0
    assert (state / "bundles" / aid / "manifest.json").exists()
    norm = " ".join(r.stdout.split())
    assert aid in norm and "whole attempt" in norm
    assert "spans: 2" in norm


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
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])

    r = runner.invoke(trajectory_app, ["verdict", "trace-1", "--status", "fail", "--why", "wrong answer"])

    assert r.exit_code == 0
    got = tverdict.read_verdicts(state, attempt_id="trace-1")
    assert len(got) == 1
    assert got[0].status == "fail" and got[0].source == "user" and got[0].why == "wrong answer"


def test_verdict_by_turn_trace_id_lands_on_attempt(state) -> None:
    """A verdict given by a turn's trace id must be readable by the attempt id
    (that is where save/list look for it)."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a", name="tool.call"),
        ],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    r = runner.invoke(trajectory_app, ["verdict", "trace-1", "--status", "fail"])

    assert r.exit_code == 0
    assert aid in " ".join(r.stdout.split())
    got = tverdict.read_verdicts(state, attempt_id=aid)
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
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])

    r = runner.invoke(trajectory_app, ["pin", "trace-1", "--reason", "bug #42"])
    assert r.exit_code == 0
    assert tstore.pins(state)["trace-1"]["reason"] == "bug #42"

    r = runner.invoke(trajectory_app, ["unpin", "trace-1"])
    assert r.exit_code == 0
    assert tstore.pins(state) == {}

    r = runner.invoke(trajectory_app, ["unpin", "trace-1"])
    assert r.exit_code == 0
    assert "not pinned" in r.stdout


def test_pin_command_uses_locked_pin_attempt(state, monkeypatch) -> None:
    """Guard: the CLI must pin through the attempts-lock-aware pin_attempt —
    a lock-free resolve + literal pin() can land on an id a concurrent split
    just deleted, leaving a dangling pin that protects nothing."""
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    calls = []

    def record(id_, *, reason="", state_dir=None):
        calls.append((id_, reason))
        return "att-1"

    def forbid(id_, *, reason="", state_dir=None):
        raise AssertionError("the pin command must not call the literal pin() directly")

    monkeypatch.setattr(tstore, "pin_attempt", record)
    monkeypatch.setattr(tstore, "pin", forbid)

    r = runner.invoke(trajectory_app, ["pin", "trace-1", "--reason", "keep"])

    assert r.exit_code == 0
    assert calls == [("trace-1", "keep")]
    assert "att-1" in r.stdout


def test_pin_unpin_resolve_turn_trace_id(state) -> None:
    """Pin/unpin by any turn's trace id must protect/release the whole attempt."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a", name="tool.call"),
        ],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    r = runner.invoke(trajectory_app, ["pin", "trace-1"])
    assert r.exit_code == 0
    assert set(tstore.pins(state)) == {aid}

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


def test_unpin_clears_definition_aliases_and_members(state) -> None:
    """Unpin on a definition drops every pin protecting the attempt: the
    definition id, absorbed aliases, and member-level pins."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    tstore.pin("trace-1", reason="member", state_dir=state)
    d1 = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    d2 = tstore.merge_attempts([d1, "trace-3"], state_dir=state)
    tstore.pin("trace-2", reason="manual", state_dir=state)
    assert tstore.pins(state)

    r = runner.invoke(trajectory_app, ["unpin", d2])

    assert r.exit_code == 0
    assert tstore.pins(state) == {}


# ── merge / split ─────────────────────────────────────────────────────


def test_merge_creates_definition(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a"), _span("trace-2", session_key="cli:a")],
    )

    r = runner.invoke(trajectory_app, ["merge", "trace-1", "trace-2"])

    assert r.exit_code == 0
    (aid,) = tstore.definitions(state)
    assert aid.startswith("att-")
    assert aid in " ".join(r.stdout.split())
    assert sorted(tstore.definitions(state)[aid]["traces"]) == ["trace-1", "trace-2"]


def test_merge_save_list_split_roundtrip(state) -> None:
    """The whole flow: merge -> save by member id -> one list row -> split ->
    two list rows again."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a", name="tool.call"),
        ],
    )

    r = runner.invoke(trajectory_app, ["merge", "trace-1", "trace-2"])
    assert r.exit_code == 0
    (aid,) = tstore.definitions(state)

    r = runner.invoke(trajectory_app, ["save", "trace-2"])
    assert r.exit_code == 0
    assert (state / "bundles" / aid / "manifest.json").exists()
    assert "whole attempt" in " ".join(r.stdout.split())

    # A wide terminal keeps the minted id in one table cell line (the default
    # 80 columns folds it mid-id, breaking substring assertions).
    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    squash = "".join(r.stdout.split())
    assert squash.count(aid) == 1
    assert "trace-1" not in squash and "trace-2" not in squash

    r = runner.invoke(trajectory_app, ["split", aid])
    assert r.exit_code == 0

    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    squash = "".join(r.stdout.split())
    assert "trace-1" in squash and "trace-2" in squash
    assert aid not in squash


def test_merge_legacy_attempt_expands_whole_group(state) -> None:
    """A legacy id input pulls its whole span-attribute group in as members,
    and the canonical legacy id survives as an alias."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", attempt_id="att-old", session_key="cli:a", name="tool.call"),
            _span("trace-3", session_key="cli:a"),
        ],
    )

    r = runner.invoke(trajectory_app, ["merge", "att-old", "trace-3"])

    assert r.exit_code == 0
    (aid,) = tstore.definitions(state)
    entry = tstore.definitions(state)[aid]
    assert sorted(entry["traces"]) == ["trace-1", "trace-2", "trace-3"]
    assert "att-old" in entry["aliases"]


def test_merge_rejects_unknown_id(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["merge", "trace-1", "no-such-id"])
    assert r.exit_code == 1
    assert "unknown id" in " ".join(r.stdout.split())
    assert tstore.definitions(state) == {}


def test_merge_rejects_cross_session(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a"), _span("trace-2", session_key="cli:b")],
    )
    r = runner.invoke(trajectory_app, ["merge", "trace-1", "trace-2"])
    assert r.exit_code == 1
    assert tstore.definitions(state) == {}


def test_merge_rejects_single_group(state) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    r = runner.invoke(trajectory_app, ["merge", "trace-1", "trace-1"])
    assert r.exit_code == 1
    assert tstore.definitions(state) == {}


def test_split_pure_legacy_exits_1(state) -> None:
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old"),
            _span("trace-2", attempt_id="att-old", name="tool.call"),
        ],
    )
    r = runner.invoke(trajectory_app, ["split", "att-old"])
    assert r.exit_code == 1
    assert "only merged attempts can be split" in " ".join(r.stdout.split())


def test_split_unknown_id_exits_1(state) -> None:
    r = runner.invoke(trajectory_app, ["split", "no-such-id"])
    assert r.exit_code == 1
    assert "only merged attempts can be split" in " ".join(r.stdout.split())


def test_split_reports_traces_not_attempts(state) -> None:
    """A definition holding a multi-trace legacy group restores fewer attempts
    than traces, so the output counts member traces, never attempts."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", attempt_id="att-old", session_key="cli:a", name="tool.call"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    aid = tstore.merge_attempts(["att-old", "trace-3"], state_dir=state)

    r = runner.invoke(trajectory_app, ["split", aid])

    assert r.exit_code == 0
    norm = " ".join(r.stdout.split())
    assert "3 member trace" in norm
    assert "3 attempts" not in norm and "per-turn" not in norm

    r = runner.invoke(trajectory_app, ["list"])
    squash = "".join(r.stdout.split())
    assert "att-old" in squash and "trace-3" in squash
    assert aid not in squash
    assert "trace-1" not in squash and "trace-2" not in squash


def test_split_by_member_id_does_not_claim_removed_input(state) -> None:
    """split addressed by a member id (or alias) must not present that input
    as the deleted definition."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    r = runner.invoke(trajectory_app, ["split", "trace-1"])

    assert r.exit_code == 0
    norm = " ".join(r.stdout.split())
    assert "addressed by trace-1" in norm
    assert "Removed definition" not in norm
    assert tstore.definitions(state) == {}

    d1 = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    tstore.merge_attempts([d1, "trace-3"], state_dir=state)

    r = runner.invoke(trajectory_app, ["split", d1])

    assert r.exit_code == 0
    norm = " ".join(r.stdout.split())
    assert f"addressed by {d1}" in norm
    assert "Removed definition" not in norm
    assert tstore.definitions(state) == {}


# ── list ──────────────────────────────────────────────────────────────


def test_list_legacy_logs_still_group(state) -> None:
    """Pre-definition logs (span-level attempt.id) keep aggregating by it."""
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
            _span("trace-1", session_key="cli:a"),
            _span("trace-3", session_key="cli:b"),
        ],
    )

    r = runner.invoke(trajectory_app, ["list", "--session", "cli:b"])

    assert r.exit_code == 0
    assert "trace-3" in r.stdout
    assert "trace-1" not in r.stdout


def test_list_folds_definition_members_single_row(state) -> None:
    """Definition ownership outranks a member's legacy attempt.id — a merged
    legacy member must not surface as a second row."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
        ],
    )
    aid = tstore.merge_attempts(["att-old", "trace-2"], state_dir=state)

    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})

    assert r.exit_code == 0
    squash = "".join(r.stdout.split())
    assert aid in squash
    assert "att-old" not in squash
    assert "trace-1" not in squash and "trace-2" not in squash


def test_list_merged_attempt_shows_verdict_via_alias(state) -> None:
    """A verdict recorded under an absorbed definition id (now an alias) stays
    visible on the current definition's row; a later verdict wins."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    d1 = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    tverdict.record_verdict(d1, "fail", source="user", state_dir=state)
    d2 = tstore.merge_attempts([d1, "trace-3"], state_dir=state)
    assert d1 in tstore.definitions(state)[d2]["aliases"]

    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert d2 in "".join(r.stdout.split())
    assert "fail" in r.stdout

    tverdict.record_verdict(d2, "pass", source="user", state_dir=state)
    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "pass" in r.stdout and "fail" not in r.stdout


def test_list_tolerates_malformed_verdict_lines(state) -> None:
    """A shape-complete verdict line with a list attempt_id must not break the
    verdict column for the healthy rows."""
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    tverdict.record_verdict("trace-1", "pass", source="user", state_dir=state)
    with (state / "verdicts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"attempt_id": ["bad"], "status": "fail", "source": "user", "ts": "now"}) + "\n")

    r = runner.invoke(trajectory_app, ["list"])

    assert r.exit_code == 0
    assert "pass" in r.stdout


def test_list_empty(state) -> None:
    r = runner.invoke(trajectory_app, ["list"])
    assert r.exit_code == 0
    assert "No attempts found" in r.stdout


# ── markup safety ─────────────────────────────────────────────────────


def test_list_renders_markup_legacy_id_literally(state) -> None:
    """A legal legacy id may contain Rich markup; the table cell must show it
    literally instead of crashing markup parsing."""
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id="x[/red]y")])

    r = runner.invoke(trajectory_app, ["list"])

    assert r.exit_code == 0
    assert "x[/red]y" in "".join(r.stdout.split())


def test_list_empty_markup_session_is_safe(state) -> None:
    """The no-matches notice echoes the --session filter, which is user input."""
    r = runner.invoke(trajectory_app, ["list", "--session", "x[/dim]y"])

    assert r.exit_code == 0
    assert "x[/dim]y" in "".join(r.stdout.split())
    assert "No attempts found" in r.stdout


def test_list_tolerates_malformed_span_attributes(state) -> None:
    """JSON-legal records with non-string ids, keys, or timestamps degrade
    per-field instead of killing the whole listing."""
    spans = [
        _span("trace-1", session_key="cli:a"),
        _span("trace-int"),
        _span("trace-list"),
        _span("trace-badkey"),
        _span("trace-badtime", session_key="cli:a"),
    ]
    spans[1]["attributes"]["attempt.id"] = 123
    spans[2]["attributes"]["attempt.id"] = ["not", "hashable"]
    spans[3]["attributes"]["session.key"] = 123
    spans[4]["startTime"] = 123
    spans.append({"schemaVersion": "audit.span.v1", "traceId": 456, "name": "session.turn", "attributes": {}})
    spans.append({"schemaVersion": "audit.span.v1", "traceId": "trace-attrs-list", "attributes": ["bad"]})
    spans.append({"schemaVersion": "audit.span.v1", "traceId": "trace-attrs-str", "attributes": "bad"})
    _write_log(state / "logs" / "audit-spans.log", spans)

    r = runner.invoke(trajectory_app, ["list"], env={"COLUMNS": "200"})

    assert r.exit_code == 0
    squash = "".join(r.stdout.split())
    assert "trace-1" in squash
    # Non-string attempt.id falls back to the trace id; the rest degrade to "-".
    assert "trace-int" in squash and "trace-list" in squash
    assert "trace-badkey" in squash and "trace-badtime" in squash
    # A truthy non-object attributes container degrades to no attributes.
    assert "trace-attrs-list" in squash and "trace-attrs-str" in squash

    # The session filter walks the same records inside iter_spans; a defective
    # container must not shadow the matching rows there either.
    r = runner.invoke(trajectory_app, ["list", "--session", "cli:a"], env={"COLUMNS": "200"})

    assert r.exit_code == 0
    squash = "".join(r.stdout.split())
    assert "trace-1" in squash and "trace-badtime" in squash
    assert "trace-attrs-list" not in squash and "trace-attrs-str" not in squash


def test_markup_legacy_id_success_paths_are_safe(state) -> None:
    legacy = "x[/red]y"
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id=legacy)])

    r = runner.invoke(trajectory_app, ["verdict", "trace-1", "--status", "fail"])
    assert r.exit_code == 0
    assert legacy in "".join(r.stdout.split())

    r = runner.invoke(trajectory_app, ["pin", "trace-1"])
    assert r.exit_code == 0
    assert legacy in "".join(r.stdout.split())
    assert legacy in tstore.pins(state)

    r = runner.invoke(trajectory_app, ["unpin", legacy])
    assert r.exit_code == 0
    assert legacy in "".join(r.stdout.split())
    assert tstore.pins(state) == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["merge", "x[/red]y", "trace-1"],
        ["split", "x[/red]y"],
        ["save", "x[/red]y"],
        ["replay", "x[/red]y"],
        ["minimize", "x[/red]y"],
    ],
    ids=["merge", "split", "save", "replay", "minimize"],
)
def test_markup_unknown_target_error_paths_are_safe(state, argv) -> None:
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])

    r = runner.invoke(trajectory_app, argv)

    assert r.exit_code == 1
    assert "x[/red]y" in "".join(r.stdout.split())


def test_minimize_markup_manifest_id_error_is_safe(state, tmp_path) -> None:
    """A markup-bearing manifest attempt_id (it contains '/') is refused as a
    default cassette directory name, with the id echoed literally."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"format_version": 1, "attempt_id": "x[/red]y"}), encoding="utf-8")

    r = runner.invoke(trajectory_app, ["minimize", str(bundle)])

    assert r.exit_code == 1
    assert "x[/red]y" in "".join(r.stdout.split())


@pytest.mark.parametrize("bad_id", [123, ["a", "b"]], ids=["int", "list"])
def test_minimize_non_string_manifest_id_fails_controlled(state, tmp_path, bad_id) -> None:
    """A JSON-legal manifest whose attempt_id is not a string falls back to the
    bundle directory name instead of blowing up the default-path join."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"format_version": 1, "attempt_id": bad_id}), encoding="utf-8")

    r = runner.invoke(trajectory_app, ["minimize", str(bundle)])

    # The empty bundle is still rejected, but through the controlled error
    # path (typer.Exit), never an uncaught TypeError from the path join.
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)


@pytest.mark.parametrize(
    "content", ['["not", "an", "object"]', '"just a string"', "{not json"], ids=["list", "string", "broken"]
)
def test_minimize_invalid_manifest_fails_controlled(state, tmp_path, content) -> None:
    """A manifest that is not a JSON object (or not JSON at all) is refused
    with a readable error instead of an AttributeError/JSONDecodeError."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(content, encoding="utf-8")

    r = runner.invoke(trajectory_app, ["minimize", str(bundle)])

    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)
    assert "not a valid bundle manifest" in " ".join(r.stdout.split())


def test_minimize_non_utf8_manifest_fails_controlled(state, tmp_path) -> None:
    """Invalid UTF-8 in the manifest raises before JSON parsing; it must reach
    the same controlled error path as broken JSON."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(b'\xff\xfe{"attempt_id": 1}')

    r = runner.invoke(trajectory_app, ["minimize", str(bundle)])

    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)
    assert "not a valid bundle manifest" in " ".join(r.stdout.split())


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


# ── report-bug (the scriptable bug report entry) ───────────────────────

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIabcdef\n-----END PRIVATE KEY-----"
_ENTROPY_TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"


@pytest.fixture
def _no_machine_secrets(monkeypatch):
    from raven.trajectory import bugreport as breport

    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))


def _simple_log(state, attrs=None):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a", attrs=attrs)])


def _staging_entries(state):
    from raven.trajectory import bugreport as breport

    staging = breport.bugreports_root(state) / breport.STAGING_DIR
    return list(staging.iterdir()) if staging.is_dir() else []


def test_report_bug_happy_path_with_yes(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 0, r.output
    assert "ready (local_ready)" in r.output
    assert "Not uploaded" in r.output
    assert "the attempt was pinned" in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["problem"]["description"] == "it broke"
    assert _staging_entries(state) == []


def test_report_bug_requires_description(state, _no_machine_secrets):
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "--yes"])
    assert r.exit_code != 0


def test_report_bug_non_tty_without_yes_fails_before_side_effects(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport
    from raven.trajectory import store as tstore_module

    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke"])

    assert r.exit_code == 1
    assert "nothing was collected or pinned" in r.output
    assert breport.list_reports(state) == []
    assert not (breport.bugreports_root(state) / breport.STAGING_DIR).exists()
    assert tstore_module.pins(state) == {}


def test_report_bug_needs_review_requires_accept_risk(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 1
    assert "--accept-risk" in r.output
    assert "residual scan flagged" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_accept_risk_ships_needs_review(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["redaction"]["classification"] == "needs_review"
    assert record["status"] == "local_ready"


def test_report_bug_blocked_cannot_be_forced(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": _PEM})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes", "--accept-risk"])

    assert r.exit_code == 1
    assert "private key block" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_rejects_bogus_severity(state, _no_machine_secrets):
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--severity", "urgent", "--yes"])
    assert r.exit_code == 1
    assert "--severity must be one of" in r.output


def test_report_bug_traversal_id_fails_cleanly(state, _no_machine_secrets):
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "../escape", "-d", "x", "--yes"])
    assert r.exit_code == 1
    assert "Could not prepare the trajectory snapshot" in r.output


def test_report_bug_freeze_failure_cleans_staging(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    _simple_log(state)

    def _boom(*_a, **_k):
        raise breport.PreparationError("disk full")

    monkeypatch.setattr(breport, "freeze_export", _boom)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    assert "Could not prepare the trajectory snapshot: disk full" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_stale_at_confirmation_cleans_staging(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    _simple_log(state)

    def _stale(*_a, **_k):
        raise breport.StaleAttemptError("changed")

    monkeypatch.setattr(breport, "confirm_and_package", _stale)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    assert "The attempt changed" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_blank_description_fails_before_side_effects(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport
    from raven.trajectory import store as tstore_module

    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "   ", "--yes"])

    assert r.exit_code == 1
    assert "--description must not be blank" in r.output
    assert breport.list_reports(state) == []
    assert not (breport.bugreports_root(state) / breport.STAGING_DIR).exists()
    assert tstore_module.pins(state) == {}


def test_report_bug_blocked_by_description_has_problem_wording(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", f"look: {_PEM}", "--yes"])

    assert r.exit_code == 1
    assert "Cannot create a bug report with these details." in r.output
    assert "without pasting the key itself" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_blocked_by_trajectory_has_source_wording(state, _no_machine_secrets):
    _simple_log(state, attrs={"llm.output": _PEM})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    assert "Cannot create a bug report from this attempt." in r.output
    assert "raven trajectory report" in r.output


def test_report_bug_tty_confirmation_shows_canonical_summary(state, _no_machine_secrets, monkeypatch):
    """The TTY confirmation must display every shipped field; rejecting keeps nothing."""
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    _simple_log(state)
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(tcmd.typer, "confirm", lambda *_a, **_k: False)

    r = runner.invoke(
        trajectory_app,
        [
            "report-bug",
            "trace-1",
            "-d",
            "wrong language",
            "--expected",
            "a reply in Chinese",
            "--actual",
            "the reply was in English",
            "--severity",
            "medium",
            "--steps",
            "ask anything in Chinese",
            "--reporter",
            "forrest",
        ],
    )

    assert r.exit_code == 1
    for needle in (
        "Bug report summary",
        "Attempt:",
        "member trace(s)",
        "wrong language",
        "a reply in Chinese",
        "the reply was in English",
        "medium",
        "ask anything in Chinese",
        "forrest (included in the package)",
        "Completeness:",
        "NOT anonymized",
    ):
        assert needle in r.output, needle
    assert "Cancelled — no bug report was created." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_summary_shows_session_and_trajectory_context(state, _no_machine_secrets, monkeypatch):
    from raven.cli import trajectory_commands as tcmd

    _simple_log(state)
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(tcmd.typer, "confirm", lambda *_a, **_k: False)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "context check"])

    assert r.exit_code == 1
    assert "Session:" in r.output and "cli:a" in r.output
    assert "last activity" in r.output
    assert "Trajectory:" in r.output and "span(s)" in r.output
    assert "session record" in r.output


def test_report_bug_needs_review_shows_sanitized_samples(state, _no_machine_secrets, monkeypatch):
    """The independent authorization is judged on the bounded sample block."""
    from raven.cli import trajectory_commands as tcmd

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])
    assert r.exit_code == 0, r.output
    assert "residual scan flagged" in r.output
    assert "spans.jsonl:" in r.output

    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    confirms: list[str] = []

    def _reject(message, *_a, **_k):
        confirms.append(message)
        return False

    monkeypatch.setattr(tcmd.typer, "confirm", _reject)
    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r2 = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x"])
    assert r2.exit_code == 1
    assert "spans.jsonl:" in r2.output
