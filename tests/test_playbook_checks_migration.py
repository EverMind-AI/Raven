"""A Harness saved before the judgement seat became one surface still loads.

Translated rather than tolerated: the rule table is read as the ``judge`` it
meant, and the test for that is the behaviour the old rule engine had, case
for case. The generated source goes through the same gate a model-written
judge does, because a migration that produced something unloadable would have
moved the failure rather than removed it.
"""

from __future__ import annotations

import pytest

from raven.agent.subagent.charter_code import compile_function, run_function
from raven.playbook.agent_spec import AgentPlaybookSpec
from raven.playbook.checks_migration import judge_from_rules, migrated_action

THREE_RULES = [
    {"tool": "write_file", "pathPrefix": "./out/", "message": "write under ./out/"},
    {"tool": "write_file", "requiresPrior": "read_file", "matchParam": "path", "message": "read it first"},
    {"tool": "exec", "forbid": "rm -rf", "message": "no rm -rf"},
]


def _run(rules: list[dict], name: str, params: dict, prior: list) -> list[str]:
    source = judge_from_rules(rules)
    return run_function(compile_function(source, "judge"), name, params, prior)


@pytest.mark.parametrize(
    ("name", "params", "prior", "expected"),
    [
        ("write_file", {"path": "/etc/passwd"}, [], ["write under ./out/", "read it first"]),
        ("write_file", {"path": "./out/a"}, [("read_file", {"path": "./out/a"})], []),
        ("write_file", {"path": "./out/a"}, [("read_file", {"path": "./elsewhere"})], ["read it first"]),
        ("exec", {"command": "rm -rf /"}, [], ["no rm -rf"]),
        ("grep", {"pattern": "x"}, [], []),
    ],
)
def test_the_translated_judge_answers_what_the_rule_engine_answered(name, params, prior, expected) -> None:
    assert _run(THREE_RULES, name, params, prior) == expected


def test_when_narrows_the_translation_to_the_calls_the_rule_was_about() -> None:
    rules = [{"tool": "spawn", "when": {"subagent": "deck"}, "requiresPrior": "spawn", "message": "research first"}]

    assert _run(rules, "spawn", {"subagent": "deck"}, []) == ["research first"]
    assert _run(rules, "spawn", {"subagent": "research"}, []) == []


def test_the_generated_source_passes_the_gate_a_written_judge_passes() -> None:
    """Not asserted through a helper that skips it: a migration that produced
    source the gate refuses would have moved the failure, not removed it."""
    compile_function(judge_from_rules(THREE_RULES), "judge")


def test_a_rule_table_that_refuses_nothing_produces_no_judge() -> None:
    assert judge_from_rules([{"tool": "write_file"}]) == ""
    assert judge_from_rules([]) == ""
    assert "functions" not in migrated_action({"checks": {"rules": []}})


def test_a_stored_harness_with_a_rule_table_loads() -> None:
    spec = AgentPlaybookSpec.model_validate(
        {"delegate": [{"name": "w", "playbook": {"action": {"checks": {"rules": THREE_RULES}}}}]}
    )

    playbook = spec.delegate[0].playbook
    assert playbook is not None
    assert "write under ./out/" in playbook.action.functions["judge"]


def test_a_stored_harness_with_a_hand_written_judge_keeps_it_whole() -> None:
    source = "def judge(name, params, prior):\n    return ['from the code']"
    spec = AgentPlaybookSpec.model_validate(
        {"delegate": [{"name": "w", "playbook": {"action": {"checks": {"code": source}}}}]}
    )

    assert spec.delegate[0].playbook.action.functions == {"judge": source}


def test_a_hand_written_judge_wins_over_the_table_beside_it() -> None:
    """Rare enough to choose rather than merge: what its author wrote is kept
    whole, and the load says plainly that the table was not folded in."""
    source = "def judge(name, params, prior):\n    return ['from the code']"
    migrated = migrated_action({"checks": {"code": source, "rules": THREE_RULES}})

    assert migrated["functions"] == {"judge": source}
    assert "checks" not in migrated


def test_a_seat_that_already_has_a_judge_is_left_alone() -> None:
    own = "def judge(name, params, prior):\n    return []"
    migrated = migrated_action({"functions": {"judge": own}, "checks": {"rules": THREE_RULES}})

    assert migrated["functions"] == {"judge": own}
