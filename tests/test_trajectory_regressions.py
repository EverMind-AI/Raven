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
from pathlib import Path

import pytest

from raven.trajectory.regression import (
    Check,
    DivergenceExpectation,
    RegressionExpectation,
    check_report,
    load_expectation,
    run_regression_case,
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


def _case_dirs() -> list[Path]:
    if not CASES_ROOT.is_dir():
        return []
    return sorted(p for p in CASES_ROOT.iterdir() if (p / "expect.yaml").is_file())


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=lambda p: p.name)
async def test_trajectory_regression_case(case_dir: Path) -> None:
    report, failures = await run_regression_case(case_dir)
    assert not failures, f"regression case {case_dir.name} failed:\n" + "\n".join(failures)
