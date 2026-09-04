"""Tests for bug report records, packaging, and export sanitization
(`raven.trajectory.bugreport`, `raven.trajectory.sanitize`, `store.member_traces`)."""

from __future__ import annotations

import json
import os
import re
import tarfile

import pytest

from raven.trajectory import bugreport as breport
from raven.trajectory import sanitize as tsan
from raven.trajectory import store as tstore
from raven.trajectory.redact import KnownSecret, RedactionReport, ResidualFinding


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    return state


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch):
    """Keep the real config/env secrets of the test machine out of every run."""
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, *, attempt_id=None, session_key=None, name="session.turn", attrs=None, span_id=None):
    attributes = {"session.key": session_key}
    if attempt_id is not None:
        attributes["attempt.id"] = attempt_id
    if attrs:
        attributes.update(attrs)
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": span_id or f"span-{trace_id}-{name}",
        "name": name,
        "startTime": "2026-08-20T10:00:00+00:00",
        "endTime": "2026-08-20T10:00:01+00:00",
        "attributes": attributes,
    }


_PEM = "-----BEGIN PRIVATE KEY-----\nMIIabcdef\n-----END PRIVATE KEY-----"
_ENTROPY_TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"


def _log_simple(state, attrs=None):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a", attrs=attrs)])


def _prepared(state, workspace, attrs=None, **prepare_kw):
    _log_simple(state, attrs)
    return breport.prepare_trajectory("trace-1", workspace=workspace, state_dir=state, **prepare_kw)


def _full_flow(state, workspace, description="the agent replied wrongly", attrs=None, **fields):
    prep = _prepared(state, workspace, attrs)
    prep = breport.freeze_export(prep, description=description, **fields)
    return breport.confirm_and_package(prep, state_dir=state)


# ── path parser / sanitizer ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "see /tmp/customer-dump.json here",
        "/customer-data",
        "macos /private/var/folders/ab/T/x.log tmp",
        "mount /mnt/company/project/source.py",
        "opt /opt/internal/model/config.yaml",
        "unicode /客户资料/项目 path",
        'quoted "/Project Files/data.json" path',
        "user dir /Users/alice/project/config.json",
        "win C:\\Users\\alice\\f.txt path",
        "unc \\\\srv\\share\\x path",
        "file url file:///etc/passwd leaks",
    ],
    ids=["tmp", "single", "private-var", "mnt", "opt", "unicode", "quoted-space", "users", "drive", "unc", "file-url"],
)
def test_parser_hits_absolute_paths(text):
    sanitized = tsan.sanitize_text(text)
    assert tsan.PATH_PLACEHOLDER in sanitized
    assert not tsan.find_absolute_paths(sanitized)


@pytest.mark.parametrize(
    "text",
    [
        "and/or 1/2 a/b words",
        "https://host/path/to/x stays",
        "see artifacts/001-trace-1-cli-a.json relative",
        "[REDACTED:path]/name.json placeholder tail",
    ],
    ids=["natural", "url", "bundle-relative", "placeholder-tail"],
)
def test_parser_leaves_non_paths_alone(text):
    assert tsan.sanitize_text(text) == text


def test_known_roots_replaced_wherever_they_appear(tmp_path):
    root = str(tmp_path)
    text = f"glued{root}/deep/file.txt end"
    sanitized = tsan.sanitize_text(text, [root])
    assert root not in sanitized
    assert tsan.PATH_PLACEHOLDER in sanitized


