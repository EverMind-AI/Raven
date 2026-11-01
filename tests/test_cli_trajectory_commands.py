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


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Widen the listing console for the whole file.

    ``env={"COLUMNS": ...}`` on the invoke cannot do it here: the suite runs
    under xdist, whose workers export ``COLUMNS=80``, and ``Console()`` freezes
    that into its own width when the module is imported -- long before any
    invoke sets the variable. A narrow table folds ids across lines and the
    assertions read the folded halves as missing.
    """
    from rich.console import Console

    from raven.cli import trajectory_commands as tcmd

    monkeypatch.setattr(tcmd, "console", Console(width=200))


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
    monkeypatch.setattr("raven.home._current_config_path", cfg)
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


def test_replay_json_outputs_only_the_machine_readable_report(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path)

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--json"])

    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["schema_version"] == 1
    assert payload["manifest"] == {"attempt_id": "att-r", "format_version": 1}
    assert payload["bundle"] == "att-r"
    assert payload["mode"] == "warn"
    assert payload["halted"] is False and payload["complete"] is True
    assert payload["llm_calls"] == {"replayed": 1, "recorded": 1, "streamed": 0}
    assert payload["divergences"] == []


def test_replay_json_emits_full_report_before_halt_exit_2(state, tmp_path) -> None:
    bundle = _replay_bundle(
        tmp_path,
        recorded_input={"model": "m", "messages": [{"role": "user", "content": "never"}], "tools": []},
    )

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--strict", "--json"])

    assert r.exit_code == 2, r.output
    payload = json.loads(r.stdout)
    assert payload["halted"] is True
    assert payload["divergences"], payload
    assert payload["divergences"][0]["fatal"] is True
    assert payload["divergences"][0]["kind"] == "llm"


def test_replay_json_out_writes_file_and_keeps_stdout_clean(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path)
    out = tmp_path / "reports" / "replay.json"

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--json", "--out", str(out)])

    assert r.exit_code == 0, r.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert r.stdout.strip() == ""


def test_replay_out_without_json_exits_1(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path)

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--out", str(tmp_path / "x.json")])

    assert r.exit_code == 1
    assert "--out requires --json" in r.stdout


def test_replay_json_manifest_summary_is_null_for_unreadable_manifest(state, tmp_path) -> None:
    bundle = _replay_bundle(tmp_path)
    (bundle / "manifest.json").write_text(json.dumps({"attempt_id": {"nested": True}}), encoding="utf-8")

    r = runner.invoke(trajectory_app, ["replay", str(bundle), "--json"])

    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["manifest"] is None


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

# Assembled at runtime: the detect-private-key pre-commit hook scans source
# bytes for the marker substring and cannot tell this fake fixture apart.
_PEM = "-----BEGIN PRIVATE " + "KEY-----\nMIIabcdef\n-----END PRIVATE " + "KEY-----"
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


def test_report_bug_backslashes_in_fields_do_not_stop_the_report(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state)
    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "problem: \\n- step one", "--steps", "matches \\d+ digits", "--yes"],
    )

    assert r.exit_code == 0, r.output
    assert "Could not prepare" not in r.output
    assert "ready (local_ready)" in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["problem"]["description"] == "problem: \\n- step one"
    assert record["problem"]["steps"] == "matches \\d+ digits"
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


def test_report_bug_suspected_without_flags_lists_items_and_both_flags(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 1
    assert "1 item(s) require your decision" in r.output
    assert "Paths are relative to the trajectory snapshot inside the package." in r.output
    assert "Suspected: high-entropy token" in r.output
    assert f"Token: {_ENTROPY_TOKEN}" in r.output
    assert "Seen at:" in r.output
    assert "the span log — spans.jsonl:1" in r.output
    assert "--keep-findings or --redact-findings" in r.output
    assert "--accept-risk" in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def _inner_spans_text(record, tmp_path):
    import tarfile as _tarfile

    with _tarfile.open(record["package"]["path"]) as tar:
        inner = tar.extractfile(f"{record['report_id']}/trajectory/trace-1.tar.gz").read()
    inner_tar = tmp_path / "inner.tar.gz"
    inner_tar.write_bytes(inner)
    extract_dir = tmp_path / "inner"
    with _tarfile.open(inner_tar) as tar:
        tar.extractall(extract_dir, filter="data")
    return (extract_dir / "trace-1" / "spans.jsonl").read_text(encoding="utf-8")


def test_report_bug_keep_findings_with_accept_risk_ships_kept(state, _no_machine_secrets, tmp_path):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "it broke", "--yes", "--keep-findings", "--accept-risk"],
    )

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["redaction"]["classification"] == "needs_review"
    assert record["status"] == "local_ready"
    assert [entry["action"] for entry in record["redaction"]["user_decisions"]] == ["kept"]
    assert _ENTROPY_TOKEN not in json.dumps(record["redaction"]["user_decisions"])
    assert _ENTROPY_TOKEN in _inner_spans_text(record, tmp_path)


def test_report_bug_redact_findings_with_accept_risk_ships_scrubbed(state, _no_machine_secrets, tmp_path):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "it broke", "--yes", "--redact-findings", "--accept-risk"],
    )

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["status"] == "local_ready"
    assert [entry["action"] for entry in record["redaction"]["user_decisions"]] == ["redacted"]
    assert _ENTROPY_TOKEN not in json.dumps(record)
    spans = _inner_spans_text(record, tmp_path)
    assert _ENTROPY_TOKEN not in spans
    assert "[REDACTED:user-confirmed]" in spans


def test_report_bug_flags_are_mutually_exclusive(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport
    from raven.trajectory import store as tstore_module

    _simple_log(state)
    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "x", "--yes", "--keep-findings", "--redact-findings"],
    )

    assert r.exit_code == 1
    assert "mutually exclusive" in r.output
    assert breport.list_reports(state) == []
    assert not (breport.bugreports_root(state) / breport.STAGING_DIR).exists()
    assert tstore_module.pins(state) == {}


@pytest.mark.parametrize("flag", ["--keep-findings", "--redact-findings"])
def test_report_bug_flag_without_accept_risk_fails_before_application(state, _no_machine_secrets, flag):
    """The missing-flag failure must precede any decision application: the
    original context is listed intact and nothing was replaced or frozen."""
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", flag])

    assert r.exit_code == 1
    assert "Suspected: high-entropy token" in r.output
    assert _ENTROPY_TOKEN in r.output
    assert "--accept-risk" in r.output
    assert "--keep-findings or --redact-findings" not in r.output
    assert "[REDACTED:user-confirmed]" not in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_accept_risk_without_keep_or_redact_lists_that_flag(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 1
    assert "--keep-findings or --redact-findings" in r.output
    assert "Pass: --keep-findings or --redact-findings (--yes does not imply them)." in r.output
    assert breport.list_reports(state) == []


def test_report_bug_both_kinds_ship_with_full_flags(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN} and {_PEM}"})
    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "x", "--yes", "--redact-findings", "--accept-risk"],
    )

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    actions = sorted(entry["action"] for entry in record["redaction"]["user_decisions"])
    assert actions == ["acknowledged", "redacted"]
    assert record["redaction"]["security_notices"] != []


def test_report_bug_policy_only_review_needs_accept_risk(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_REQUIRE_REVIEW", "1")
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])
    assert r.exit_code == 1
    assert "organization policy requires manual review" in r.output
    assert breport.list_reports(state) == []

    r2 = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])
    assert r2.exit_code == 0, r2.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["redaction"]["classification"] == "needs_review"
    assert record["redaction"]["user_decisions"] == []


def test_report_bug_tty_full_flags_skip_all_prompts(state, _no_machine_secrets, monkeypatch):
    """On a TTY, flags pre-decide their kinds: with everything covered plus
    --yes, no questionary prompt may appear at all."""
    import raven.cli.trajectory_browse as tbrowse
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    def _no_prompts():
        raise AssertionError("questionary must not be used when flags cover every item")

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(tbrowse, "_require_questionary", _no_prompts)

    r = runner.invoke(
        trajectory_app,
        ["report-bug", "trace-1", "-d", "x", "--yes", "--keep-findings", "--accept-risk"],
    )

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    assert [entry["action"] for entry in record["redaction"]["user_decisions"]] == ["kept"]


def test_report_bug_tty_flags_without_accept_risk_still_confirm_risk(state, _no_machine_secrets, monkeypatch):
    """--keep-findings covers the items, but the risk consent still gates the
    ship on a TTY when --accept-risk was not given; declining keeps nothing."""
    import raven.cli.trajectory_browse as tbrowse
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    def _no_prompts():
        raise AssertionError("no per-item prompt is expected: the flag covers every item")

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(tbrowse, "_require_questionary", _no_prompts)
    confirms: list[str] = []

    def _reject(message, *_a, **_k):
        confirms.append(message)
        return False

    monkeypatch.setattr(tcmd.typer, "confirm", _reject)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--keep-findings"])

    assert r.exit_code == 1
    assert confirms == ["Ship the package with the risks listed above?"]
    assert "Cancelled — no bug report was created." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_private_key_ships_with_accept_risk(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": _PEM})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["status"] == "local_ready"
    assert record["redaction"]["security_notices"] == [
        "the original trajectory contained 1 private key block(s), replaced before export"
    ]
    assert [entry["action"] for entry in record["redaction"]["user_decisions"]] == ["acknowledged"]
    assert _PEM not in json.dumps(record)


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


def test_report_bug_private_key_in_description_requires_accept_risk(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", f"look: {_PEM}", "--yes"])

    assert r.exit_code == 1
    assert "Confirmed sensitive: private key block (already replaced)" in r.output
    assert "Pass: --accept-risk (--yes does not imply them)." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_private_key_in_trajectory_requires_accept_risk(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": _PEM})
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    assert "Confirmed sensitive: private key block (already replaced)" in r.output
    assert "Pass: --accept-risk (--yes does not imply them)." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


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


class _FakeReviewQuestionary:
    """Minimal questionary stand-in: every select resolves via the factory.

    No ``application`` attribute, so ``_inject_bindings`` leaves it untouched
    and the real ``_ask`` / ``_ask_action`` conversion logic runs.
    """

    class Choice:
        def __init__(self, title, value=None):
            self.title = title
            self.value = value if value is not None else title

    def __init__(self, result_factory):
        self._factory = result_factory

    def select(self, *_a, **_k):
        factory = self._factory
        return type("_Ask", (), {"unsafe_ask": staticmethod(factory)})()

    def text(self, *_a, **_k):
        factory = self._factory
        return type("_Ask", (), {"unsafe_ask": staticmethod(factory)})()


def _tty_review_setup(state, monkeypatch, attrs):
    from raven.cli import trajectory_commands as tcmd

    _simple_log(state, attrs=attrs)
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)


def test_report_bug_tty_escape_during_review_cancels(state, _no_machine_secrets, monkeypatch):
    import raven.cli.trajectory_browse as tbrowse
    from raven.trajectory import bugreport as breport

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    monkeypatch.setattr(tbrowse, "_require_questionary", lambda: _FakeReviewQuestionary(lambda: tbrowse._BACK))

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x"])

    assert r.exit_code == 1
    assert "Cancelled — no bug report was created." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_tty_ctrl_c_during_review_cancels(state, _no_machine_secrets, monkeypatch):
    import raven.cli.trajectory_browse as tbrowse
    from raven.trajectory import bugreport as breport

    def _interrupt():
        raise KeyboardInterrupt

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    monkeypatch.setattr(tbrowse, "_require_questionary", lambda: _FakeReviewQuestionary(_interrupt))

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x"])

    assert r.exit_code == 1
    assert "Cancelled — no bug report was created." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_tty_conflict_then_cancel(state, _no_machine_secrets, monkeypatch):
    import raven.cli.trajectory_browse as tbrowse
    from raven.trajectory import bugreport as breport

    inner = "Hj5tR8uE3iO7pA1sD4fGk9lZ"
    outer = inner + "W6xC2v"
    _tty_review_setup(state, monkeypatch, {"llm.output": f"a {inner} b {outer}"})
    answers = iter(["k", "r", "c"])
    monkeypatch.setattr(tbrowse, "_require_questionary", lambda: _FakeReviewQuestionary(lambda: next(answers)))

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x"])

    assert r.exit_code == 1
    assert "must share one decision" in r.output
    assert "Cancelled — no bug report was created." in r.output
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_summary_shows_semantic_decisions(state, _no_machine_secrets, monkeypatch):
    """The confirmation summary lists every decision with its semantic source."""
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--keep-findings", "--accept-risk"])
    assert r.exit_code == 0, r.output
    assert "residual scan flagged" in r.output
    assert "decision(s) complete" in r.output
    assert "the span log" in r.output
    assert _ENTROPY_TOKEN not in r.output

    # TTY path: the interactive decider is stubbed to keep-all; declining the
    # single risk confirmation must leave nothing behind.
    import raven.cli.trajectory_browse as tbrowse
    from raven.trajectory import review as treview

    def _keep_all(_questionary, _style, **_kwargs):
        def _decide(items, _reasons):
            return [treview.ReviewDecision(item.id, treview.ACTION_KEPT) for item in items]

        return _decide

    monkeypatch.setattr(tbrowse, "make_review_decider", _keep_all)
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    confirms: list[str] = []

    def _reject(message, *_a, **_k):
        confirms.append(message)
        return False

    monkeypatch.setattr(tcmd.typer, "confirm", _reject)
    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    r2 = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x"])
    assert r2.exit_code == 1
    assert "the span log" in r2.output
    assert confirms == ["Ship the package with the risks listed above?"]
    assert len(breport.list_reports(state)) == 1  # only the first run's report
    assert _staging_entries(state) == []


# ── report-bug redaction history ──────────────────────────────────────

_SECOND_TOKEN = "Qw8eR3tY6uI1oP4aS7dF2gH5"


def _history():
    from raven.trajectory import redaction_history as thistory

    return thistory.RedactionHistory()


def _seed(token, action="kept"):
    return _history().record(token, action, category="high-entropy", source="cli")


def _one_line(text):
    return " ".join(text.split())


def _no_asking():
    raise AssertionError("no item may be asked")


def _actions(record):
    return [entry["action"] for entry in record["redaction"]["user_decisions"]]


def _ask_with(monkeypatch, answer):
    import raven.cli.trajectory_browse as tbrowse

    monkeypatch.setattr(tbrowse, "_require_questionary", lambda: _FakeReviewQuestionary(answer))


def test_report_bug_tty_replays_a_remembered_decision(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    args = ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"]
    _ask_with(monkeypatch, lambda: "k")
    r = runner.invoke(trajectory_app, args)
    assert r.exit_code == 0, r.output
    first = _history().lookup(_ENTROPY_TOKEN)
    assert (first.action, first.source, first.applied) == ("kept", "cli", 0)

    _ask_with(monkeypatch, _no_asking)
    r2 = runner.invoke(trajectory_app, args)

    assert r2.exit_code == 0, r2.output
    out = _one_line(r2.output)
    assert "Redaction review — every item was decided from your redaction history" in out
    assert "1 item(s) decided from your redaction history:" in out
    assert "-- kept (decided " in out and "reused 0 time(s)" in out
    assert [_actions(record) for _dir, record in breport.list_reports(state)] == [["kept"], ["kept"]]
    second = _history().lookup(_ENTROPY_TOKEN)
    assert second.applied == 1 and second.revision == first.revision


def test_report_bug_non_tty_history_covers_every_finding(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    assert "1 item(s) decided from your redaction history:" in _one_line(r.output)
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["kept"]
    assert _history().lookup(_ENTROPY_TOKEN).applied == 1


def test_report_bug_non_tty_history_still_needs_accept_risk(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN)
    before = _history().path.read_bytes()

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    out = _one_line(r.output)
    assert "The redaction needs review:" in out
    assert "1 item(s) decided from your redaction history:" in out
    assert "Pass: --accept-risk (--yes does not imply them)." in out
    assert "--keep-findings or --redact-findings" not in out
    assert _history().path.read_bytes() == before
    assert breport.list_reports(state) == []
    assert _staging_entries(state) == []


def test_report_bug_non_tty_partial_history_lists_the_rest(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN} and {_SECOND_TOKEN}"})
    _seed(_ENTROPY_TOKEN)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 1
    out = _one_line(r.output)
    assert "1 item(s) require your decision:" in out
    assert "1 item(s) decided from your redaction history:" in out
    assert f"Token: {_SECOND_TOKEN}" in out
    assert f"Token: {_ENTROPY_TOKEN}" not in out
    assert "Pass: --keep-findings or --redact-findings (--yes does not imply them)." in out
    assert breport.list_reports(state) == []


def test_report_bug_flags_win_over_history_without_touching_it(state, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN)
    before = _history().path.read_bytes()

    r = runner.invoke(
        trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk", "--redact-findings"]
    )

    assert r.exit_code == 0, r.output
    assert "decided from your redaction history" not in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["redacted"]
    assert _history().path.read_bytes() == before


def test_report_bug_blanket_flags_are_not_remembered(state, _no_machine_secrets):
    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk", "--keep-findings"])

    assert r.exit_code == 0, r.output
    assert not _history().path.exists()


def test_report_bug_review_all_asks_again_and_replaces(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    seeded = _seed(_ENTROPY_TOKEN)
    _ask_with(monkeypatch, lambda: "r")

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk", "--review-all"])

    assert r.exit_code == 0, r.output
    assert "Redaction review — 1 item(s) need your decision" in _one_line(r.output)
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["redacted"]
    entry = _history().lookup(_ENTROPY_TOKEN)
    assert entry.action == "redacted" and entry.revision != seeded.revision and entry.applied == 0


def test_report_bug_review_all_without_tty_ignores_history(state, _no_machine_secrets):
    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN)
    before = _history().path.read_bytes()

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk", "--review-all"])

    assert r.exit_code == 1
    out = _one_line(r.output)
    assert "Pass: --keep-findings or --redact-findings (--yes does not imply them)." in out
    assert "decided from your redaction history" not in out
    assert _history().path.read_bytes() == before


def test_report_bug_history_switched_off(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_REDACTION_HISTORY", "off")
    _simple_log(state, attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN, "redacted")
    before = _history().path.read_bytes()

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])
    assert r.exit_code == 1
    assert "Pass: --keep-findings or --redact-findings" in _one_line(r.output)

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    _ask_with(monkeypatch, lambda: "k")
    r2 = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r2.exit_code == 0, r2.output
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["kept"]
    assert _history().path.read_bytes() == before


def test_report_bug_cancelled_review_remembers_nothing(state, _no_machine_secrets, monkeypatch):
    import raven.cli.trajectory_browse as tbrowse

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    _ask_with(monkeypatch, lambda: tbrowse._BACK)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 1
    assert not _history().path.exists()


def test_report_bug_declined_ship_still_remembers_decisions(state, _no_machine_secrets, monkeypatch):
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    _ask_with(monkeypatch, lambda: "k")
    monkeypatch.setattr(tcmd.typer, "confirm", lambda *_a, **_k: False)

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes"])

    assert r.exit_code == 1
    assert breport.list_reports(state) == []
    assert _history().lookup(_ENTROPY_TOKEN).action == "kept"


def test_report_bug_corrupt_history_asks_and_backs_up(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    path = _history().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")
    _ask_with(monkeypatch, lambda: "k")

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    out = _one_line(r.output)
    assert "Redaction history could not be read (not valid JSON (line 1))" in out
    assert "Backed up the unreadable redaction history to" in out
    assert len(list(breport.list_reports(state))) == 1
    (backup,) = path.parent.glob(f"{path.name}.corrupt-*")
    assert backup.read_bytes() == b"{not json"
    assert _history().lookup(_ENTROPY_TOKEN).action == "kept"


def test_report_bug_unreadable_history_asks_every_item(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport
    from raven.trajectory import redaction_history as thistory

    def _denied(self, _items):
        raise thistory.HistoryStorageError("cannot read X: Permission denied")

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    _seed(_ENTROPY_TOKEN)
    monkeypatch.setattr(thistory.RedactionHistory, "resolve", _denied)
    _ask_with(monkeypatch, lambda: "r")

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    assert "Redaction history is unavailable (cannot read X: Permission denied)" in _one_line(r.output)
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["redacted"]


def test_report_bug_unsaved_history_still_ships(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import bugreport as breport
    from raven.trajectory import redaction_history as thistory

    def _full(self, *_args, **_kwargs):
        raise thistory.HistoryStorageError("cannot save X: No space left on device")

    _tty_review_setup(state, monkeypatch, {"llm.output": f"token {_ENTROPY_TOKEN}"})
    monkeypatch.setattr(thistory.RedactionHistory, "remember", _full)
    _ask_with(monkeypatch, lambda: "k")

    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "x", "--yes", "--accept-risk"])

    assert r.exit_code == 0, r.output
    assert "Your decisions were not remembered: cannot save X: No space left on device" in _one_line(r.output)
    ((_dir, record),) = breport.list_reports(state)
    assert _actions(record) == ["kept"]


# ── redaction-history management ──────────────────────────────────────


def _rh(*args):
    return runner.invoke(trajectory_app, ["redaction-history", *args])


def _storage_failure(monkeypatch, method):
    from raven.trajectory import redaction_history as thistory

    def _fail(self, *_args, **_kwargs):
        raise thistory.HistoryStorageError("cannot save X: No space left on device")

    monkeypatch.setattr(thistory.RedactionHistory, method, _fail)


def _write_corrupt_history():
    path = _history().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")
    return path


def test_redaction_history_list_empty():
    r = _rh("list")

    assert r.exit_code == 0, r.output
    assert "No remembered redaction decisions." in r.output


def test_redaction_history_list_shows_masked_entries():
    kept = _seed(_ENTROPY_TOKEN)
    replaced = _seed(_SECOND_TOKEN, "redacted")

    r = _rh("list")

    assert r.exit_code == 0, r.output
    out = _one_line(r.output)
    for entry in (kept, replaced):
        assert entry.id in out and entry.masked in out
    assert " kept " in out and " replaced " in out
    assert "2 remembered decision(s) in" in out
    assert _ENTROPY_TOKEN not in r.output and _SECOND_TOKEN not in r.output


def test_redaction_history_list_reports_an_unreadable_file():
    path = _write_corrupt_history()

    r = _rh("list")

    assert r.exit_code == 1
    assert "could not be read (not valid JSON (line 1)); it is backed up on the next save" in _one_line(r.output)
    assert path.read_bytes() == b"{not json"


def test_redaction_history_list_storage_error(monkeypatch):
    _storage_failure(monkeypatch, "read")

    r = _rh("list")

    assert r.exit_code == 1
    assert "cannot save X: No space left on device" in _one_line(r.output)


def test_redaction_history_list_notes_the_switch_is_off(monkeypatch):
    entry = _seed(_ENTROPY_TOKEN)
    monkeypatch.setenv("RAVEN_BUGREPORT_REDACTION_HISTORY", "off")

    r = _rh("list")

    assert r.exit_code == 0, r.output
    out = _one_line(r.output)
    assert "RAVEN_BUGREPORT_REDACTION_HISTORY is off" in out
    assert entry.id in out


def test_redaction_history_forget_by_id():
    entry = _seed(_ENTROPY_TOKEN)

    r = _rh("forget", entry.id)

    assert r.exit_code == 0, r.output
    assert f"Forgot {entry.id}." in r.output
    assert _history().lookup(_ENTROPY_TOKEN) is None


def test_redaction_history_forget_unknown_id_removes_nothing():
    kept = _seed(_ENTROPY_TOKEN)
    _seed(_SECOND_TOKEN)

    r = _rh("forget", kept.id, "ffffffffffff")

    assert r.exit_code == 1
    out = _one_line(r.output)
    assert "No remembered decision matches ffffffffffff." in out
    assert "Nothing was forgotten." in out
    assert len(_history().read().entries) == 2


def test_redaction_history_forget_ambiguous_prefix():
    path = _history().path
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "action": "kept",
        "category": "high-entropy",
        "masked": "abcd***wxyz",
        "decided_at": "2026-10-01T00:00:00.000000+00:00",
        "source": "cli",
        "revision": "0123456789abcdef",
        "applied": 0,
        "last_applied_at": None,
    }
    prefix = "abcdef012345"
    entries = {prefix + "0" * 52: entry, prefix + "1" * 52: entry}
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")

    r = _rh("forget", prefix)

    assert r.exit_code == 1
    assert f"{prefix} matches more than one decision; give more digits." in _one_line(r.output)
    assert len(_history().read().entries) == 2


def test_redaction_history_forget_storage_error(monkeypatch):
    entry = _seed(_ENTROPY_TOKEN)
    _storage_failure(monkeypatch, "forget")

    r = _rh("forget", entry.id)

    assert r.exit_code == 1
    assert "nothing was forgotten" in _one_line(r.output)


def test_redaction_history_clear_needs_yes_without_a_tty():
    _seed(_ENTROPY_TOKEN)

    r = _rh("clear")

    assert r.exit_code == 1
    assert "--yes is required without a TTY; nothing was cleared." in _one_line(r.output)
    assert len(_history().read().entries) == 1


@pytest.mark.parametrize("answer", [False, True])
def test_redaction_history_clear_confirms_on_a_tty(monkeypatch, answer):
    from raven.cli import trajectory_commands as tcmd

    _seed(_ENTROPY_TOKEN)
    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(tcmd.typer, "confirm", lambda *_a, **_k: answer)

    r = _rh("clear")

    if answer:
        assert r.exit_code == 0, r.output
        assert "Removed 1 remembered decision(s)." in r.output
        assert _history().read().entries == []
    else:
        assert r.exit_code == 1
        assert "Cancelled — nothing was cleared." in r.output
        assert len(_history().read().entries) == 1


def test_redaction_history_clear_with_yes():
    _seed(_ENTROPY_TOKEN)
    _seed(_SECOND_TOKEN)

    r = _rh("clear", "--yes")
    assert r.exit_code == 0, r.output
    assert "Removed 2 remembered decision(s)." in r.output

    _history().path.unlink()
    r2 = _rh("clear", "--yes")
    assert r2.exit_code == 0, r2.output
    assert "Removed 0 remembered decision(s)." in r2.output
    assert not _history().path.exists()


def test_redaction_history_clear_backs_up_an_unreadable_file():
    path = _write_corrupt_history()

    r = _rh("clear", "--yes")

    assert r.exit_code == 0, r.output
    assert "Backed up the unreadable redaction history to" in _one_line(r.output)
    (backup,) = path.parent.glob(f"{path.name}.corrupt-*")
    assert backup.read_bytes() == b"{not json"
    assert "No remembered redaction decisions." in _rh("list").output


def test_redaction_history_clear_storage_error(monkeypatch):
    _seed(_ENTROPY_TOKEN)
    _storage_failure(monkeypatch, "clear")

    r = _rh("clear", "--yes")

    assert r.exit_code == 1
    assert "nothing was cleared" in _one_line(r.output)
    assert len(_history().read().entries) == 1


# ── report-bug upload (RAVEN_BUGREPORT_URL) ───────────────────────────


@pytest.fixture
def no_proxy_env(monkeypatch):
    from tests.bug_report_receiver_stub import clear_proxy_env

    clear_proxy_env(monkeypatch)


@pytest.fixture
def receiver_url(tmp_path, no_proxy_env):
    from tests.bug_report_receiver_stub import receiver_thread

    with receiver_thread(tmp_path / "inbox") as url:
        yield url


def _dead_url():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_report_bug_uploads_when_a_receiver_is_configured(
    state, _no_machine_secrets, receiver_url, monkeypatch, tmp_path
):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_URL", receiver_url)
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 0, r.output
    assert f"Will upload to {receiver_url} after the package is created." in r.output
    assert "Uploaded — receipt rc-" in r.output
    assert "Not uploaded" not in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["upload"]["state"] == "uploaded"
    assert list((tmp_path / "inbox").glob(f"*/{record['report_id']}.tar.gz"))


def test_report_bug_without_a_receiver_keeps_the_old_words(state, _no_machine_secrets, monkeypatch):
    monkeypatch.delenv("RAVEN_BUGREPORT_URL", raising=False)
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 0, r.output
    assert "Nothing will be uploaded" in r.output and "Not uploaded" in r.output


@pytest.mark.parametrize(
    "url", ["http://10.1.2.3", "https://[broken", "https://ops.example:notaport", "https://ops.example:99999"]
)
def test_report_bug_misconfigured_url_fails_before_side_effects(state, _no_machine_secrets, monkeypatch, url):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_URL", url)
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 1 and "misconfigured" in r.output
    assert not breport.bugreports_root(state).exists()


def test_report_bug_upload_failure_keeps_the_package(state, _no_machine_secrets, monkeypatch, no_proxy_env):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_URL", _dead_url())
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 1, r.output
    assert "is not reachable" in r.output
    assert "✗ Upload failed:" in r.output and "raven trajectory upload-bug-report br-" in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["status"] == "local_ready"
    assert record["upload"]["state"] == "failed" and record["upload"]["error"]


def _record_confirms(monkeypatch, *answers):
    """Script typer.confirm with ``answers`` in order; returns the messages it was asked."""
    from raven.cli import trajectory_commands as tcmd

    asked: list[str] = []
    replies = list(answers)

    def _confirm(message, *_a, **_k):
        asked.append(message)
        return replies.pop(0)

    monkeypatch.setattr(tcmd.typer, "confirm", _confirm)
    return asked


def test_report_bug_probes_a_reachable_receiver_without_asking(state, _no_machine_secrets, receiver_url, monkeypatch):
    from raven.trajectory import upload as tupload

    probed: list[str] = []
    real_probe = tupload.probe_receiver

    def _counting(base_url, **kw):
        probed.append(base_url)
        return real_probe(base_url, **kw)

    monkeypatch.setattr(tupload, "probe_receiver", _counting)
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", receiver_url)
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 0, r.output
    assert probed == [receiver_url]
    assert "not reachable" not in r.output and "Uploaded — receipt rc-" in r.output


def test_report_bug_without_a_receiver_does_not_probe(state, _no_machine_secrets, monkeypatch):
    from raven.trajectory import upload as tupload

    def _no_probe(*_a, **_k):
        raise AssertionError("nothing is probed when uploading is off")

    monkeypatch.setattr(tupload, "probe_receiver", _no_probe)
    monkeypatch.delenv("RAVEN_BUGREPORT_URL", raising=False)
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 0, r.output
    assert "Not uploaded" in r.output


def test_report_bug_unreachable_receiver_declined_collects_nothing(
    state, _no_machine_secrets, monkeypatch, no_proxy_env
):
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport
    from raven.trajectory import store as tstore_module

    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    asked = _record_confirms(monkeypatch, False)
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", _dead_url())
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke"])

    assert r.exit_code == 1, r.output
    assert "is not reachable: could not reach 127.0.0.1:" in _one_line(r.output)
    assert "nothing was collected or pinned" in r.output
    assert asked == [tcmd._CONTINUE_WITHOUT_RECEIVER]
    assert breport.list_reports(state) == []
    assert not (breport.bugreports_root(state) / breport.STAGING_DIR).exists()
    assert tstore_module.pins(state) == {}


def test_report_bug_unreachable_receiver_accepted_keeps_the_package(
    state, _no_machine_secrets, monkeypatch, no_proxy_env
):
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: True)
    asked = _record_confirms(monkeypatch, True, True)
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", _dead_url())
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke"])

    assert r.exit_code == 1, r.output
    assert asked == [tcmd._CONTINUE_WITHOUT_RECEIVER, "Create the bug report?"]
    assert "✗ Upload failed:" in r.output and "raven trajectory upload-bug-report br-" in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["status"] == "local_ready" and record["upload"]["state"] == "failed"


@pytest.mark.parametrize("tty", [False, True], ids=["no-tty", "tty"])
def test_report_bug_unreachable_receiver_with_yes_continues_without_asking(
    state, _no_machine_secrets, monkeypatch, no_proxy_env, tty
):
    from raven.cli import trajectory_commands as tcmd
    from raven.trajectory import bugreport as breport

    def _no_confirm(*_a, **_k):
        raise AssertionError("--yes must not ask whether to continue")

    monkeypatch.setattr(tcmd, "_stdin_is_tty", lambda: tty)
    monkeypatch.setattr(tcmd.typer, "confirm", _no_confirm)
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", _dead_url())
    _simple_log(state)
    r = runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"])

    assert r.exit_code == 1, r.output
    assert "is not reachable" in r.output
    assert "Continuing: if the upload fails, the package stays on this machine." in r.output
    ((_dir, record),) = breport.list_reports(state)
    assert record["status"] == "local_ready" and record["upload"]["state"] == "failed"


def test_upload_bug_report_retries_and_repeats(state, _no_machine_secrets, receiver_url, monkeypatch):
    from raven.trajectory import bugreport as breport

    monkeypatch.setenv("RAVEN_BUGREPORT_URL", _dead_url())
    _simple_log(state)
    assert runner.invoke(trajectory_app, ["report-bug", "trace-1", "-d", "it broke", "--yes"]).exit_code == 1
    ((_dir, record),) = breport.list_reports(state)

    monkeypatch.setenv("RAVEN_BUGREPORT_URL", receiver_url)
    r = runner.invoke(trajectory_app, ["upload-bug-report", record["report_id"]])
    assert r.exit_code == 0, r.output
    assert "Uploaded — receipt rc-" in r.output
    assert breport.load_record(_dir)["upload"]["state"] == "uploaded"

    r = runner.invoke(trajectory_app, ["upload-bug-report", record["report_id"]])
    assert r.exit_code == 0 and "Already uploaded — receipt rc-" in r.output


def test_upload_bug_report_argument_errors(state, _no_machine_secrets, receiver_url, monkeypatch):
    monkeypatch.delenv("RAVEN_BUGREPORT_URL", raising=False)
    r = runner.invoke(trajectory_app, ["upload-bug-report", "br-20260904-abcdef"])
    assert r.exit_code == 2 and "No upload URL is configured" in r.output

    r = runner.invoke(trajectory_app, ["upload-bug-report", "br-20260904-abcdef", "--url", receiver_url])
    assert r.exit_code == 1 and "Unknown bug report" in r.output

    r = runner.invoke(trajectory_app, ["upload-bug-report", "../x", "--url", receiver_url])
    assert r.exit_code == 1 and "Unknown bug report" in r.output

    r = runner.invoke(trajectory_app, ["upload-bug-report", "br-20260904-abcdef", "--url", "http://10.1.2.3"])
    assert r.exit_code == 1 and "misconfigured" in r.output


# ── receiver (run the Bug Report Receiver) ─────────────────────────────


def _run_receiver_stub(monkeypatch, *, raise_exc=None, announce=False):
    from raven.trajectory import receiver as trecv

    calls = []

    def _run(config, *, ready=None):
        calls.append((config, ready))
        if announce and ready is not None:
            ready("127.0.0.1", 18795)
        if raise_exc is not None:
            raise raise_exc

    monkeypatch.setattr(trecv, "run_receiver", _run)
    return calls


def test_receiver_defaults_map_to_the_config(tmp_path, monkeypatch):
    from raven.trajectory import receiver as trecv

    calls = _run_receiver_stub(monkeypatch, announce=True)
    r = runner.invoke(trajectory_app, ["receiver", "--save-dir", str(tmp_path / "inbox")])

    assert r.exit_code == 0, r.output
    ((config, ready),) = calls
    assert isinstance(config, trecv.ReceiverConfig)
    assert config.save_dir == (tmp_path / "inbox").resolve()
    assert (config.host, config.port, config.trusted_proxies) == ("127.0.0.1", 18795, ())
    assert (config.limits.max_package_bytes, config.limits.max_unpacked_bytes) == (64 * 2**20, 256 * 2**20)
    assert (config.max_daily_bytes, config.max_daily_count, config.min_free_bytes) == (2048 * 2**20, 500, 1024 * 2**20)
    assert (config.rate_burst, config.rate_per_10min, config.concurrency) == (10, 30, 4)
    assert (config.idle_timeout, config.total_timeout) == (30.0, 600.0)
    assert ready is not None
    assert "listening on http://127.0.0.1:18795" in r.output
    assert "Saving packages under" in r.output and "Press Ctrl-C to stop." in r.output
    assert "Bug report receiver stopped." in r.output


def test_receiver_options_map_to_the_config(tmp_path, monkeypatch):
    calls = _run_receiver_stub(monkeypatch)
    args = [
        "receiver",
        "--save-dir",
        str(tmp_path),
        "--host",
        "0.0.0.0",
        "--port",
        "0",
        "--trusted-proxy",
        "10.0.0.0/8",
        "--trusted-proxy",
        "fd00::/8",
        "--max-package-mib",
        "1",
        "--max-unpacked-mib",
        "2",
        "--max-daily-mib",
        "3",
        "--max-daily-count",
        "4",
        "--min-free-mib",
        "5",
        "--rate-burst",
        "0",
        "--rate-per-10min",
        "7",
        "--concurrency",
        "8",
        "--idle-timeout",
        "1.5",
        "--total-timeout",
        "2.5",
    ]
    r = runner.invoke(trajectory_app, args)

    assert r.exit_code == 0, r.output
    ((config, _ready),) = calls
    assert (config.host, config.port) == ("0.0.0.0", 0)
    assert config.trusted_proxies == ("10.0.0.0/8", "fd00::/8")
    assert (config.limits.max_package_bytes, config.limits.max_unpacked_bytes) == (2**20, 2 * 2**20)
    assert (config.max_daily_bytes, config.max_daily_count, config.min_free_bytes) == (3 * 2**20, 4, 5 * 2**20)
    assert (config.rate_burst, config.rate_per_10min, config.concurrency) == (0, 7, 8)
    assert (config.idle_timeout, config.total_timeout) == (1.5, 2.5)


@pytest.mark.parametrize(
    "args,needle",
    [
        (["--trusted-proxy", "nope"], "not a CIDR network"),
        (["--concurrency", "0"], "--concurrency"),
        (["--port", "70000"], "--port"),
    ],
)
def test_receiver_rejects_bad_options(tmp_path, monkeypatch, args, needle):
    calls = _run_receiver_stub(monkeypatch)
    r = runner.invoke(trajectory_app, ["receiver", "--save-dir", str(tmp_path), *args])
    assert r.exit_code == 2, r.output
    assert needle in r.output
    assert calls == []


def test_receiver_refuses_a_file_as_save_dir(tmp_path, monkeypatch):
    calls = _run_receiver_stub(monkeypatch)
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    r = runner.invoke(trajectory_app, ["receiver", "--save-dir", str(target)])
    assert r.exit_code == 1 and "is not a directory" in r.output
    assert calls == []


@pytest.mark.parametrize(
    "exc", [PermissionError(13, "Permission denied", "/srv/inbox"), OSError(28, "No space left on device")]
)
def test_receiver_reports_an_unusable_save_dir(tmp_path, monkeypatch, exc):
    import raven.cli.trajectory_commands as tcmd

    _run_receiver_stub(monkeypatch, raise_exc=exc)
    # A narrow console and a deep path: the diagnostic has to stay one line
    # regardless of how long the temporary directory of the host is.
    monkeypatch.setattr(tcmd.console, "_width", 40)
    deep = tmp_path.joinpath(*(["a-rather-long-directory-name"] * 4), "inbox")
    r = runner.invoke(trajectory_app, ["receiver", "--save-dir", str(deep)])
    assert r.exit_code == 1, r.output
    (line,) = [line for line in r.output.splitlines() if "Cannot prepare" in line]
    assert str(deep) in line and exc.strerror in line
    assert isinstance(r.exception, SystemExit)


def test_backfill_frames_reports_what_it_wrote_and_what_it_closed(monkeypatch):
    from raven.trajectory import frames_backfill

    seen: dict = {}

    def backfill(*, session_key=None, dry_run=False, force=False):
        seen.update(session_key=session_key, dry_run=dry_run)
        return {"runs": 3, "written": 1, "steps": 7, "already": 2, "repaired": 4}

    monkeypatch.setattr(frames_backfill, "backfill", backfill)
    r = runner.invoke(trajectory_app, ["backfill-frames", "--session", "tui:x", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert seen == {"session_key": "tui:x", "dry_run": True}
    assert "7 step span(s) for 1 of 3" in r.output
    assert "2 already on record" in r.output
    assert "4 step(s)" in r.output


def test_backfill_frames_says_how_many_runs_it_could_not_read(monkeypatch):
    from raven.trajectory import frames_backfill

    monkeypatch.setattr(
        frames_backfill,
        "backfill",
        lambda **_: {"runs": 3, "written": 1, "steps": 7, "already": 1, "repaired": 0, "unread": 1},
    )
    r = runner.invoke(trajectory_app, ["backfill-frames"])
    assert r.exit_code == 0, r.output
    assert "1 already on record" in r.output
    assert "1 already on record; 1 with nothing left to read in the journal." in r.output


def test_backfill_frames_says_tracing_is_off_and_how_to_write_anyway(monkeypatch):
    from raven.trajectory import frames_backfill

    seen: list[bool] = []

    def backfill(*, session_key=None, dry_run=False, force=False):
        seen.append(force)
        if not force:
            return {"runs": 0, "written": 0, "steps": 0, "already": 0, "repaired": 0, "tracing_off": 1}
        return {"runs": 1, "written": 1, "steps": 3, "already": 0, "repaired": 0}

    monkeypatch.setattr(frames_backfill, "backfill", backfill)
    off = runner.invoke(trajectory_app, ["backfill-frames"])
    assert off.exit_code == 0, off.output
    assert "Tracing is off" in off.output and "--force" in off.output
    assert "step span(s)" not in off.output

    forced = runner.invoke(trajectory_app, ["backfill-frames", "--force"])
    assert forced.exit_code == 0, forced.output
    assert seen == [False, True]
    assert "Wrote 3 step span(s) for 1 of 1" in forced.output
