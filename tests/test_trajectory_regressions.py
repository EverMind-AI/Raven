"""Trajectory regression cases: the expectation DSL and the case runner.

Covers ``raven/trajectory/regression.py`` (expectation parsing/validation,
report checking with readable failures, the end-to-end case runner) and
auto-discovers every committed case under ``tests/trajectories/`` — one
directory per case: a ``cassette/`` (a minimized, redacted bundle) plus an
``expect.yaml`` declaring where the replay must diverge and what the live
side must do there.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from raven.trajectory.regression import (
    MAX_FILE_BYTES,
    Check,
    DivergenceExpectation,
    RegressionExpectation,
    check_report,
    discover_case_dirs,
    load_case_metadata,
    load_expectation,
    run_regression_case,
    validate_case,
)
from raven.trajectory.replay import Divergence, ReplayReport

pytestmark = pytest.mark.asyncio

CASES_ROOT = Path(__file__).parent / "trajectories"


# ── fixture helpers ────────────────────────────────────────────────────


def _write_cassette(root: Path, *, recorded_user: str | None = None) -> Path:
    """A single-turn cassette: user says "go", the model answers "done".

    With ``recorded_user`` set, the recorded ``llm.input`` carries that text
    as the user message (standing in for a recording made by buggy harness
    code), so a strict replay diverges at llm call #1 on ``messages[1]``.
    """
    cassette = root / "cassette"
    (cassette / "artifacts").mkdir(parents=True)

    def artifact(name: str, payload: dict) -> str:
        rel = f"artifacts/{name}"
        (cassette / rel).write_text(json.dumps(payload), encoding="utf-8")
        return rel

    attrs: dict = {
        "llm.output.artifact_path": artifact(
            "llm-out-0.json", {"content": "done", "finish_reason": "stop", "tool_calls": []}
        )
    }
    if recorded_user is not None:
        attrs["llm.input.artifact_path"] = artifact(
            "llm-in-0.json",
            {
                "model": "stub",
                "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": recorded_user}],
                "tools": [],
            },
        )
    spans = [
        {
            "traceId": "trace-x",
            "spanId": "turn-0",
            "name": "session.turn",
            "attributes": {
                "session.key": "cli:regression-test",
                "turn.input.artifact_path": artifact(
                    "turn-in-0.json", {"content": "go", "channel": "cli", "chat_id": "d"}
                ),
            },
        },
        {"traceId": "trace-x", "spanId": "llm-0", "name": "llm.call", "attributes": attrs},
    ]
    (cassette / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (cassette / "manifest.json").write_text(
        json.dumps({"format_version": 1, "attempt_id": "trace-x", "session_key": "cli:regression-test"}),
        encoding="utf-8",
    )
    return cassette


def _write_expect(root: Path, text: str) -> Path:
    path = root / "expect.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _report(*, divergences=(), llm_requests=(), tool_requests=(), halted=False) -> ReplayReport:
    return ReplayReport(
        bundle_dir=Path("."),
        mode="strict",
        turns_replayed=1,
        turns_recorded=1,
        llm_calls_replayed=len(llm_requests),
        llm_calls_recorded=len(llm_requests),
        llm_calls_streamed=0,
        tool_calls_replayed=len(tool_requests),
        tool_calls_recorded=len(tool_requests),
        divergences=list(divergences),
        halted=halted,
        replies=["done"],
        llm_requests=list(llm_requests),
        tool_requests=list(tool_requests),
    )


def _divergence(kind="llm", index=0, field="messages[1]") -> Divergence:
    return Divergence(kind=kind, index=index, fatal=True, field=field, detail="expected 'a', got 'b'")


# ── load_expectation ───────────────────────────────────────────────────


async def test_load_expectation_parses_the_full_shape(tmp_path) -> None:
    path = _write_expect(
        tmp_path,
        """
# a comment survives YAML
mode: warn
divergence:
  kind: llm
  index: 2
  field: messages[3]
