"""Tests for the trajectory bundle collector (`raven.trajectory.bundle`)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from raven.session.manager import SessionManager
from raven.trajectory import bundle as tbundle
from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, *, attempt_id=None, session_key=None, name="session.turn", start=None, end=None, attrs=None):
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
        "startTime": start or "2026-08-20T10:00:00+00:00",
        "endTime": end or "2026-08-20T10:00:01+00:00",
        "attributes": attributes,
    }


@pytest.fixture
def state(tmp_path):
    return tmp_path / "traces"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _make_artifact(state, name, content):
    path = state / "logs" / "audit-artifacts" / "tool.output" / "2026-08-20" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_bundle_roundtrip(state, workspace):
    artifact = _make_artifact(state, "output.json", '{"result": 42}')
    manager = SessionManager(workspace)
    session = manager.get_or_create("cli:chat1")
    session.add_message("user", "hello")
    manager.save(session)
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:chat1", start="2026-08-20T10:00:00+00:00"),
            _span(
                "trace-1",
                session_key="cli:chat1",
                name="tool.call",
                end="2026-08-20T10:00:05+00:00",
                attrs={"tool.output.artifact_path": str(artifact)},
            ),
        ],
    )
    tverdict.record_verdict("trace-1", "fail", source="user", why="wrong answer", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert bundle_dir == (state / "bundles" / "trace-1").resolve()
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == tbundle.BUNDLE_FORMAT_VERSION
    assert manifest["attempt_id"] == "trace-1"
    assert manifest["session_key"] == "cli:chat1"
    assert manifest["time_range"] == {"start": "2026-08-20T10:00:00+00:00", "end": "2026-08-20T10:00:05+00:00"}
    assert manifest["span_count"] == 2
    assert manifest["artifact_count"] == 1
    assert manifest["verdict_count"] == 1
    assert manifest["session_included"] is True
    assert manifest["missing_artifacts"] == []
    assert manifest["raven_version"]

    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["name"] for s in spans] == ["session.turn", "tool.call"]
    verdicts = _read_lines(bundle_dir / "verdicts.jsonl")
    assert verdicts[0]["status"] == "fail" and verdicts[0]["why"] == "wrong answer"
    source_session = manager._get_session_path("cli:chat1")
    assert (bundle_dir / "session.jsonl").read_text(encoding="utf-8") == source_session.read_text(encoding="utf-8")


def test_artifact_paths_rewritten_offline(state, workspace, tmp_path):
    artifact = _make_artifact(state, "big-output.json", '{"tokens": [1, 2, 3]}')
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(artifact)})],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace)

    span = _read_lines(bundle_dir / "spans.jsonl")[0]
    rel = span["attributes"]["tool.output.artifact_path"]
    assert rel == "artifacts/big-output.json"
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rewritten_artifact_paths"] == 1

    # The bundle must read on its own after the live store is gone.
    moved = tmp_path / "elsewhere"
    shutil.move(str(bundle_dir), str(moved))
    shutil.rmtree(state)
    assert (moved / rel).read_text(encoding="utf-8") == '{"tokens": [1, 2, 3]}'


def test_missing_artifact_not_fatal(state, workspace):
    gone = str(state / "logs" / "audit-artifacts" / "purged.json")
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": gone})],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["missing_artifacts"] == [gone]
    assert manifest["artifact_count"] == 0
    span = _read_lines(bundle_dir / "spans.jsonl")[0]
    assert span["attributes"]["tool.output.artifact_path"] == gone


def test_artifact_basename_collision_disambiguated(state, workspace):
    a = _make_artifact(state, "output.json", "first")
    b = state / "other" / "output.json"
    b.parent.mkdir(parents=True)
    b.write_text("second", encoding="utf-8")
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(a)}),
            _span("trace-1", name="llm.call", attrs={"llm.output.artifact_path": str(b)}),
        ],
    )

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    spans = _read_lines(bundle_dir / "spans.jsonl")
    rel_a = spans[0]["attributes"]["tool.output.artifact_path"]
    rel_b = spans[1]["attributes"]["llm.output.artifact_path"]
    assert rel_a != rel_b
    assert (bundle_dir / rel_a).read_text(encoding="utf-8") == "first"
    assert (bundle_dir / rel_b).read_text(encoding="utf-8") == "second"


def test_bundle_spans_rotation(state, workspace):
    archive = state / "logs" / "archive"
    _write_log(
        archive / "2026-08-18" / "audit-spans-2026-08-18-010101.log",
        [_span("trace-1", attempt_id="att-x", start="2026-08-18T09:00:00+00:00")],
    )
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-2", attempt_id="att-x", end="2026-08-20T11:00:00+00:00"),
            _span("trace-9"),
        ],
    )

    bundle_dir = tbundle.collect_bundle("att-x", state_dir=state, workspace=workspace)

    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["traceId"] for s in spans] == ["trace-1", "trace-2"]
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["time_range"] == {"start": "2026-08-18T09:00:00+00:00", "end": "2026-08-20T11:00:00+00:00"}


def test_save_auto_pins(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)
    registry = tstore.pins(state)
    assert registry["trace-1"]["reason"] == "bundled"


def test_missing_session_recorded(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:never-saved")])

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is False
    assert not (bundle_dir / "session.jsonl").exists()


def test_unknown_id_raises(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    with pytest.raises(LookupError):
        tbundle.collect_bundle("no-such-id", state_dir=state, workspace=workspace)
    with pytest.raises(ValueError):
        tbundle.collect_bundle("", state_dir=state, workspace=workspace)


def test_trace_id_resolves_to_full_attempt(state, workspace):
    """One turn's trace id must pack the whole multi-turn attempt it belongs to."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-x", session_key="cli:a"),
            _span("trace-2", attempt_id="att-x", session_key="cli:a", name="tool.call"),
        ],
    )
    tverdict.record_verdict("att-x", "fail", source="user", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert bundle_dir.name == "att-x"
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == "att-x"
    assert manifest["span_count"] == 2
    assert manifest["verdict_count"] == 1
    assert [s["traceId"] for s in _read_lines(bundle_dir / "spans.jsonl")] == ["trace-1", "trace-2"]
    registry = tstore.pins(state)
    assert "att-x" in registry and "trace-1" not in registry


def test_unsafe_id_rejected(state, workspace, tmp_path):
    """An attempt id (user-mintable) must not escape the output root as a path."""
    for evil in ("../escaped", str(tmp_path / "abs-escaped"), "a/b"):
        _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", attempt_id=evil)])
        with pytest.raises(ValueError):
            tbundle.collect_bundle(evil, state_dir=state, workspace=workspace)
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "abs-escaped").exists()
    assert not (state / "escaped").exists()


