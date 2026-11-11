"""Acceptance suite loading and the report model of the agent package validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolver.validator.report import LayerResult, Report, RuleResult, ValidatorInputError, exit_code
from evolver.validator.suite import load_suite


def _suite(root: Path, cases: dict[str, dict], **meta) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    head = {"name": "s", "version": "1", "target": None, "timeout_s": 900, **meta}
    (root / "suite.json").write_text(json.dumps(head), encoding="utf-8")
    (root / "cases").mkdir(exist_ok=True)
    for name, body in cases.items():
        (root / "cases" / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    return root


def _case(case_id: str = "a", **extra) -> dict:
    return {
        "id": case_id,
        "task": "Write hello.txt.",
        "criteria": [{"id": "c1", "statement": "hello.txt exists."}],
        **extra,
    }


def test_loads_cases_with_text_and_base64_files(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case(files={"in.txt": "hi", "b.bin": {"base64": "AAE="}})})
    suite = load_suite(root, agent_name="Demo")
    assert [c.id for c in suite.cases] == ["a"]
    assert suite.cases[0].files == {"in.txt": b"hi", "b.bin": b"\x00\x01"}
    assert suite.cases[0].timeout_s == 900
    assert suite.cases[0].criteria[0].statement == "hello.txt exists."
    assert len(suite.digest) == 64


def test_case_timeout_overrides_suite_default(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case(timeout_s=30)})
    assert load_suite(root, agent_name="Demo").cases[0].timeout_s == 30


def test_target_naming_another_agent_is_input_error(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case()}, target="Other")
    with pytest.raises(ValidatorInputError, match="Other"):
        load_suite(root, agent_name="Demo")


def test_target_naming_this_agent_loads(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case()}, target="Demo")
    assert load_suite(root, agent_name="Demo").target == "Demo"


@pytest.mark.parametrize("bad", ["../x.txt", "/etc/x.txt", "a/../../x.txt", "a\\b.txt"])
def test_case_file_path_escape_is_input_error(tmp_path, bad):
    root = _suite(tmp_path / "s", {"a": _case(files={bad: "x"})})
    with pytest.raises(ValidatorInputError):
        load_suite(root, agent_name="Demo")


def test_duplicate_case_ids_are_input_errors(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case("same"), "b": _case("same")})
    with pytest.raises(ValidatorInputError, match="same"):
        load_suite(root, agent_name="Demo")


def test_duplicate_criterion_ids_are_input_errors(tmp_path):
    crit = [{"id": "c1", "statement": "x"}, {"id": "c1", "statement": "y"}]
    root = _suite(tmp_path / "s", {"a": _case(criteria=crit)})
    with pytest.raises(ValidatorInputError, match="c1"):
        load_suite(root, agent_name="Demo")


def test_case_without_criteria_is_input_error(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case(criteria=[])})
    with pytest.raises(ValidatorInputError):
        load_suite(root, agent_name="Demo")


def test_missing_suite_json_is_input_error(tmp_path):
    (tmp_path / "s").mkdir()
    with pytest.raises(ValidatorInputError):
        load_suite(tmp_path / "s", agent_name="Demo")


def test_suite_without_cases_is_input_error(tmp_path):
    root = _suite(tmp_path / "s", {})
    with pytest.raises(ValidatorInputError):
        load_suite(root, agent_name="Demo")


def test_digest_changes_when_a_case_changes(tmp_path):
    root = _suite(tmp_path / "s", {"a": _case()})
    before = load_suite(root, agent_name="Demo").digest
    (root / "cases" / "a.json").write_text(json.dumps(_case(task="Write bye.txt.")), encoding="utf-8")
    assert load_suite(root, agent_name="Demo").digest != before


def _report(outcome: str) -> Report:
    return Report(
        folder_digest="f",
        suite_digest=None,
        raven_version="0",
        raven_commit=None,
        agent_model="m",
        judge_model="j",
        rendered={},
        layers=[LayerResult("F", [RuleResult("F1", "pass", "ok")])],
        cases=[],
        verdicts=[],
        outcome=outcome,
    )


@pytest.mark.parametrize("outcome, code", [("PASS", 0), ("STATIC-OK", 0), ("FAIL", 1)])
def test_exit_code_maps_outcomes(outcome, code):
    assert exit_code(_report(outcome)) == code


def test_layer_passes_unless_a_rule_fails():
    assert LayerResult("F", [RuleResult("F5", "advisory", "build output")]).passed
    assert not LayerResult("F", [RuleResult("F1", "fail", "missing run.py")]).passed


def test_report_json_round_trips_through_json():
    data = json.loads(json.dumps(_report("PASS").to_json()))
    assert data["outcome"] == "PASS"
    assert data["layers"][0]["results"][0]["rule"] == "F1"