checks:
  - call: llm
    index: 2
    message: -1
    op: contains
    value: "fixed text"
  - call: tool
    index: 0
    op: params_equal
    value: {path: a.txt}
""",
    )
    exp = load_expectation(path)
    assert exp.mode == "warn"
    assert exp.divergence == DivergenceExpectation(kind="llm", index=2, field="messages[3]")
    assert exp.checks == (
        Check(call="llm", index=2, op="contains", value="fixed text", message=-1),
        Check(call="tool", index=0, op="params_equal", value={"path": "a.txt"}),
    )


async def test_load_expectation_defaults(tmp_path) -> None:
    exp = load_expectation(_write_expect(tmp_path, "divergence: null\n"))
    assert exp.mode == "strict"
    assert exp.divergence is None
    assert exp.checks == ()
    # message defaults to -1 (the newest message of the request)
    exp = load_expectation(_write_expect(tmp_path, "checks:\n  - {call: llm, index: 0, op: contains, value: x}\n"))
    assert exp.checks[0].message == -1


async def test_load_expectation_rejects_malformed_files(tmp_path) -> None:
    cases = [
        ("divergenc: {kind: llm, index: 0}\n", "unknown key"),
        ("mode: loose\n", "mode must be one of"),
        ("divergence: {kind: http, index: 0}\n", "divergence.kind"),
        ("divergence: {kind: llm, index: -1}\n", "divergence.index"),
        ("divergence: {kind: llm, index: 0, fields: x}\n", "unknown divergence key"),
        ("checks:\n  - {call: llm, index: 0, op: params_equal, value: {}}\n", "llm op must be one of"),
        ("checks:\n  - {call: tool, index: 0, op: contains, value: x}\n", "tool op must be one of"),
        ("checks:\n  - {call: tool, index: 0, op: name_equals, value: x, message: 1}\n", "llm checks only"),
        ("checks:\n  - {call: llm, index: 0, op: contains}\n", "value is required"),
        ("checks:\n  - {call: llm, index: 0, op: contains, value: {a: 1}}\n", "must be a string"),
    ]
    for text, expected_error in cases:
        with pytest.raises(ValueError, match=expected_error):
            load_expectation(_write_expect(tmp_path, text))


# ── check_report ───────────────────────────────────────────────────────


async def test_check_report_no_divergence_expectation() -> None:
    expectation = RegressionExpectation(divergence=None)
    assert check_report(_report(), expectation) == []

    failures = check_report(_report(divergences=[_divergence()], halted=True), expectation)
    assert len(failures) == 1
    assert "expected no divergence" in failures[0] and "messages[1]" in failures[0]


async def test_check_report_divergence_direction() -> None:
    expectation = RegressionExpectation(divergence=DivergenceExpectation(kind="llm", index=0, field="messages[1]"))
    report = _report(divergences=[_divergence()], halted=True)
    assert check_report(report, expectation) == []

    complete = check_report(_report(), expectation)
    assert "completed with no divergence" in complete[0]

    wrong_place = check_report(_report(divergences=[_divergence(kind="tool", index=2)], halted=True), expectation)
    assert "expected the first divergence at llm call #1" in wrong_place[0]
    assert "tool call #3" in wrong_place[0]

    wrong_field = check_report(_report(divergences=[_divergence(field="tools")], halted=True), expectation)
    assert "expected the divergence on field 'messages[1]'" in wrong_field[0]


async def test_check_report_llm_and_tool_checks() -> None:
    report = _report(
        llm_requests=[
            {
                "model": "stub",
                "stream": False,
                "messages": [{"role": "user", "content": "fix applied: go"}],
                "tools": [],
            }
        ],
        tool_requests=[{"name": "exec", "params": {"command": "ls"}}],
    )
    good = RegressionExpectation(
        checks=(
            Check(call="llm", index=0, op="contains", value="fix applied"),
            Check(call="llm", index=0, op="not_contains", value="stale header"),
            Check(call="llm", index=0, op="equals", value="fix applied: go"),
            Check(call="tool", index=0, op="name_equals", value="exec"),
            Check(call="tool", index=0, op="params_equal", value={"command": "ls"}),
        )
    )
    assert check_report(report, good) == []

    bad = RegressionExpectation(
        checks=(
            Check(call="llm", index=0, op="contains", value="missing text"),
            Check(call="llm", index=0, op="not_contains", value="go"),
            Check(call="llm", index=1, op="contains", value="x"),
            Check(call="llm", index=0, op="contains", value="x", message=5),
            Check(call="tool", index=0, op="params_equal", value={"command": "rm"}),
            Check(call="tool", index=3, op="name_equals", value="exec"),
        )
    )
    failures = check_report(report, bad)
    assert len(failures) == 6
    assert "does not contain 'missing text'" in failures[0] and "fix applied: go" in failures[0]
    assert "must not contain 'go'" in failures[1]
    assert "only 1 llm call(s)" in failures[2]
    assert "out of range" in failures[3]
    assert "params differ" in failures[4] and "rm" in failures[4] and "ls" in failures[4]
    assert "only 1 tool call(s)" in failures[5]


async def test_check_report_failures_accumulate() -> None:
    expectation = RegressionExpectation(
        divergence=DivergenceExpectation(kind="llm", index=0),
        checks=(Check(call="llm", index=0, op="contains", value="x"),),
    )
    failures = check_report(_report(), expectation)
    assert len(failures) == 2, "the divergence failure must not mask check failures"


# ── run_regression_case end to end ─────────────────────────────────────


async def test_run_regression_case_passes_on_a_faithful_replay(tmp_path) -> None:
    _write_cassette(tmp_path)
    _write_expect(
        tmp_path,
        """
