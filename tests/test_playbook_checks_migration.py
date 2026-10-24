"""A Harness saved before the judgement seat became one surface still loads.

Translated rather than tolerated: the rule table is read as the ``judge`` it
meant, and the test for that is the behaviour the old rule engine had, case
for case. The generated source goes through the same gate a model-written
judge does, because a migration that produced something unloadable would have
moved the failure rather than removed it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


HAND_WRITTEN = "def judge(name, params, prior):\n    if name == 'exec':\n        return ['from the code']\n"
RAISING = "def judge(name, params, prior):\n    if name == 'grep':\n        return []\n    return [params['missing']]\n"


def _migrated(code: str = HAND_WRITTEN, rules: list[dict] | None = None) -> str:
    migrated = migrated_action({"checks": {"code": code, "rules": THREE_RULES if rules is None else rules}})
    assert "checks" not in migrated
    return migrated["functions"]["judge"]


def _dispatch(source: str, name: str, params: dict, prior: list) -> list[str]:
    """What the seat refuses, asked the way a dispatch asks it."""
    from raven.agent.subagent.charter import Charter, charter_scope, judge

    with charter_scope(Charter(functions=(("judge", source),))):
        return judge(name, params, prior)


@pytest.mark.parametrize(
    ("name", "params", "prior", "expected"),
    [
        ("write_file", {"path": "/etc/passwd"}, [], ["write under ./out/", "read it first"]),
        ("exec", {"command": "rm -rf /"}, [], ["no rm -rf", "from the code"]),
        ("exec", {"command": "ls"}, [], ["from the code"]),
        ("write_file", {"path": "./out/a"}, [("read_file", {"path": "./out/a"})], []),
        ("grep", {"pattern": "x"}, [], []),
    ],
)
def test_a_hand_written_judge_and_the_table_beside_it_both_refuse(name, params, prior, expected) -> None:
    """The pre-merge runtime ran the table and then the code, and a call either
    refused did not run. Keeping the code alone would have let every call the
    table refused through, with nothing on the seat to say so."""
    assert _dispatch(_migrated(), name, params, prior) == expected


def test_a_call_both_halves_refuse_carries_both_refusals() -> None:
    code = "def judge(name, params, prior):\n    return ['from the code'] if name == 'write_file' else []\n"

    assert _dispatch(_migrated(code), "write_file", {"path": "/etc/passwd"}, []) == [
        "write under ./out/",
        "read it first",
        "from the code",
    ]


@pytest.mark.parametrize(
    ("name", "params", "expected"),
    [
        ("write_file", {"path": "/etc/passwd"}, ["write under ./out/", "read it first"]),
        ("exec", {"command": "rm -rf /"}, ["no rm -rf"]),
    ],
)
def test_a_hand_written_judge_that_raises_leaves_the_table_refusing(name, params, expected) -> None:
    """The old runtime ran the table, then the code behind a guard that read a
    raise as no opinion, so a broken judge cost its own refusals and nothing
    else. A raise that silenced the table too would turn every call it
    refused into permission."""
    assert _dispatch(_migrated(RAISING), name, params, []) == expected


def test_the_hand_written_judge_is_kept_whole_beside_the_table() -> None:
    """Its own control flow is what it was written with. Folding the table
    into its body would have to rewrite its returns."""
    source = _migrated()

    assert source.startswith(HAND_WRITTEN.strip())
    compile_function(source, "judge")


def test_the_call_the_old_table_refused_is_still_refused_when_a_dispatch_reads_it() -> None:
    """Through the stored spec, the wire payload and the charter's own judge:
    the table beside a hand-written judge refused ``write_file('/etc/passwd')``
    before the merge, and the seat that loads it must still."""
    from raven.agent.subagent.charter import charter_scope, judge, parse
    from raven.playbook.agent_generator import build_payload

    spec = AgentPlaybookSpec.model_validate(
        {"delegate": [{"name": "w", "playbook": {"action": {"checks": {"code": HAND_WRITTEN, "rules": THREE_RULES}}}}]}
    )
    charter = parse(build_payload("brief", spec.delegate[0].playbook))
    assert charter is not None
    with charter_scope(charter):
        refused = judge("write_file", {"path": "/etc/passwd"}, [])
        coded = judge("exec", {"command": "ls"}, [])

    assert refused == ["write under ./out/", "read it first"]
    assert coded == ["from the code"]


def test_the_table_does_not_depend_on_the_arguments_the_hand_written_judge_named() -> None:
    """The gate checks a judge's arity, not its argument names."""
    code = "def judge(tool, args, history):\n    return 'own: ' + tool if tool == 'grep' else []\n"
    source = _migrated(code)

    assert _dispatch(source, "write_file", {"path": "/etc/passwd"}, []) == ["write under ./out/", "read it first"]
    assert _dispatch(source, "grep", {"pattern": "x"}, []) == ["own: grep"]