def test_sanitize_tree_rewrites_structured_paths(tmp_path):
    tree = tmp_path / "redacted"
    tree.mkdir()
    (tree / "manifest.json").write_text(
        json.dumps({"missing_artifacts": ["/abs/gone/report.png"], "session_key": "cli:a"}), encoding="utf-8"
    )
    span = _span("trace-1", attrs={"tool.artifact_path": "/abs/gone/report.png"})
    (tree / "spans.jsonl").write_text(json.dumps(span) + "\n", encoding="utf-8")

    tsan.sanitize_export_tree(tree)

    manifest = json.loads((tree / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["missing_artifacts"] == ["[REDACTED:path]/report.png"]
    rewritten = json.loads((tree / "spans.jsonl").read_text(encoding="utf-8"))
    assert rewritten["attributes"]["tool.artifact_path"] == "[REDACTED:path]/report.png"
    assert tsan.scan_absolute_paths(tree) == []


def test_tree_digest_change_add_and_symlink(tmp_path):
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("one", encoding="utf-8")
    (tree / "sub" / "b.txt").write_text("two", encoding="utf-8")
    baseline = tsan.tree_digest(tree)
    assert baseline == tsan.tree_digest(tree)

    (tree / "a.txt").write_text("changed", encoding="utf-8")
    assert tsan.tree_digest(tree) != baseline
    (tree / "a.txt").write_text("one", encoding="utf-8")
    assert tsan.tree_digest(tree) == baseline

    (tree / "extra.txt").write_text("x", encoding="utf-8")
    assert tsan.tree_digest(tree) != baseline
    (tree / "extra.txt").unlink()

    (tree / "link").symlink_to(tree / "a.txt")
    with pytest.raises(ValueError):
        tsan.tree_digest(tree)


# ── member trace resolution ────────────────────────────────────────────


def test_member_traces_covers_all_address_shapes(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", attempt_id="att-old", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
            _span("trace-4", session_key="cli:a"),
            _span("trace-5", session_key="cli:a"),
        ],
    )
    aid = tstore.merge_attempts(["trace-4", "trace-5"], state_dir=state)

    assert tstore.member_traces(aid, state) == ("trace-4", "trace-5")
    assert tstore.member_traces("trace-4", state) == ("trace-4", "trace-5")
    assert tstore.member_traces("att-old", state) == ("trace-1", "trace-2")
    assert tstore.member_traces("trace-3", state) == ("trace-3",)
    assert tstore.member_traces("nope", state) is None


# ── classification ─────────────────────────────────────────────────────


def _report(**kw):
    defaults = dict(bundle_dir=None, redacted_dir=None)
    defaults.update(kw)
    return RedactionReport(**defaults)


def test_classify_blocked_on_original_private_key_hit():
    classification, reasons = breport.classify_redaction(_report(patterns={"private-key-block": 1}))
    assert classification == "blocked"
    assert reasons == ["the original trajectory contains a private key block"]
    classification, _ = breport.classify_redaction(_report(), _report(patterns={"private-key-block": 2}))
    assert classification == "blocked"


def test_classify_needs_review_reasons_merge_both_sides():
    finding = ResidualFinding(category="high-entropy", sample="x", file="spans.jsonl")
    classification, reasons = breport.classify_redaction(
        _report(findings=[finding], config_loaded=False, skipped_binaries=["a.bin"]),
        _report(findings=[finding]),
    )
    assert classification == "needs_review"
    assert reasons == [
        "residual scan flagged 2 suspicious token(s)",
        "config could not be fully read — known-value redaction may be incomplete",
        "1 non-UTF-8 file(s) excluded from the copy and not scanned",
    ]


def test_classify_clean_ignores_ordinary_pattern_hits():
    classification, reasons = breport.classify_redaction(_report(patterns={"openai-style-key": 3}))
    assert classification == "clean"
    assert reasons == []


# ── record schema ──────────────────────────────────────────────────────


def test_record_version_and_schema_gate(tmp_path):
    record_dir = tmp_path / "br-20260904-aaaaaa"
    record_dir.mkdir()
    payload = {"schema": breport.RECORD_SCHEMA, "schema_version": 1, "report_id": "br-20260904-aaaaaa"}
    breport.save_record(record_dir, payload)
    assert breport.load_record(record_dir)["report_id"] == "br-20260904-aaaaaa"
    assert not [p for p in record_dir.iterdir() if p.name != breport.RECORD_FILE]

    breport.save_record(record_dir, {**payload, "schema_version": 2})
    with pytest.raises(ValueError):
        breport.load_record(record_dir)
    breport.save_record(record_dir, {**payload, "schema": "something_else"})
    with pytest.raises(ValueError):
        breport.load_record(record_dir)
    (record_dir / breport.RECORD_FILE).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        breport.load_record(record_dir)


def test_report_id_shape_and_uniqueness(tmp_path):
    ids = {breport.new_report_id(tmp_path) for _ in range(32)}
    assert len(ids) == 32
    for report_id in ids:
        assert re.fullmatch(r"br-\d{8}-[0-9a-f]{6}", report_id)


# ── the vertical flow ──────────────────────────────────────────────────


def test_full_flow_lands_local_ready(state, workspace):
    record_dir, record = _full_flow(state, workspace)

    assert record["status"] == "local_ready"
    assert record["schema"] == breport.RECORD_SCHEMA
    assert record["attempt"]["member_traces"] == ["trace-1"]
    assert record["snapshot"]["kept"] is False
    assert not (record_dir / "snapshot").exists()
    assert not (breport.bugreports_root(state) / breport.STAGING_DIR / record["report_id"]).exists()

    package = record_dir / f"{record['report_id']}.tar.gz"
    assert str(package) == record["package"]["path"]
    with tarfile.open(package) as tar:
        names = set(tar.getnames())
        rid = record["report_id"]
        assert f"{rid}/bugreport.json" in names
        assert f"{rid}/trajectory/trace-1.tar.gz" in names
        meta = json.loads(tar.extractfile(f"{rid}/bugreport.json").read().decode("utf-8"))
    assert meta["schema"] == breport.PACKAGE_SCHEMA
    assert meta["redaction"]["classification"] == "clean"
    assert meta["redaction"]["risk_accepted"] is False
    assert "session_key" not in meta["attempt"]
    assert meta["contents"][1]["path"] == "trajectory/trace-1.tar.gz"


def test_problem_token_redacted_but_clean(state, workspace):
    secret = "sk-" + "a1b2c3d4" * 3
    _record_dir, record = _full_flow(state, workspace, description=f"my key {secret} leaked")
    assert record["status"] == "local_ready"
    assert record["redaction"]["classification"] == "clean"
    assert secret not in json.dumps(record)
    assert "[REDACTED:pattern.openai-style-key]" in record["problem"]["description"]


def test_problem_entropy_needs_review_with_problem_file(state, workspace):
    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description=f"weird token {_ENTROPY_TOKEN} appeared")
    assert prep.classification == "needs_review"
    findings = prep.package_metadata["redaction"]["residual_findings"]
    assert any(f["file"] == "problem.description" for f in findings)
    prep.cleanup()