divergence: null
checks:
  - {call: llm, index: 0, message: -1, op: contains, value: go}
""",
    )
    report, failures = await run_regression_case(tmp_path)
    assert failures == []
    assert report.complete and report.replies == ["done"]


async def test_run_regression_case_asserts_the_divergence_direction(tmp_path) -> None:
    """The regression shape for a fixed bug: the recording (made by the buggy
    harness) diverges at a known call, and the live side must show the fix."""
    _write_cassette(tmp_path, recorded_user="go [buggy duplicated header]")
    _write_expect(
        tmp_path,
        """
mode: strict
divergence: {kind: llm, index: 0, field: "messages[1]"}
checks:
  - {call: llm, index: 0, message: -1, op: not_contains, value: "[buggy duplicated header]"}
  - {call: llm, index: 0, message: -1, op: contains, value: go}
""",
    )
    report, failures = await run_regression_case(tmp_path)
    assert failures == []
    assert report.halted, "strict mode halts at the asserted divergence"


async def test_run_regression_case_reports_readable_failures(tmp_path) -> None:
    _write_cassette(tmp_path)
    _write_expect(
        tmp_path,
        """
divergence: {kind: llm, index: 0}
checks:
  - {call: llm, index: 0, message: -1, op: contains, value: "text that is absent"}
""",
    )
    _, failures = await run_regression_case(tmp_path)
    assert len(failures) == 2
    assert "expected the first divergence at llm call #1" in failures[0]
    assert "does not contain 'text that is absent'" in failures[1]


# ── committed cases under tests/trajectories/ ──────────────────────────

# Raised by hand as real cases land; never derived from the directory, so
# deleting a case is a deliberate, reviewable act.
MIN_COMMITTED_CASES = 2

REPORT_DIR_ENV = "RAVEN_REGRESSION_REPORT_DIR"


def _case_dirs() -> list[Path]:
    if not CASES_ROOT.is_dir():
        return []
    return sorted(p for p in CASES_ROOT.iterdir() if (p / "expect.yaml").is_file())


def _guard_problems(root: Path, minimum: int) -> list[str]:
    """Why the committed-case set cannot vouch for anything (empty = fine).

    Counts by the same expect.yaml discovery the parametrization uses: an
    empty parameter list would make the whole suite pass vacuously. A broken
    directory missing its expect.yaml is the validate --all gate's job."""
    if not root.is_dir():
        return [f"cases root {root} is missing"]
    count = sum(1 for p in root.iterdir() if (p / "expect.yaml").is_file())
    if count < minimum:
        return [f"only {count} committed case(s) under {root}, expected at least {minimum}"]
    return []