@pytest.mark.parametrize(
    "code",
    [
        "def judge(name, params, str):\n    return []\n",
        "def judge(name, params, prior):\n    str = 'shadow'\n    return []\n",
        "def judge(name, params, prior):\n    return [isinstance for isinstance in [1] if False]\n",
    ],
)
def test_a_name_the_hand_written_judge_binds_for_itself_does_not_reach_the_table(code: str) -> None:
    assert _dispatch(_migrated(code), "exec", {"command": "rm -rf /"}, []) == ["no rm -rf"]


def test_a_hand_written_source_that_redefines_a_builtin_refuses_the_load() -> None:
    """Both functions share one module, so a top-level ``def list`` would be the
    ``list`` the table calls."""
    code = "def list(x):\n    return []\n\ndef judge(name, params, prior):\n    return []\n"

    with pytest.raises(ValueError, match="as its own name"):
        migrated_action({"checks": {"code": code, "rules": THREE_RULES}})


def test_a_hand_written_judge_the_gate_refuses_leaves_the_table_in_force() -> None:
    """The old runtime treated a judge it could not compile as having no
    opinion and still ran the table. The table alone is that behaviour."""
    source = _migrated("def judge(name, params, prior):\n    import os\n    return []\n")

    assert _dispatch(source, "exec", {"command": "rm -rf /"}, []) == ["no rm -rf"]


def test_a_judge_with_no_table_beside_it_is_asked_without_a_warning() -> None:
    """Most judges carry no table, so finding none is not worth a log line."""
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    try:
        refused = _dispatch(HAND_WRITTEN, "exec", {"command": "ls"}, [])
    finally:
        logger.remove(sink)

    assert refused == ["from the code"]
    assert records == []


def test_a_hand_written_judge_with_no_table_beside_it_is_kept_byte_for_byte() -> None:
    migrated = migrated_action({"checks": {"code": HAND_WRITTEN, "rules": [{"tool": "write_file"}]}})

    assert migrated["functions"] == {"judge": HAND_WRITTEN.strip()}


def test_a_merge_over_the_ceiling_refuses_the_load() -> None:
    rules = [{"tool": "exec", "forbid": "x" * 300} for _ in range(20)]
    code = "def judge(name, params, prior):\n    return " + repr("y" * 3000) + "\n"

    with pytest.raises(ValueError, match="and its hand-written judge"):
        migrated_action({"checks": {"code": code, "rules": rules}})


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


def test_a_default_message_is_built_where_it_is_needed_not_stored() -> None:
    """A default message quotes the value it is about. Writing both puts every
    forbidden path in the source twice -- once as the needle, once inside its
    own sentence, escaped again -- so the source grew with the rules squared.
    Thirty-two 91-character paths reached 8339 characters that way, over the
    ceiling a charter may carry."""
    from raven.agent.subagent.charter import MAX_CODE_CHARS

    paths = [
        rf"C:\Users\Example User\Documents\Raven output\project\customer-deliverables\protected-{i:02d}.txt"
        for i in range(32)
    ]
    rules = [{"tool": "exec", "forbid": path} for path in paths]
    source = judge_from_rules(rules)

    assert len(source) <= MAX_CODE_CHARS, f"{len(source)} characters"
    # Counted as the source spells it: a backslash path is escaped once as a
    # literal, and twice over if it is also quoted inside a stored sentence.
    assert source.count(repr(paths[-1])[1:-1]) == 1, "the path is the needle once, not inside its own sentence too"
    assert _run(rules, "exec", {"command": 'type "' + paths[-1] + '"'}, []) == [
        f"exec: this call may not contain '{paths[-1]}'"
    ]


def test_a_custom_message_is_still_carried_beside_the_defaults() -> None:
    rules = [
        {"tool": "exec", "forbid": "rm -rf"},
        {"tool": "exec", "forbid": "shutdown", "message": "ask an operator first"},
    ]

    assert _run(rules, "exec", {"command": "rm -rf /"}, []) == ["exec: this call may not contain 'rm -rf'"]
    assert _run(rules, "exec", {"command": "shutdown now"}, []) == ["ask an operator first"]


def test_a_translation_over_the_ceiling_refuses_the_load_with_what_to_do() -> None:
    """Refused where it is read rather than carried to a dispatch that drops
    it: a seat that loaded without the rules it was written with enforces
    nothing while reading as though it does."""
    from raven.playbook.agent_spec import AgentPlaybookSpec

    rules = [{"tool": "exec", "forbid": "x" * 300} for _ in range(40)]

    with pytest.raises(ValidationError, match="over the 8000 a charter may carry"):
        AgentPlaybookSpec.model_validate(
            {"delegate": [{"name": "w", "playbook": {"action": {"checks": {"rules": rules}}}}]}
        )
