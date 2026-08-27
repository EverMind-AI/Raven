"""Trajectory regression cases — replay a cassette, assert the divergence direction.

A Trajectory Regression Case turns a fixed harness bug into a permanent CI
guard: one directory holding a Trajectory Cassette (``cassette/``) and an
expectation file (``expect.yaml``). Running the case replays the cassette
through the live harness and asserts *where* the replay departs from the
recording and *what the live side does* there. After a bug fix the harness
necessarily diverges from a recording made by the buggy code, so the natural
assertion shape is "the first divergence is at the expected call and the live
value is the fixed behavior" — not "zero divergence" (though a case may assert
that too, to guard faithful reproduction).

Expectation file (YAML)::

    mode: strict              # optional: strict (default) | warn
    divergence:               # omit (or null) to expect zero divergence
      kind: llm               # llm | tool — the replay's FIRST divergence
      index: 0                # 0-based call index within its kind
      field: messages[1]      # optional: the diverging field name
    checks:                   # optional assertions on the live side
      - call: llm
        index: 0              # 0-based llm call index
        message: -1           # message index in that call's live request
                              # (optional, default -1: the newest message)
        op: contains          # contains | not_contains | equals
        value: "fixed text"
      - call: tool
        index: 0              # 0-based tool call index
        op: params_equal      # params_equal | name_equals
        value: {path: a.txt}

Checks read the live requests the replay captured
(:attr:`raven.trajectory.replay.ReplayReport.llm_requests` /
``tool_requests``), so they can assert actual values at and before the
divergence point even when strict mode halted there. Failures are returned as
human-readable strings, one per unmet expectation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from raven.trajectory.replay import REPLAY_MODES, ReplayReport, run_replay

EXPECTATION_FILE = "expect.yaml"
CASSETTE_DIR = "cassette"

_TOP_KEYS = {"mode", "divergence", "checks"}
_DIVERGENCE_KEYS = {"kind", "index", "field"}
_CHECK_KEYS = {"call", "index", "op", "value", "message"}
_KINDS = ("llm", "tool")
_LLM_OPS = ("contains", "not_contains", "equals")
_TOOL_OPS = ("params_equal", "name_equals")


@dataclass(frozen=True)
class DivergenceExpectation:
    """Where the replay's first divergence must land."""

    kind: str
    index: int
    field: str | None = None

    def render(self) -> str:
        suffix = f" on field {self.field!r}" if self.field else ""
        return f"{self.kind} call #{self.index + 1}{suffix}"


@dataclass(frozen=True)
class Check:
    """One assertion against a captured live request."""

    call: str
    index: int
    op: str
    value: Any
    message: int = -1


@dataclass(frozen=True)
class RegressionExpectation:
    """The parsed ``expect.yaml`` of one regression case."""

    mode: str = "strict"
    divergence: DivergenceExpectation | None = None
    checks: tuple[Check, ...] = ()


def _require(condition: bool, where: str, problem: str) -> None:
    if not condition:
        raise ValueError(f"{where}: {problem}")


def _parse_divergence(where: str, data: Any) -> DivergenceExpectation:
    _require(isinstance(data, dict), where, f"divergence must be a mapping, got {type(data).__name__}")
    unknown = set(data) - _DIVERGENCE_KEYS
    _require(not unknown, where, f"unknown divergence key(s) {sorted(unknown)}; allowed: {sorted(_DIVERGENCE_KEYS)}")
    kind = data.get("kind")
    _require(kind in _KINDS, where, f"divergence.kind must be one of {_KINDS}, got {kind!r}")
    index = data.get("index")
    _require(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0,
        where,
        f"divergence.index must be a 0-based integer, got {index!r}",
    )
    field = data.get("field")
    _require(field is None or isinstance(field, str), where, f"divergence.field must be a string, got {field!r}")
    return DivergenceExpectation(kind=kind, index=index, field=field)


def _parse_check(where: str, pos: int, data: Any) -> Check:
    where = f"{where}: checks[{pos}]"
    _require(isinstance(data, dict), where, f"must be a mapping, got {type(data).__name__}")
    unknown = set(data) - _CHECK_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_CHECK_KEYS)}")
    call = data.get("call")
    _require(call in _KINDS, where, f"call must be one of {_KINDS}, got {call!r}")
    index = data.get("index")
    _require(
        isinstance(index, int) and not isinstance(index, bool) and index >= 0,
        where,
        f"index must be a 0-based integer, got {index!r}",
    )
    op = data.get("op")
    _require("value" in data, where, "value is required")
    if call == "llm":
        _require(op in _LLM_OPS, where, f"llm op must be one of {_LLM_OPS}, got {op!r}")
        message = data.get("message", -1)
        _require(
            isinstance(message, int) and not isinstance(message, bool),
            where,
            f"message must be an integer index, got {message!r}",
        )
        if op in ("contains", "not_contains"):
            _require(isinstance(data["value"], str), where, f"{op} value must be a string")
        return Check(call=call, index=index, op=op, value=data["value"], message=message)
    _require(op in _TOOL_OPS, where, f"tool op must be one of {_TOOL_OPS}, got {op!r}")
    _require("message" not in data, where, "message applies to llm checks only")
    return Check(call=call, index=index, op=op, value=data["value"])