def _dump_failure_report(case_dir: Path, report, failures: list[str]) -> None:
    """Serialize a failed case for the CI artifact, best-effort: the report
    reuses the replay --json schema (mode comes from the case's expect.yaml
    by construction) plus the check failures; any serialization problem must
    never mask the test failure itself."""
    out_dir = os.environ.get(REPORT_DIR_ENV)
    if not out_dir or not failures:
        return
    try:
        manifest = json.loads((case_dir / "cassette" / "manifest.json").read_text(encoding="utf-8"))
        summary = {
            key: manifest[key]
            for key in ("attempt_id", "format_version")
            if isinstance(manifest, dict) and isinstance(manifest.get(key), (str, int))
        } or None
    except (OSError, ValueError):
        summary = None
    payload = {**report.to_dict(manifest=summary), "check_failures": failures}
    try:
        path = Path(out_dir) / f"{case_dir.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


async def test_committed_case_guard() -> None:
    assert _guard_problems(CASES_ROOT, MIN_COMMITTED_CASES) == []


async def test_guard_flags_missing_and_empty_roots(tmp_path) -> None:
    assert _guard_problems(tmp_path / "nope", 1) == [f"cases root {tmp_path / 'nope'} is missing"]
    problems = _guard_problems(tmp_path, 1)
    assert problems and "only 0 committed case(s)" in problems[0]