def test_resave_drops_stale_artifacts(state, workspace):
    """A re-pack must not keep artifacts whose source was purged since last pack."""
    artifact = _make_artifact(state, "secret.json", '{"api_key": "sk-live"}')
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(artifact)})],
    )
    first = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)
    assert (first / "artifacts" / "secret.json").exists()

    artifact.unlink()
    second = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    assert not (second / "artifacts" / "secret.json").exists()
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_count"] == 0
    assert manifest["missing_artifacts"] == [str(artifact)]
    leftovers = [p.name for p in second.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_default_workspace_reads_config(state, tmp_path, monkeypatch):
    """Without an explicit workspace, the configured agent workspace is used."""
    import types

    ws = tmp_path / "custom-ws"
    ws.mkdir()
    manager = SessionManager(ws)
    session = manager.get_or_create("cli:chat1")
    session.add_message("user", "hello")
    manager.save(session)
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda config_path=None: types.SimpleNamespace(workspace_path=ws),
    )
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:chat1")])

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is True
    assert (bundle_dir / "session.jsonl").exists()


def test_end_to_end_with_real_tracer(tmp_path, monkeypatch):
    """A trajectory recorded by the live tracer bundles into an offline dir."""
    from raven.tracing import spans as _spans
    from raven.tracing import trace

    state = tmp_path / "traces"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    _spans._store = None
    try:
        # A single turn is addressed by its trace id (attempt id = trace id).
        with trace.span("session.turn", session_key="cli:e2e") as root:
            with trace.span("tool.call") as s:
                s.artifact("tool.output", {"result": "ok", "items": list(range(50))})
        aid = root.trace_id

        bundle_dir = tbundle.collect_bundle(aid, state_dir=state, workspace=workspace)

        spans = _read_lines(bundle_dir / "spans.jsonl")
        tool_span = next(s for s in spans if s["name"] == "tool.call")
        rel = tool_span["attributes"]["tool.output.artifact_path"]
        assert rel.startswith("artifacts/")
        payload = json.loads((bundle_dir / rel).read_text(encoding="utf-8"))
        assert payload["result"] == "ok" and len(payload["items"]) == 50
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["attempt_id"] == aid
        assert manifest["artifact_count"] == 1
        assert tstore.pins(state)[aid]["reason"] == "bundled"
    finally:
        _spans._store = None