def test_problem_private_key_blocks_before_export(state, workspace):
    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description=f"look: {_PEM}")
    assert prep.classification == "blocked"
    assert not prep.export_dir.exists()
    prep.cleanup()


def test_trajectory_private_key_blocks_before_input(state, workspace):
    prep = _prepared(state, workspace, attrs={"llm.output": _PEM})
    assert prep.classification == "blocked"
    prep.cleanup()


def test_description_paths_are_sanitized(state, workspace):
    _record_dir, record = _full_flow(
        state,
        workspace,
        description="crashed reading /Users/alice/project/config.json",
        steps="run C:\\Users\\alice\\run.bat first",
    )
    text = json.dumps(record["problem"])
    assert "/Users/alice" not in text and "C:\\\\Users" not in text and "C:\\Users" not in text
    assert "[REDACTED:path]" in record["problem"]["description"]
    assert "[REDACTED:path]" in record["problem"]["steps"]


def test_residual_sample_paths_are_sanitized(state, workspace):
    prep = _prepared(state, workspace, attrs={"llm.output": f"token {_ENTROPY_TOKEN} at /tmp/leak/x.json"})
    prep = breport.freeze_export(prep, description="it broke")
    findings = prep.package_metadata["redaction"]["residual_findings"]
    assert findings, "the injected token must flag"
    assert all("/tmp/leak" not in f["sample"] for f in findings)
    prep.cleanup()


