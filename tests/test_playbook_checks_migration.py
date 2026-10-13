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


def test_a_seat_that_is_not_a_mapping_passes_through_untouched() -> None:
    """The validator runs before pydantic has read anything, so it has to
    answer for whatever arrived rather than assume a dict."""
    from raven.playbook.agent_spec import SubPlaybook

    assert SubPlaybook.model_validate({"action": {"functions": {}}}).action.functions == {}
    assert migrated_action({"functions": {"judge": "x"}}) == {"functions": {"judge": "x"}}
    assert migrated_action({"checks": "not a block"}) == {"checks": "not a block"}


def test_a_rule_with_no_tool_names_nothing_to_judge() -> None:
    assert judge_from_rules([{"pathPrefix": "src/"}]) == ""
    assert judge_from_rules(["not a rule"]) == ""


def test_a_table_at_the_old_runtimes_limit_survives_the_translation() -> None:
    """Thirty-two rules were within what the rule engine supported, so a
    translation that cannot be dispatched has preserved nothing. The first cut
    of this wrote a block per rule, reached 10309 characters, and was cut to
    8000 on the wire -- which is a syntax error, which is silence, which for
    ``judge`` is permission.
    """
    from raven.agent.subagent.charter import MAX_CODE_CHARS, charter_scope, judge, parse
    from raven.playbook.agent_generator import build_payload
    from raven.playbook.agent_spec import SubPlaybook

    paths = [rf"C:\Users\Example User\Documents\Raven output\project\protected-{i:02d}.txt" for i in range(32)]
    rules = [{"tool": "exec", "forbid": path} for path in paths]

    source = judge_from_rules(rules)
    assert len(source) <= MAX_CODE_CHARS, f"translated source is {len(source)} characters"

    playbook = SubPlaybook.model_validate({"action": {"checks": {"rules": rules}}})
    charter = parse(build_payload("brief", playbook))
    assert charter is not None
    with charter_scope(charter):
        refusals = judge("exec", {"command": 'type "' + paths[-1] + '"'}, [])

    assert refusals, "the last of the thirty-two still refuses"
    assert "protected-31.txt" in refusals[0]


def test_one_tool_with_many_fragments_is_one_comprehension() -> None:
    """Grouped before anything is written: a table is mostly the same few
    shapes repeated, and a block per rule is what put the 32-rule case over
    the ceiling."""
    rules = [{"tool": "exec", "forbid": f"fragment-{i}"} for i in range(20)]
    source = judge_from_rules(rules)

    assert source.count("if name == 'exec'") == 1
    assert source.count("for v in params.values()") == 1


def test_rules_for_different_tools_stay_apart() -> None:
    rules = [
        {"tool": "exec", "forbid": "rm -rf", "message": "no rm"},
        {"tool": "write_file", "pathPrefix": "src/", "message": "src only"},
    ]
    assert _run(rules, "exec", {"command": "rm -rf /"}, []) == ["no rm"]
    assert _run(rules, "write_file", {"path": "/tmp/x"}, []) == ["src only"]
    assert _run(rules, "grep", {"pattern": "x"}, []) == []


def test_a_when_clause_separates_rules_for_the_same_tool() -> None:
    rules = [
        {"tool": "spawn", "when": {"subagent": "deck"}, "forbid": "draft", "message": "no draft deck"},
        {"tool": "spawn", "when": {"subagent": "research"}, "forbid": "draft", "message": "no draft research"},
    ]
    assert _run(rules, "spawn", {"subagent": "deck", "task": "draft it"}, []) == ["no draft deck"]
    assert _run(rules, "spawn", {"subagent": "research", "task": "draft it"}, []) == ["no draft research"]