async def test_dump_failure_report_writes_the_artifact_schema(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(REPORT_DIR_ENV, str(tmp_path / "reports"))
    case = _case_copy(tmp_path)
    _dump_failure_report(case, _report(halted=True), ["checks[0]: failed"])
    payload = json.loads((tmp_path / "reports" / "case_copy.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["check_failures"] == ["checks[0]: failed"]
    assert payload["manifest"] == {"attempt_id": "trace-1a02235c198-17f742c1", "format_version": 1}
    assert payload["halted"] is True


async def test_dump_failure_report_is_inert_without_the_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(REPORT_DIR_ENV, raising=False)
    _dump_failure_report(tmp_path, _report(), ["boom"])
    assert list(tmp_path.iterdir()) == []


async def test_dump_failure_report_swallows_write_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(REPORT_DIR_ENV, str(tmp_path / "reports"))
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    _dump_failure_report(_case_copy(tmp_path), _report(), ["boom"])


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
async def test_trajectory_regression_case(case_dir: Path) -> None:
    report, failures = await run_regression_case(case_dir)
    if failures:
        _dump_failure_report(case_dir, report, failures)
    assert not failures, f"regression case {case_dir.name} failed:\n" + "\n".join(failures)


# ── load_case_metadata ─────────────────────────────────────────────────


_ABSENT = object()


def _write_case_yaml(root: Path, **overrides) -> Path:
    data = {"issue": "#362", "owner": "forrest", "why": "guards the fix", "re_record": "never"}
    data.update(overrides)
    path = root / "case.yaml"
    path.write_text(yaml.safe_dump({k: v for k, v in data.items() if v is not _ABSENT}), encoding="utf-8")
    return path


async def test_load_case_metadata_parses_the_full_shape(tmp_path) -> None:
    path = _write_case_yaml(tmp_path, risk="low", created_from="att-1")
    meta = load_case_metadata(path)
    assert meta.issue == "#362"
    assert meta.owner == "forrest"
    assert meta.why == "guards the fix"
    assert meta.re_record == "never"
    assert meta.risk == "low"
    assert meta.created_from == "att-1"


async def test_load_case_metadata_optionals_default_to_none(tmp_path) -> None:
    meta = load_case_metadata(_write_case_yaml(tmp_path))
    assert meta.risk is None and meta.created_from is None


@pytest.mark.parametrize("key", ["issue", "owner", "why", "re_record"])
@pytest.mark.parametrize("value", [_ABSENT, None, "", "   "], ids=["absent", "null", "empty", "blank"])
async def test_load_case_metadata_rejects_missing_or_blank_required(tmp_path, key, value) -> None:
    path = _write_case_yaml(tmp_path, **{key: value})
    with pytest.raises(ValueError, match=f"{key} is required"):
        load_case_metadata(path)


async def test_load_case_metadata_rejects_unknown_keys(tmp_path) -> None:
    path = _write_case_yaml(tmp_path, ticket="EVE-1")
    with pytest.raises(ValueError, match="unknown key"):
        load_case_metadata(path)


async def test_load_case_metadata_rejects_bad_risk(tmp_path) -> None:
    path = _write_case_yaml(tmp_path, risk="serious")
    with pytest.raises(ValueError, match="risk must be one of"):
        load_case_metadata(path)


async def test_load_case_metadata_rejects_non_mapping(tmp_path) -> None:
    path = tmp_path / "case.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_case_metadata(path)


async def test_load_case_metadata_turns_yaml_syntax_errors_into_valueerror(tmp_path) -> None:
    path = tmp_path / "case.yaml"
    path.write_text("issue: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be parsed as YAML"):
        load_case_metadata(path)


async def test_load_case_metadata_parses_reviewed_residuals(tmp_path) -> None:
    digest = "a" * 64
    path = _write_case_yaml(tmp_path, reviewed_residuals=[{"sha256": digest.upper(), "note": "benign prose"}])
    meta = load_case_metadata(path)
    assert meta.reviewed_residuals[0].sha256 == digest
    assert meta.reviewed_residuals[0].note == "benign prose"


@pytest.mark.parametrize(
    ("entry", "expected_error"),
    [
        ("not-a-mapping", "must be a mapping"),
        ({"sha256": "abc", "note": "n"}, "64-hex"),
        ({"sha256": "z" * 64, "note": "n"}, "64-hex"),
        ({"sha256": "a" * 64}, "note is required"),
        ({"sha256": "a" * 64, "note": "n", "extra": 1}, "unknown key"),
    ],
    ids=["non-mapping", "short-digest", "non-hex", "missing-note", "unknown-key"],
)
async def test_load_case_metadata_rejects_bad_reviewed_residuals(tmp_path, entry, expected_error) -> None:
    path = _write_case_yaml(tmp_path, reviewed_residuals=[entry])
    with pytest.raises(ValueError, match=expected_error):
        load_case_metadata(path)


# ── discover_case_dirs ─────────────────────────────────────────────────


async def test_discover_case_dirs_lists_every_subdirectory(tmp_path) -> None:
    (tmp_path / "good_case").mkdir()
    (tmp_path / "broken_case").mkdir()
    (tmp_path / "README.md").write_text("not a case", encoding="utf-8")
    assert [p.name for p in discover_case_dirs(tmp_path)] == ["broken_case", "good_case"]


async def test_discover_case_dirs_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="is not a directory"):
        discover_case_dirs(tmp_path / "nope")


async def test_discover_case_dirs_skips_only_init_staging_directories(tmp_path) -> None:
    """Only this feature's own .init- publish-staging pattern is skipped;
    any other dot directory is still discovered by the gate."""
    (tmp_path / ".init-some_case-abc123").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "real_case").mkdir()
    assert [p.name for p in discover_case_dirs(tmp_path)] == [".hidden", "real_case"]


# ── validate_case ──────────────────────────────────────────────────────


def _case_copy(tmp_path: Path) -> Path:
    """A validate baseline: a copy of a committed, replayable sample case."""
    case = tmp_path / "case_copy"
    shutil.copytree(CASES_ROOT / "sample_reproduces_recording", case)
    return case


def _spans(case: Path) -> list[dict]:
    return [json.loads(x) for x in (case / "cassette" / "spans.jsonl").read_text(encoding="utf-8").splitlines()]


def _write_spans(case: Path, spans: list[dict]) -> None:
    (case / "cassette" / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _artifact_ref(case: Path, key_suffix: str) -> tuple[dict, str]:
    for span in _spans(case):
        for key, ref in (span.get("attributes") or {}).items():
            if key.endswith(key_suffix):
                return span, ref
    raise AssertionError(f"no span references {key_suffix}")


async def test_validate_case_passes_on_a_sample_copy(tmp_path) -> None:
    assert validate_case(_case_copy(tmp_path)) == []


async def test_validate_committed_cases_pass() -> None:
    cases = discover_case_dirs(CASES_ROOT)
    assert cases
    for case in cases:
        assert validate_case(case) == [], case.name


async def test_validate_case_rejects_missing_files_without_stopping(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "case.yaml").unlink()
    (case / "cassette" / "redaction.json").unlink()
    problems = validate_case(case)
    assert any("case.yaml is missing" in p for p in problems)
    assert any("redaction.json is missing" in p for p in problems)


async def test_validate_case_rejects_blank_required_metadata(tmp_path) -> None:
    case = _case_copy(tmp_path)
    _write_case_yaml(case, owner="")
    problems = validate_case(case)
    assert any("owner is required" in p for p in problems)


async def test_validate_case_rejects_manifest_without_minimized_block(tmp_path) -> None:
    case = _case_copy(tmp_path)
    manifest_path = case / "cassette" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["minimized"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = validate_case(case)
    assert any("no minimized block" in p for p in problems)


async def test_validate_case_rejects_unparseable_manifest(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "cassette" / "manifest.json").write_text("{broken", encoding="utf-8")
    problems = validate_case(case)
    assert any("manifest.json cannot be parsed" in p for p in problems)


async def test_validate_case_rejects_null_manifest(tmp_path) -> None:
    """JSON null parses fine and must not slip past the object checks."""
    case = _case_copy(tmp_path)
    (case / "cassette" / "manifest.json").write_text("null", encoding="utf-8")
    problems = validate_case(case)
    assert any("must hold a JSON object" in p for p in problems)


async def test_validate_case_collects_malformed_case_yaml_instead_of_raising(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "case.yaml").write_text("issue: [\n", encoding="utf-8")
    problems = validate_case(case)
    assert any("cannot be parsed as YAML" in p for p in problems)


async def test_validate_case_collects_non_mapping_span_attributes_instead_of_raising(tmp_path) -> None:
    case = _case_copy(tmp_path)
    spans = _spans(case)
    spans[0]["attributes"] = ["not", "a", "mapping"]
    _write_spans(case, spans)
    problems = validate_case(case)
    assert any("attributes must be a mapping" in p for p in problems)


async def test_validate_case_collects_non_object_artifact_payload_instead_of_raising(tmp_path) -> None:
    """[1] is valid JSON but not a payload the loaders can index into."""
    case = _case_copy(tmp_path)
    _, ref = _artifact_ref(case, "tool.output.artifact_path")
    (case / "cassette" / ref).write_text("[1]", encoding="utf-8")
    problems = validate_case(case)
    assert any("must hold a JSON object" in p for p in problems)


async def test_validate_case_collects_non_string_span_id_instead_of_raising(tmp_path) -> None:
    """A list spanId would make load_recording's dedup key unhashable."""
    case = _case_copy(tmp_path)
    spans = _spans(case)
    spans[0]["spanId"] = ["bad"]
    _write_spans(case, spans)
    problems = validate_case(case)
    assert any("spanId must be a string" in p for p in problems)


async def test_validate_case_collects_non_string_case_yaml_keys_instead_of_raising(tmp_path) -> None:
    """Mixed int/str keys must not crash the unknown-key sort."""
    case = _case_copy(tmp_path)
    (case / "case.yaml").write_text("42: foo\nbadkey: bar\n", encoding="utf-8")
    problems = validate_case(case)
    assert any("keys must be strings" in p for p in problems)


async def test_load_expectation_rejects_non_string_keys_at_every_level(tmp_path) -> None:
    top = _write_expect(tmp_path, "42: foo\nmode: strict\n")
    with pytest.raises(ValueError, match="keys must be strings"):
        load_expectation(top)
    nested = _write_expect(tmp_path, "divergence: {42: llm, index: 0}\n")
    with pytest.raises(ValueError, match="keys must be strings"):
        load_expectation(nested)


async def test_validate_case_rejects_missing_referenced_artifact(tmp_path) -> None:
    case = _case_copy(tmp_path)
    _, ref = _artifact_ref(case, "llm.output.artifact_path")
    (case / "cassette" / ref).unlink()
    problems = validate_case(case)
    assert any("does not exist" in p for p in problems)


async def test_validate_case_rejects_corrupt_referenced_artifact(tmp_path) -> None:
    case = _case_copy(tmp_path)
    _, ref = _artifact_ref(case, "llm.output.artifact_path")
    (case / "cassette" / ref).write_text("not json{{", encoding="utf-8")
    problems = validate_case(case)
    assert any("is not valid JSON" in p for p in problems)


async def test_validate_case_rejects_escaping_artifact_reference(tmp_path) -> None:
    case = _case_copy(tmp_path)
    spans = _spans(case)
    spans.append({"spanId": "evil", "attributes": {"llm.output.artifact_path": "../../evil.json"}})
    _write_spans(case, spans)
    problems = validate_case(case)
    assert any("escapes the cassette" in p for p in problems)


async def test_validate_case_rejects_semantically_empty_turn_input(tmp_path) -> None:
    """Valid JSON is not a usable payload: {} yields no replayable turn."""
    case = _case_copy(tmp_path)
    _, ref = _artifact_ref(case, "turn.input.artifact_path")
    (case / "cassette" / ref).write_text("{}", encoding="utf-8")
    problems = validate_case(case)
    assert any("no recorded turn inputs" in p for p in problems)


async def test_validate_case_rejects_null_tool_result(tmp_path) -> None:
    case = _case_copy(tmp_path)
    _, ref = _artifact_ref(case, "tool.output.artifact_path")
    (case / "cassette" / ref).write_text(json.dumps({"result": None}), encoding="utf-8")
    problems = validate_case(case)
    assert any("no usable tool.output payload" in p for p in problems)


async def test_validate_case_rejects_llm_call_without_input_reference(tmp_path) -> None:
    case = _case_copy(tmp_path)
    spans = _spans(case)
    for span in spans:
        (span.get("attributes") or {}).pop("llm.input.artifact_path", None)
    _write_spans(case, spans)
    problems = validate_case(case)
    assert any("has no llm.input payload" in p for p in problems)


async def test_validate_case_rejects_emptied_spans(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "cassette" / "spans.jsonl").write_text("", encoding="utf-8")
    problems = validate_case(case)
    assert any("no recorded turn inputs" in p for p in problems)


async def test_validate_case_rejects_unscannable_file(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "blob.bin").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]))
    problems = validate_case(case)
    assert any("not readable as UTF-8" in p for p in problems)