def test_missing_artifact_paths_never_reach_the_package(state, workspace, tmp_path):
    gone = tmp_path / "artifacts" / "gone-evidence.png"
    _record_dir, record = _full_flow(state, workspace, attrs={"tool.artifact_path": str(gone)})
    assert record["status"] == "local_ready"
    with tarfile.open(record["package"]["path"]) as tar:
        rid = record["report_id"]
        inner = tar.extractfile(f"{rid}/trajectory/trace-1.tar.gz").read()
    inner_tar = tmp_path / "inner.tar.gz"
    inner_tar.write_bytes(inner)
    extract_dir = tmp_path / "inner"
    with tarfile.open(inner_tar) as tar:
        tar.extractall(extract_dir, filter="data")
    manifest = json.loads((extract_dir / "trace-1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["missing_artifacts"] == ["[REDACTED:path]/gone-evidence.png"]
    assert tsan.scan_absolute_paths(extract_dir) == []


def test_binary_artifact_flags_needs_review(state, workspace, tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\xff\xfe\x00\x01")
    prep = _prepared(state, workspace, attrs={"tool.artifact_path": str(blob)})
    prep = breport.freeze_export(prep, description="binary attached")
    assert prep.classification == "needs_review"
    assert any("non-UTF-8" in reason for reason in prep.reasons)
    prep.cleanup()


def test_config_read_failure_flags_needs_review(state, workspace, monkeypatch):
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], False))
    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description="it broke")
    assert prep.classification == "needs_review"
    assert any(reason.startswith("config could not be fully read") for reason in prep.reasons)
    prep.cleanup()


def test_known_secret_redacted_in_problem_and_trajectory(state, workspace, monkeypatch):
    monkeypatch.setattr(
        breport, "collect_known_secrets", lambda _p: ([KnownSecret("config.api_key", "supersecretvalue")], True)
    )
    _record_dir, record = _full_flow(
        state, workspace, description="failed with supersecretvalue", attrs={"llm.output": "sent supersecretvalue"}
    )
    assert "supersecretvalue" not in json.dumps(record)
    assert record["redaction"]["classification"] == "clean"


# ── freezing, retry, and crash recovery ────────────────────────────────


def _fail_next_tar(monkeypatch):
    real_open = tarfile.open
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(breport.tarfile, "open", flaky)


def _failed_report(state, workspace, monkeypatch, **fields):
    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description="it broke", **fields)
    frozen = (prep.export_dir / "bugreport.json").read_bytes()
    _fail_next_tar(monkeypatch)
    with pytest.raises(breport.PackagingError):
        breport.confirm_and_package(prep, state_dir=state)
    record_dir = breport.bugreports_root(state) / prep.report_id
    return record_dir, frozen


def test_retry_reuses_frozen_bytes_despite_config_change(state, workspace, monkeypatch):
    record_dir, frozen = _failed_report(state, workspace, monkeypatch)
    record = breport.load_record(record_dir)
    assert record["status"] == "failed" and record["failure"]["retryable"] is True
    assert (record_dir / "snapshot").exists() and record["snapshot"]["kept"] is True

    monkeypatch.setattr(
        breport, "collect_known_secrets", lambda _p: ([KnownSecret("config.other", "changedvalue")], False)
    )
    record = breport.retry_packaging(record_dir)

    assert record["status"] == "local_ready"
    assert record["failure"]["retry_count"] == 1
    with tarfile.open(record["package"]["path"]) as tar:
        shipped = tar.extractfile(f"{record['report_id']}/bugreport.json").read()
    assert shipped == frozen


@pytest.mark.parametrize("damage", ["modify", "add", "symlink"], ids=["modified", "added-file", "symlink-injected"])
def test_retry_refuses_corrupted_snapshot(state, workspace, monkeypatch, damage):
    record_dir, _frozen = _failed_report(state, workspace, monkeypatch)
    export_dir = record_dir / "snapshot" / "export"
    if damage == "modify":
        target = export_dir / "bugreport.json"
        target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif damage == "add":
        (export_dir / "extra.txt").write_text("x", encoding="utf-8")
    else:
        (export_dir / "link").symlink_to(export_dir / "bugreport.json")

    with pytest.raises(breport.PackagingError):
        breport.retry_packaging(record_dir)

    record = breport.load_record(record_dir)
    assert record["status"] == "failed"
    assert record["failure"]["retryable"] is False
    assert record["failure"]["reason"] == breport.REASON_SNAPSHOT_CORRUPTED


def test_interrupted_draft_recovers_to_retryable_failed(state, workspace):
    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description="it broke")
    breport.save_record(prep.staging_dir, breport._new_record_payload(prep))
    record_dir = breport.bugreports_root(state) / prep.report_id
    os.replace(prep.staging_dir, record_dir)

    ((found_dir, record),) = breport.list_reports(state)

    assert found_dir == record_dir
    assert record["status"] == "failed"
    assert record["failure"]["reason"] == breport.REASON_INTERRUPTED
    assert record["failure"]["retryable"] is True
    assert record["failure"]["retry_count"] == 0

    record = breport.retry_packaging(record_dir)
    assert record["status"] == "local_ready"