def test_bundle_tolerates_non_string_legacy_id(state, workspace):
    """save by trace id still bundles when the span carries a list attempt.id
    (unvalidated history must degrade, not crash the collector)."""
    span = _span("trace-1", session_key="cli:chat1")
    span["attributes"]["attempt.id"] = ["not", "a", "string"]
    _write_log(state / "logs" / "audit-spans.log", [span])

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == "trace-1"
    assert manifest["span_count"] == 1
    assert "trace-1" in tstore.pins(state)


def test_bundle_of_merged_attempt_survives_bad_neighbor_record(state):
    """A list-traceId record elsewhere in the log must not break bundling a
    merged attempt (the member filter is a set lookup)."""
    bad = {"schemaVersion": "audit.span.v1", "traceId": ["x"], "attributes": {}}
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:chat1"), bad, _span("trace-2", session_key="cli:chat1")],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == aid
    assert manifest["span_count"] == 2


def test_bundle_of_merged_attempt_tolerates_malformed_verdict_lines(state):
    """The alias-set verdict filter must skip a list attempt_id line instead
    of failing the whole collection."""
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:chat1"), _span("trace-2", session_key="cli:chat1")],
    )
    tverdict.record_verdict("trace-1", "fail", source="user", state_dir=state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    with (state / "verdicts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"attempt_id": ["bad"], "status": "fail", "source": "user", "ts": "now"}) + "\n")

    bundle_dir = tbundle.collect_bundle(aid, state_dir=state)

    verdicts = _read_lines(bundle_dir / "verdicts.jsonl")
    assert {v["attempt_id"] for v in verdicts} == {"trace-1"}


def test_bundle_of_merged_attempt_collects_all_member_traces(state):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:chat1", start="2026-08-20T10:00:00+00:00"),
            _span("trace-2", session_key="cli:chat1", start="2026-08-20T11:00:00+00:00"),
        ],
    )
    tverdict.record_verdict("trace-1", "fail", source="user", state_dir=state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    tverdict.record_verdict(aid, "pass", source="user", state_dir=state)

    bundle_dir = tbundle.collect_bundle("trace-2", state_dir=state)

    assert bundle_dir == (state / "bundles" / aid).resolve()
    spans = _read_lines(bundle_dir / "spans.jsonl")
    assert [s["traceId"] for s in spans] == ["trace-1", "trace-2"]
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attempt_id"] == aid
    assert manifest["span_count"] == 2
    verdicts = _read_lines(bundle_dir / "verdicts.jsonl")
    assert {v["attempt_id"] for v in verdicts} == {"trace-1", aid}


def test_bundle_of_definition_with_purged_members_raises_lookup_error(state):
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1"), _span("trace-2")],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    (state / "logs" / "audit-spans.log").unlink()

    with pytest.raises(LookupError, match="no spans found for attempt"):
        tbundle.collect_bundle(aid, state_dir=state)


def test_bundle_pin_falls_back_to_member_traces_when_attempt_vanishes(state, monkeypatch):
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1"), _span("trace-2")],
    )
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    def vanished(id_, *, reason="", state_dir=None):
        raise LookupError(f"no spans found for id {id_!r}")

    monkeypatch.setattr(tbundle, "pin_attempt", vanished)
    tbundle.collect_bundle(aid, state_dir=state)

    registry = tstore.pins(state)
    assert registry["trace-1"]["reason"] == "bundled"
    assert registry["trace-2"]["reason"] == "bundled"