async def test_validate_case_rejects_unreviewed_residual_findings(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "notes.txt").write_text("token a3f9c2b7d8e64a1b9c0d2e5f7a8b3c4d5e6f7a8b9c0d1e2f here", encoding="utf-8")
    problems = validate_case(case)
    assert any("residual scan flagged" in p and "notes.txt" in p for p in problems)


async def test_validate_case_rejects_a_new_token_sharing_a_reviewed_prefix_and_suffix(tmp_path) -> None:
    """The review entry hashes the full token, so a different token that keeps
    the first/last four characters of a reviewed one must still fail."""
    case = _case_copy(tmp_path)
    reviewed_token = "most-recently-modified"
    collided_token = "mostAb9Cx7De6Fg5Hi4Jk3Lm2Nofied"
    assert (reviewed_token[:4], reviewed_token[-4:]) == (collided_token[:4], collided_token[-4:])
    replaced = 0
    for path in sorted(p for p in (case / "cassette").rglob("*") if p.is_file()):
        text = path.read_text(encoding="utf-8")
        if reviewed_token in text:
            path.write_text(text.replace(reviewed_token, collided_token), encoding="utf-8")
            replaced += 1
    assert replaced
    problems = validate_case(case)
    assert any("residual scan flagged" in p for p in problems)