def test_interrupted_draft_without_snapshot_is_terminal(state, workspace):
    import shutil

    prep = _prepared(state, workspace)
    prep = breport.freeze_export(prep, description="it broke")
    breport.save_record(prep.staging_dir, breport._new_record_payload(prep))
    record_dir = breport.bugreports_root(state) / prep.report_id
    os.replace(prep.staging_dir, record_dir)
    shutil.rmtree(record_dir / "snapshot")

    ((_dir, record),) = breport.list_reports(state)

    assert record["status"] == "failed"
    assert record["failure"]["retryable"] is False
    assert record["failure"]["reason"] == breport.REASON_INTERRUPTED_INCOMPLETE
    with pytest.raises(breport.BugReportError):
        breport.retry_packaging(record_dir)


# ── cardinality, association, cleanup ──────────────────────────────────


def test_two_reports_on_one_attempt_coexist(state, workspace):
    _log_simple(state)
    dirs = []
    for description in ("first problem", "second problem"):
        prep = breport.prepare_trajectory("trace-1", workspace=workspace, state_dir=state)
        prep = breport.freeze_export(prep, description=description)
        record_dir, record = breport.confirm_and_package(prep, state_dir=state)
        assert record["status"] == "local_ready"
        dirs.append(record_dir)

    assert dirs[0] != dirs[1]
    matches = breport.reports_for_attempt("trace-1", ("trace-1",), state)
    assert len(matches) == 2
    descriptions = {record["problem"]["description"] for _d, record in matches}
    assert descriptions == {"first problem", "second problem"}


def test_legacy_multi_trace_attempt_freezes_both_members(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", attempt_id="att-old", session_key="cli:a"),
        ],
    )
    prep = breport.prepare_trajectory("att-old", workspace=workspace, state_dir=state)
    prep = breport.freeze_export(prep, description="legacy pair broke")
    _record_dir, record = breport.confirm_and_package(prep, state_dir=state)

    assert record["attempt"]["attempt_id"] == "att-old"
    assert record["attempt"]["member_traces"] == ["trace-1", "trace-2"]
    assert record["attempt"]["merged_definition"] is False
    matches = breport.reports_for_attempt("att-old", ("trace-1", "trace-2"), state)
    assert len(matches) == 1


def test_confirm_rejects_concurrent_member_change(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a"), _span("trace-2", session_key="cli:a")],
    )
    prep = breport.prepare_trajectory("trace-1", workspace=workspace, state_dir=state)
    prep = breport.freeze_export(prep, description="it broke")
    tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    with pytest.raises(breport.StaleAttemptError):
        breport.confirm_and_package(prep, state_dir=state)
    prep.cleanup()
    assert breport.list_reports(state) == []


def test_prepare_rejects_expected_trace_mismatch(state, workspace):
    _log_simple(state)
    with pytest.raises(breport.StaleAttemptError):
        breport.prepare_trajectory("trace-1", expected_traces=("trace-9",), workspace=workspace, state_dir=state)
    assert not list((breport.bugreports_root(state) / breport.STAGING_DIR).iterdir())


def test_stale_staging_cleanup(state, workspace):
    staging_root = breport.bugreports_root(state) / breport.STAGING_DIR
    old = staging_root / "br-20260101-aaaaaa"
    old.mkdir(parents=True)
    os.utime(old, (0, 0))
    fresh = staging_root / "br-20260904-bbbbbb"
    fresh.mkdir()

    removed = breport.cleanup_stale_staging(state)

    assert removed == 1
    assert not old.exists() and fresh.exists()


def test_unreadable_record_is_skipped(state, workspace):
    _record_dir, _record = _full_flow(state, workspace)
    broken = breport.bugreports_root(state) / "br-20260904-ffffff"
    broken.mkdir()
    (broken / breport.RECORD_FILE).write_text("{broken", encoding="utf-8")

    assert len(breport.list_reports(state)) == 1