def load_expectation(path: Path) -> RegressionExpectation:
    """Parse and validate one ``expect.yaml``; raises ``ValueError`` on any
    unknown key or malformed field, naming the file and the problem."""
    path = Path(path)
    where = str(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        data = {}
    _require(isinstance(data, dict), where, f"expectation must be a mapping, got {type(data).__name__}")
    unknown = set(data) - _TOP_KEYS
    _require(not unknown, where, f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}")
    mode = data.get("mode", "strict")
    _require(mode in REPLAY_MODES, where, f"mode must be one of {REPLAY_MODES}, got {mode!r}")
    divergence = None if data.get("divergence") is None else _parse_divergence(where, data["divergence"])
    raw_checks = data.get("checks") or []
    _require(isinstance(raw_checks, list), where, f"checks must be a list, got {type(raw_checks).__name__}")
    checks = tuple(_parse_check(where, pos, c) for pos, c in enumerate(raw_checks))
    return RegressionExpectation(mode=mode, divergence=divergence, checks=checks)


def _excerpt(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _content_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _run_check(report: ReplayReport, pos: int, check: Check) -> str | None:
    label = f"checks[{pos}] ({check.call} call #{check.index + 1}, {check.op})"
    if check.call == "llm":
        if check.index >= len(report.llm_requests):
            return f"{label}: the harness made only {len(report.llm_requests)} llm call(s)"
        messages = report.llm_requests[check.index]["messages"]
        try:
            msg = messages[check.message]
        except IndexError:
            return f"{label}: message index {check.message} is out of range ({len(messages)} message(s))"
        content = msg.get("content") if isinstance(msg, dict) else msg
        text = _content_text(content)
        if check.op == "contains" and check.value not in text:
            return f"{label}: message[{check.message}] does not contain {check.value!r}; content: {_excerpt(text)!r}"
        if check.op == "not_contains" and check.value in text:
            return (
                f"{label}: message[{check.message}] must not contain {check.value!r},"
                f" but does; content: {_excerpt(text)!r}"
            )
        if check.op == "equals" and content != check.value:
            return (
                f"{label}: message[{check.message}] differs;"
                f" expected {_excerpt(_content_text(check.value))!r}, got {_excerpt(text)!r}"
            )
        return None
    if check.index >= len(report.tool_requests):
        return f"{label}: the harness made only {len(report.tool_requests)} tool call(s)"
    request = report.tool_requests[check.index]
    if check.op == "name_equals" and request["name"] != check.value:
        return f"{label}: expected tool name {check.value!r}, got {request['name']!r}"
    if check.op == "params_equal" and request["params"] != check.value:
        return (
            f"{label}: params differ; expected {_excerpt(_content_text(check.value))!r},"
            f" got {_excerpt(_content_text(request['params']))!r}"
        )
    return None


def check_report(report: ReplayReport, expectation: RegressionExpectation) -> list[str]:
    """Evaluate an expectation against a replay report.

    Returns one human-readable failure string per unmet expectation; an empty
    list means the case passes. The ``divergence`` expectation is matched
    against the replay's *first* divergence.
    """
    failures: list[str] = []
    first = report.divergences[0] if report.divergences else None
    expected = expectation.divergence
    if expected is None:
        if report.divergences:
            listed = "; ".join(d.render() for d in report.divergences[:3])
            more = f" (+{len(report.divergences) - 3} more)" if len(report.divergences) > 3 else ""
            failures.append(
                f"expected no divergence, but the replay recorded {len(report.divergences)}: {listed}{more}"
            )
    elif first is None:
        failures.append(
            f"expected the first divergence at {expected.render()}, but the replay completed with no divergence"
        )
    elif (first.kind, first.index) != (expected.kind, expected.index):
        failures.append(f"expected the first divergence at {expected.render()}, got: {first.render()}")
    elif expected.field is not None and first.field != expected.field:
        failures.append(f"expected the divergence on field {expected.field!r}, got: {first.render()}")

    for pos, check in enumerate(expectation.checks):
        failure = _run_check(report, pos, check)
        if failure is not None:
            failures.append(failure)
    return failures


async def run_regression_case(case_dir: Path) -> tuple[ReplayReport, list[str]]:
    """Replay one case directory (``cassette/`` + ``expect.yaml``).

    Returns the replay report and the failure list from :func:`check_report`
    (empty = the case passes). Tool execution and tracing suppression follow
    :func:`raven.trajectory.replay.run_replay` — no real tool runs, no spans
    are emitted.
    """
    case_dir = Path(case_dir)
    expectation = load_expectation(case_dir / EXPECTATION_FILE)
    report = await run_replay(case_dir / CASSETTE_DIR, mode=expectation.mode)
    return report, check_report(report, expectation)