async def test_validate_case_exempts_listed_digest_literals_but_not_other_hex(tmp_path) -> None:
    """The 64-hex digests in reviewed_residuals are themselves high-entropy
    tokens to the scanner; a listed digest literal passes, unlisted hex of the
    same shape still fails."""
    case = _case_copy(tmp_path)
    meta = load_case_metadata(case / "case.yaml")
    listed = meta.reviewed_residuals[0].sha256
    unlisted = "b7e4a19c3f5d28061e9c4a7b5d3f18092c6e4a1b8d7f3052a9c1e6b4d8f27a30"
    assert unlisted not in {entry.sha256 for entry in meta.reviewed_residuals}
    (case / "notes.txt").write_text(f"listed {listed} unlisted {unlisted}\n", encoding="utf-8")
    problems = validate_case(case)
    residual = [p for p in problems if "residual scan flagged" in p]
    assert len(residual) == 1 and "notes.txt" in residual[0]


async def test_validate_case_rejects_findings_when_case_yaml_is_unusable(tmp_path) -> None:
    """No case.yaml means no review entries: every finding fails."""
    case = _case_copy(tmp_path)
    (case / "case.yaml").unlink()
    problems = validate_case(case)
    assert any("case.yaml is missing" in p for p in problems)
    assert any("residual scan flagged" in p for p in problems)


