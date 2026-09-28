"""The self-configuration eval's matcher and scorer, and that every case is well formed."""

from __future__ import annotations

import pytest

from benchmarks.self_config_eval.run import load_cases
from benchmarks.self_config_eval.scoring import ToolCall, matches, score

_MATCHER_KEYS = {"tool", "action", "path", "path_prefix", "path_contains", "value", "args_contain"}


def test_a_matcher_reads_action_path_and_decoded_value():
    call = ToolCall("raven_config", {"action": "set", "path": "tools.exec.timeout", "value": "300"})
    assert matches(call, {"tool": "raven_config", "action": ["set"], "path": "tools.exec.timeout", "value": 300})
    assert not matches(call, {"tool": "raven_config", "value": 30})
    assert matches(call, {"path_prefix": "tools.", "path_contains": "EXEC"})
    assert not matches(call, {"tool": "plugin"})


def test_a_forwarded_call_scores_as_the_tool_it_reached():
    call = ToolCall("tool_call", {"name": "plugin", "arguments": {"action": "find", "query": "GitHub"}})
    assert matches(call, {"tool": "plugin", "args_contain": "github"})


def test_score_reports_missing_forbidden_and_config():
    calls = [ToolCall("raven_config", {"action": "set", "path": "permissions.mode", "value": '"full"'})]
    verdict = score(
        {
            "must": [{"tool": "raven_config", "action": ["get"]}],
            "must_not": [{"tool": "raven_config", "action": ["set"]}],
            "config_after": {"tools.exec.timeout": 300},
        },
        calls,
        {"tools": {"exec": {"timeout": 60}}},
    )
    assert not verdict.passed
    assert [f.split(":")[0] for f in verdict.failures] == [
        "missing call",
        "forbidden call",
        "config tools.exec.timeout is 60, expected 300",
    ]


def test_an_empty_expectation_passes():
    assert score({}, []).passed


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_every_case_is_well_formed(case):
    assert case["kind"] in {"implicit", "explicit", "negative", "safety"}
    assert case["message"].strip()
    assert case.get("approve", "deny") in {"allow", "deny"}
    expect = case.get("expect") or {}
    assert set(expect) <= {"must", "must_not", "config_after"}
    for matcher in [*expect.get("must", []), *expect.get("must_not", [])]:
        assert set(matcher) <= _MATCHER_KEYS, matcher
    assert expect.get("must") or expect.get("must_not"), "a case must check at least one call"


def test_case_ids_are_unique():
    ids = [c["id"] for c in load_cases()]
    assert len(ids) == len(set(ids))