def _point_config_at(monkeypatch, path):
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.config.raven.get_config_path", lambda: path)


class TestManifestEnvironment:
    _SECRETS = ("sk-super-secret-123", "tg-secret-456", "secret-host", "Bearer abc", "gm-secret-789")

    def _write_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "providers": {
                        "openrouter": {"apiKey": "sk-super-secret-123"},
                        "gemini": {"apiKeyList": ["gm-secret-789"]},
                        "anthropic": {},
                        "mystery": {},
                    },
                    "channels": {"telegram": {"enabled": True, "token": "tg-secret-456"}},
                    "tools": {
                        "mcpServers": {
                            "search": {"url": "http://secret-host:9/x", "headers": {"Authorization": "Bearer abc"}}
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        _point_config_at(monkeypatch, cfg)
        return cfg

    def _pack_manifest_text(self, state, workspace):
        _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:chat1")])
        bundle_dir = tbundle.collect_bundle("trace-1", state_dir=state, workspace=workspace)
        return (bundle_dir / "manifest.json").read_text(encoding="utf-8")

    def test_environment_block_present(self, state, workspace, tmp_path, monkeypatch):
        cfg = self._write_config(tmp_path, monkeypatch)

        manifest = json.loads(self._pack_manifest_text(state, workspace))

        assert manifest["format_version"] == 1
        assert manifest["raven_version"]
        env = manifest["environment"]
        assert env["raven_version"] == manifest["raven_version"]
        assert env["python_version"] and env["os"] and env["arch"]
        assert {"openrouter", "gemini"} <= set(env["providers"])
        assert "anthropic" not in env["providers"]
        assert "mystery" not in env["providers"]
        assert env["channels"] == ["telegram"]
        assert env["mcp_servers"] == ["search"]
        assert env["tracing_schema"] == "audit.span.v1"
        assert env["bundle_format"] == tbundle.BUNDLE_FORMAT_VERSION
        assert env["config"]["path"] == str(cfg)
        assert env["config"]["exists"] is True
        assert set(env["config"]["top_level_keys"]) == {"providers", "channels", "tools"}
        assert isinstance(env["discovered_plugins"], list)
        assert all(p["id"] and p["version"] and isinstance(p["admitted"], bool) for p in env["discovered_plugins"])

    def test_no_secret_values_reach_the_manifest(self, state, workspace, tmp_path, monkeypatch):
        self._write_config(tmp_path, monkeypatch)

        text = self._pack_manifest_text(state, workspace)

        for secret in self._SECRETS:
            assert secret not in text

    def test_failed_probe_degrades_to_null(self, state, workspace, tmp_path, monkeypatch):
        self._write_config(tmp_path, monkeypatch)

        def boom(*_a, **_k):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(tbundle.platform, "python_version", boom)
        monkeypatch.setattr(tbundle, "_configured_provider_names", boom)

        manifest = json.loads(self._pack_manifest_text(state, workspace))

        env = manifest["environment"]
        assert env["python_version"] is None
        assert env["providers"] is None
        assert env["os"] and env["arch"]

    def test_missing_git_and_config_yield_nulls(self, state, workspace, tmp_path, monkeypatch):
        _point_config_at(monkeypatch, tmp_path / "nope" / "config.json")

        def no_git(*_a, **_k):
            raise FileNotFoundError("git not installed")

        monkeypatch.setattr(tbundle.subprocess, "run", no_git)

        manifest = json.loads(self._pack_manifest_text(state, workspace))

        env = manifest["environment"]
        assert env["git_revision"] is None
        # Not asserted empty: credential_status(include_external=True) may
        # accept ambient material (env vars, OAuth token files) on the host.
        assert isinstance(env["providers"], list)
        assert env["channels"] == []
        assert env["mcp_servers"] == []
        assert env["config"]["exists"] is False
        assert env["config"]["top_level_keys"] is None

    def test_entry_point_plugin_discovered_without_import(self, state, workspace, tmp_path, monkeypatch):
        self._write_config(tmp_path, monkeypatch)
        site = tmp_path / "site"
        pkg = site / "evil_plugin"
        pkg.mkdir(parents=True)
        marker = tmp_path / "imported.marker"
        (pkg / "__init__.py").write_text(
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('imported')\n", encoding="utf-8"
        )
        (pkg / "raven-plugin.toml").write_text(
            '[plugin]\nid = "evil-plugin"\nversion = "9.9"\nenabled_by_default = true\n', encoding="utf-8"
        )
        dist_info = site / "evil_plugin-9.9.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: evil-plugin\nVersion: 9.9\n", encoding="utf-8"
        )
        (dist_info / "entry_points.txt").write_text("[raven.plugins]\nevil = evil_plugin\n", encoding="utf-8")
        (dist_info / "RECORD").write_text(
            "evil_plugin/__init__.py,,\n"
            "evil_plugin/raven-plugin.toml,,\n"
            "evil_plugin-9.9.dist-info/METADATA,,\n"
            "evil_plugin-9.9.dist-info/entry_points.txt,,\n"
            "evil_plugin-9.9.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(site))

        manifest = json.loads(self._pack_manifest_text(state, workspace))

        assert "evil_plugin" not in sys.modules
        assert not marker.exists()
        plugins = {p["id"]: p for p in manifest["environment"]["discovered_plugins"]}
        assert plugins["evil-plugin"] == {"id": "evil-plugin", "version": "9.9", "admitted": True}

    def test_admission_flags_follow_disabled_and_default(self, state, workspace, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"plugins": {"disabled": ["plug-off"]}}), encoding="utf-8")
        _point_config_at(monkeypatch, cfg)
        user_dir = tmp_path / "plugins"
        for pid, default in (("plug-on", "true"), ("plug-off", "true"), ("plug-optin", "false")):
            d = user_dir / pid
            d.mkdir(parents=True)
            (d / "raven-plugin.toml").write_text(
                f'[plugin]\nid = "{pid}"\nversion = "1.0"\nenabled_by_default = {default}\n', encoding="utf-8"
            )
        monkeypatch.setattr(
            "raven.plugin.bootstrap.default_discovery_sources",
            lambda: {
                "bundled_dir": tmp_path / "none",
                "user_dir": user_dir,
                "project_dir": tmp_path / "none",
                "entry_points_group": "raven.plugins.none-such-group",
            },
        )

        manifest = json.loads(self._pack_manifest_text(state, workspace))

        plugins = {p["id"]: p["admitted"] for p in manifest["environment"]["discovered_plugins"]}
        assert plugins == {"plug-on": True, "plug-off": False, "plug-optin": False}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_revision_binds_to_the_raven_checkout_only(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "raven").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "x"],
        cwd=repo,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert tbundle._git_revision(repo / "raven") == head

    nested = repo / "venv" / "site-packages" / "raven"
    nested.mkdir(parents=True)
    assert tbundle._git_revision(nested) is None