async def test_validate_case_rejects_oversized_file(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "big.txt").write_text("a" * (MAX_FILE_BYTES + 1), encoding="utf-8")
    problems = validate_case(case)
    assert any("per-file budget" in p for p in problems)


async def test_validate_case_rejects_oversized_total(tmp_path) -> None:
    case = _case_copy(tmp_path)
    for i in range(5):
        (case / f"pad-{i}.txt").write_text("a" * (MAX_FILE_BYTES - 1024), encoding="utf-8")
    problems = validate_case(case)
    assert any("over the" in p and "budget" in p and "totals" in p for p in problems)


async def test_validate_case_rejects_non_directory(tmp_path) -> None:
    assert validate_case(tmp_path / "nope") == [f"{tmp_path / 'nope'} is not a directory"]


# ── replayability_problems (extracted minimize gate) ───────────────────


async def test_replayability_problems_reports_every_gap() -> None:
    from raven.trajectory.cassette import replayability_problems
    from raven.trajectory.replay import RecordedLLMCall, RecordedToolCall, Recording

    recording = Recording(
        bundle_dir=Path("."),
        manifest={},
        llm_calls=[RecordedLLMCall(input=None, output=None)],
        tool_calls=[RecordedToolCall(name=None, params=None, result=None)],
        turns=[],
    )
    problems = replayability_problems(recording)
    assert "no recorded turn inputs" in problems
    assert "llm call #1 has no llm.input payload" in problems
    assert "llm call #1 has no llm.output payload" in problems
    assert "tool call #1 has no tool.input payload" in problems
    assert "tool call #1 has no usable tool.output payload" in problems
