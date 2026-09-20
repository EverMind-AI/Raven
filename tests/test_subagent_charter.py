"""A dispatch's charter: what it narrows, what it refuses, and what it cannot be.

Two halves, and the second is the one that matters. A charter arrives from
another process, so the tests that count are the ones showing what it *cannot*
do: it cannot hand a tool back, it cannot replace the identity that tells a turn
where it is standing, and the Python it may carry cannot import, loop, or reach
a name outside a named handful.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.agent.subagent.charter import (
    Charter,
    CheckRule,
    charter_scope,
    current_charter,
    judge,
    narrowed_timeout,
    narrowed_tools,
    parse,
)
from raven.agent.subagent.charter_code import (
    CharterCodeError,
    compile_function,
    compile_judge,
    run_function,
    run_judge,
)

OFFERED = ("read_file", "write_file", "web_search", "web_fetch", "exec")


# --------------------------------------------------------------------------- #
# Reading one off the wire                                                      #
# --------------------------------------------------------------------------- #


def test_a_payload_becomes_a_charter() -> None:
    charter = parse(
        {
            "prompt": "only look at A",
            "tools": ["web_search", "web_fetch"],
            "stopWhen": "both tables land",
            "checks": [{"tool": "write_file", "pathPrefix": "./out/a/", "message": "out/a only"}],
        }
    )
    assert charter is not None
    assert charter.prompt == "only look at A"
    assert charter.tools == ("web_search", "web_fetch")
    assert charter.stop_when == "both tables land"
    assert charter.checks[0].path_prefix == "./out/a/"


def test_nothing_in_means_nothing_out() -> None:
    """An empty payload is not a charter: a turn carrying one must be exactly
    the turn it would have been without."""
    assert parse({}) is None
    assert parse(None) is None
    assert parse("not a dict") is None


def test_an_unknown_key_is_dropped_rather_than_refused() -> None:
    """The sender may be a newer host. A field this build cannot use is not a
    reason to fail the dispatch it rode in on."""
    charter = parse({"prompt": "hi", "somethingNewer": {"deeply": "nested"}})
    assert charter is not None
    assert charter.prompt == "hi"


def test_an_over_long_prompt_is_cut_not_honoured() -> None:
    """A brief is a brief. Unbounded, it would push the turn's own identity out
    of the window it is meant to sit beside."""
    charter = parse({"prompt": "x" * 99_999})
    assert charter is not None
    assert len(charter.prompt) <= 4000


def test_an_empty_tool_list_survives_the_read() -> None:
    """Three-valued: absent is "whatever this worker offers", `[]` is a real
    answer, and folding one into the other reverses its meaning."""
    assert parse({"tools": []}).tools == ()
    assert parse({"prompt": "x"}).tools is None


# --------------------------------------------------------------------------- #
# Narrowing: only ever downward                                                 #
# --------------------------------------------------------------------------- #


def test_the_charter_withholds_what_it_did_not_ask_for() -> None:
    with charter_scope(Charter(tools=("web_search", "web_fetch"))):
        assert narrowed_tools(OFFERED) == frozenset({"read_file", "write_file", "exec"})


def test_a_charter_cannot_hand_back_a_tool_this_install_lacks() -> None:
    """The narrowing is expressed as a withholding, so a name the charter asks
    for that nobody offers simply is not there to give. Widening is not refused
    so much as unsayable."""
    with charter_scope(Charter(tools=("web_search", "a_tool_nobody_has"))):
        withheld = narrowed_tools(OFFERED)
    assert "a_tool_nobody_has" not in withheld
    assert withheld == frozenset({"read_file", "write_file", "web_fetch", "exec"})


def test_an_empty_tool_list_withholds_everything() -> None:
    with charter_scope(Charter(tools=())):
        assert narrowed_tools(OFFERED) == frozenset(OFFERED)


def test_no_tools_field_withholds_nothing() -> None:
    with charter_scope(Charter(prompt="just a brief")):
        assert narrowed_tools(OFFERED) == frozenset()


def test_outside_a_scope_nothing_is_withheld() -> None:
    assert narrowed_tools(OFFERED) == frozenset()
    assert current_charter() is None


def test_the_scope_restores_on_an_exception() -> None:
    """It rides a `finally`: a dispatch that raises must not leave its charter
    binding the next turn on the same task."""
    with pytest.raises(RuntimeError):
        with charter_scope(Charter(tools=())):
            raise RuntimeError("boom")
    assert current_charter() is None


# --------------------------------------------------------------------------- #
# Declarative judgements                                                        #
# --------------------------------------------------------------------------- #


def _rules() -> Charter:
    return Charter(
        checks=(
            CheckRule(tool="write_file", path_prefix="./out/", message="write under ./out/"),
            CheckRule(tool="write_file", requires_prior="read_file", match_param="path", message="read it first"),
            CheckRule(tool="exec", forbid="rm -rf", message="no rm -rf"),
        )
    )


def test_a_path_outside_the_prefix_is_refused() -> None:
    with charter_scope(_rules()):
        assert "write under ./out/" in judge("write_file", {"path": "/etc/passwd"}, [])


def test_reading_the_same_file_first_satisfies_the_rule() -> None:
    with charter_scope(_rules()):
        assert judge("write_file", {"path": "./out/a"}, [("read_file", {"path": "./out/a"})]) == []


def test_reading_a_different_file_does_not() -> None:
    """`matchParam` is what separates "read the file you are about to write"
    from "read something, anything"."""
    with charter_scope(_rules()):
        assert judge("write_file", {"path": "./out/a"}, [("read_file", {"path": "./elsewhere"})]) == ["read it first"]


def test_a_forbidden_fragment_is_caught_in_any_argument() -> None:
    with charter_scope(_rules()):
        assert judge("exec", {"command": "rm -rf /"}, []) == ["no rm -rf"]


def test_a_rule_says_nothing_about_another_tool() -> None:
    with charter_scope(_rules()):
        assert judge("grep", {"pattern": "x"}, []) == []


def test_when_narrows_a_rule_to_the_calls_it_is_about() -> None:
    charter = Charter(
        checks=(CheckRule(tool="spawn", when={"subagent": "deck"}, requires_prior="spawn", message="research first"),)
    )
    with charter_scope(charter):
        assert judge("spawn", {"subagent": "deck"}, []) == ["research first"]
        assert judge("spawn", {"subagent": "research"}, []) == []


def test_no_checks_means_no_opinion() -> None:
    with charter_scope(Charter(prompt="x")):
        assert judge("write_file", {"path": "/etc/passwd"}, []) == []


# --------------------------------------------------------------------------- #
# A charter's own judge: what the gate admits                                   #
# --------------------------------------------------------------------------- #

GOOD_JUDGE = """
def judge(name, params, prior):
    if name != "write_file":
        return []
    path = str(params.get("path", ""))
    if not path.startswith("./out/"):
        return ["write under ./out/"]
    if not any(n == "read_file" and p.get("path") == path for n, p in prior):
        return ["read it first"]
    return []
"""


def test_a_judgement_about_one_call_compiles_and_runs() -> None:
    fn = compile_judge(GOOD_JUDGE)
    assert run_judge(fn, "write_file", {"path": "/etc/x"}, []) == ["write under ./out/"]
    assert run_judge(fn, "write_file", {"path": "./out/a"}, []) == ["read it first"]
    assert run_judge(fn, "write_file", {"path": "./out/a"}, [("read_file", {"path": "./out/a"})]) == []
    assert run_judge(fn, "grep", {}, []) == []


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("an import", "import os\ndef judge(n, p, q):\n    return []"),
        ("a from-import", "from os import path\ndef judge(n, p, q):\n    return []"),
        ("a while loop", "def judge(n, p, q):\n    while True:\n        pass"),
        ("a for loop", "def judge(n, p, q):\n    for i in q:\n        pass\n    return []"),
        ("a call nobody allowed", "def judge(n, p, q):\n    return [compile('x', 'y', 'exec')]"),
        ("a dunder", "def judge(n, p, q):\n    return [str(n.__class__)]"),
        ("a private attribute", "def judge(n, p, q):\n    return [str(p._secret)]"),
        ("a statement outside a function", "x = 1\ndef judge(n, p, q):\n    return []"),
        ("no entry point", "def other(n, p, q):\n    return []"),
        ("a class", "class C:\n    pass\ndef judge(n, p, q):\n    return []"),
        ("a lambda handed somewhere", "def judge(n, p, q):\n    return [(lambda: 1)()]"),
        ("nothing at all", "   "),
    ],
)
def test_the_gate_refuses_what_a_judgement_does_not_need(label: str, source: str) -> None:
    """Each of these is refused on the parse tree, before anything is compiled,
    so a refused source was never executable rather than executable-but-unlucky."""
    with pytest.raises(CharterCodeError):
        compile_judge(source)


def test_an_over_long_source_is_refused_before_parsing() -> None:
    with pytest.raises(CharterCodeError, match="characters"):
        compile_judge("def judge(n, p, q):\n    return []\n" + "# padding\n" * 5000)


def test_generic_function_compilation_rejects_unknown_verbs_and_runtime_errors_are_silence() -> None:
    with pytest.raises(CharterCodeError, match="unsupported charter function"):
        compile_function("def archive(step):\n    return None", "archive")
    assert run_function(lambda value: 1 / 0, {"detached": True}) is None


def test_a_judge_cannot_reach_a_name_it_was_not_given() -> None:
    """The namespace is an allow-list, not a denylist: a denylist is only as
    complete as the day it was written."""
    fn = compile_judge("def judge(n, p, q):\n    return [str(len(p))]")
    assert run_judge(fn, "x", {"a": 1}, []) == ["1"]


def test_a_judge_that_raises_has_said_nothing() -> None:
    """Not "allow" and not "refuse". Letting it refuse would let one bad line
    stop every call; letting the exception out would cost the dispatch. The
    declarative rules beside it still apply."""
    # A missing key rather than arithmetic: division is not on the allow-list,
    # so `1 / 0` would be refused at the gate and never reach the runtime this
    # test is about.
    fn = compile_judge('def judge(n, p, q):\n    return [p["absent"]]')
    assert run_judge(fn, "x", {}, []) == []


def test_a_judge_that_answers_nonsense_is_ignored() -> None:
    fn = compile_judge("def judge(n, p, q):\n    return 42")
    assert run_judge(fn, "x", {}, []) == []


def test_one_sentence_is_as_good_as_a_list() -> None:
    fn = compile_judge('def judge(n, p, q):\n    return "just this"')
    assert run_judge(fn, "x", {}, []) == ["just this"]


def test_the_judge_cannot_edit_the_call_it_judges() -> None:
    """Copies go in. A judge that mutated its arguments would be editing the
    call it was asked to rule on."""
    fn = compile_judge('def judge(n, p, q):\n    p["path"] = "/etc/passwd"\n    return []')
    params = {"path": "./out/a"}
    run_judge(fn, "write_file", params, [])
    assert params == {"path": "./out/a"}


# --------------------------------------------------------------------------- #
# A charter's judge, reached through the charter itself                         #
# --------------------------------------------------------------------------- #


def test_a_charter_can_carry_its_own_judgement() -> None:
    with charter_scope(Charter(code=GOOD_JUDGE)):
        assert judge("write_file", {"path": "/etc/x"}, []) == ["write under ./out/"]
        assert judge("write_file", {"path": "./out/a"}, [("read_file", {"path": "./out/a"})]) == []


def test_rules_and_code_both_speak() -> None:
    """Neither replaces the other: the declarative half is what still applies
    when the code half is refused or silent."""
    charter = Charter(
        checks=(CheckRule(tool="write_file", path_prefix="./out/", message="from the rule"),),
        code=GOOD_JUDGE,
    )
    with charter_scope(charter):
        refusals = judge("write_file", {"path": "/etc/x"}, [])
    assert "from the rule" in refusals
    assert "write under ./out/" in refusals


def test_a_judge_the_gate_refused_leaves_the_rules_standing() -> None:
    """A brief whose code was rejected has said nothing about this call.
    Refusing every call instead would be a far larger claim than its author made."""
    charter = Charter(
        checks=(CheckRule(tool="write_file", path_prefix="./out/", message="from the rule"),),
        code="import os\ndef judge(n, p, q):\n    return ['never runs']",
    )
    with charter_scope(charter):
        assert judge("write_file", {"path": "/etc/x"}, []) == ["from the rule"]
        assert judge("write_file", {"path": "./out/a"}, []) == []


def test_the_wire_carries_code_and_a_deadline() -> None:
    charter = parse({"code": GOOD_JUDGE, "timeoutSeconds": 90})
    assert charter is not None
    assert charter.timeout_s == 90
    assert "startswith" in charter.code


# --------------------------------------------------------------------------- #
# The deadline: tighter, never looser                                           #
# --------------------------------------------------------------------------- #


def test_a_brief_may_shorten_a_dispatch() -> None:
    with charter_scope(Charter(timeout_s=60)):
        assert narrowed_timeout(600) == 60


def test_a_brief_may_shorten_one_that_had_no_limit() -> None:
    """Every shipped worker configures none, so this is the case that matters."""
    with charter_scope(Charter(timeout_s=60)):
        assert narrowed_timeout(None) == 60


def test_a_brief_may_not_lengthen_one() -> None:
    with charter_scope(Charter(timeout_s=600)):
        assert narrowed_timeout(30) == 30


def test_no_brief_leaves_the_deadline_alone() -> None:
    assert narrowed_timeout(None) is None
    assert narrowed_timeout(600) == 600
    with charter_scope(Charter(prompt="just a brief")):
        assert narrowed_timeout(600) == 600


def test_a_nonsense_deadline_is_ignored() -> None:
    """A deadline that is not a positive number is not a deadline. Carried
    through as one it would either never fire or fire immediately."""
    assert parse({"prompt": "x", "timeoutSeconds": 0}).timeout_s is None
    assert parse({"prompt": "x", "timeoutSeconds": -5}).timeout_s is None
    assert parse({"prompt": "x", "timeoutSeconds": "soon"}).timeout_s is None


def test_the_judge_cache_does_not_grow_without_a_ceiling() -> None:
    """A worker process outlives every dispatch it serves, and the cache is
    keyed by source, so an unbounded one grows with each new brief."""
    from raven.agent.subagent.charter import _COMPILED, MAX_COMPILED

    _COMPILED.clear()
    try:
        for n in range(MAX_COMPILED + 5):
            source = f"def judge(name, params, prior):\n    return [] if {n} else []\n"
            with charter_scope(Charter(code=source)):
                judge("write_file", {"path": "a"}, ())
            assert len(_COMPILED) <= MAX_COMPILED
    finally:
        _COMPILED.clear()


# --------------------------------------------------------------------------- #
# The registry seat                                                            #
# --------------------------------------------------------------------------- #


def test_a_registry_nobody_handed_a_role_still_refuses_what_the_charter_refuses() -> None:
    """Most registries this tree builds take no Action role, and one of them --
    the curator's -- dispatches inside the turn's charter scope. An absent role
    is "nobody chose one here", not "this dispatch carries no Charter"."""
    from raven.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    with charter_scope(_rules()):
        assert registry._verifier_refusals("write_file", {"path": "/etc/passwd"}) == [
            "write under ./out/",
            "read it first",
        ]
        assert registry._verifier_refusals("write_file", {"path": "./out/a"}) == ["read it first"]
    assert registry._verifier_refusals("write_file", {"path": "/etc/passwd"}) == [], (
        "outside a dispatch there is no playbook to enforce"
    )


def test_an_injected_role_is_asked_instead_of_the_default() -> None:
    """The provider is the seam a replacement uses; when one is installed it is
    the only thing asked, charter or no charter."""
    from raven.agent.tools.registry import ToolRegistry

    asked: list[str] = []

    class Loud:
        def ask_judge(self, name, params, prior, participants=()):
            asked.append(name)
            return ["the role said no"]

    registry = ToolRegistry(verifier_provider=lambda: Loud())
    with charter_scope(_rules()):
        assert registry._verifier_refusals("write_file", {"path": "./out/a"}) == ["the role said no"]
    assert asked == ["write_file"]


def test_a_dispatch_brings_its_judgements_as_a_participant() -> None:
    """The Charter answers the same verb a plugin answers, rather than being
    read by the role itself. That is what lets a judgement generated for one
    dispatch join the same list later."""
    from raven.agent.subagent.charter import charter_participants

    assert charter_participants() == (), "no charter bound, no participant"
    with charter_scope(Charter(prompt="just a brief")):
        assert charter_participants() == (), "a charter with no judgements brings none"
    with charter_scope(_rules()):
        brought = charter_participants()
        assert len(brought) == 1
        assert brought[0].judge("write_file", {"path": "/etc/passwd"}, []) == [
            "write under ./out/",
            "read it first",
        ]


def test_a_refused_generated_function_is_cached_as_silence(monkeypatch) -> None:
    from raven.agent.subagent import charter as charter_mod

    source = "def advise(step):\n    import os\n    return 'never'"
    charter_mod._COMPILED.clear()
    monkeypatch.setattr(charter_mod, "MAX_COMPILED", 0)
    try:
        with charter_scope(Charter(functions=(("advise", source),))):
            assert charter_mod._generated_answer("advise", {}) is None
            assert charter_mod._generated_answer("advise", {}) is None
        assert charter_mod._COMPILED[("advise", source)] is False
    finally:
        charter_mod._COMPILED.clear()


def test_a_plugins_own_rules_speak_before_the_dispatchs() -> None:
    """Participant order is product first: a veto needs one voice, and the
    wording the model reads should be the product's own where both would
    refuse."""
    from raven.agent.harness.action import DefaultAction

    class Product:
        def judge(self, name, params, prior):
            return ["the product refuses this one"]

    action = DefaultAction()
    with charter_scope(_rules()):
        assert action.ask_judge("write_file", {"path": "/etc/passwd"}, [], [Product()]) == [
            "the product refuses this one"
        ]
        assert action.ask_judge("write_file", {"path": "/etc/passwd"}, []) == [
            "write under ./out/",
            "read it first",
        ]


def test_one_sentence_of_refusal_stays_one_sentence() -> None:
    """A bare ``str`` satisfies ``Sequence[str]`` structurally, so a participant
    that refuses with one sentence passes every annotation and every type check
    -- and iterating it yields one refusal per character. Eighteen refusals for
    a malformed answer is the opposite of "a malformed answer is silence", and
    this seam exists to carry judgements that are generated rather than written,
    where a bare string is a plausible thing to answer."""
    from raven.agent.harness.participants import compose_judge

    class Sentence:
        def judge(self, name, params, prior):
            return "write under ./out/"

    class Blank:
        def judge(self, name, params, prior):
            return "   "

    assert compose_judge("write_file", {}, [], [Sentence()]) == ["write under ./out/"]
    assert compose_judge("write_file", {}, [], [Blank(), Sentence()]) == ["write under ./out/"], (
        "whitespace is not a refusal, and must not stop the next participant"
    )


def test_a_participant_that_says_nothing_lets_the_next_one_speak() -> None:
    """A veto stops at the first refusal, not at the first participant."""
    from raven.agent.harness.action import DefaultAction

    class Quiet:
        def judge(self, name, params, prior):
            return []

    with charter_scope(_rules()):
        assert DefaultAction().ask_judge("write_file", {"path": "/etc/passwd"}, [], [Quiet()]) == [
            "write under ./out/",
            "read it first",
        ]


# --------------------------------------------------------------------------- #
# The seat a DAG node's charter travels in                                     #
# --------------------------------------------------------------------------- #


async def test_a_chartered_node_dispatches_inside_its_charter() -> None:
    """The layer that refuses a stray write before it lands, rather than after.

    The three other layers all act on a write that already happened; this one
    is the only one that can stop one, and it reaches the worker by being in
    scope for the call -- which is what the transport asks for on the way out --
    rather than by changing any signature.
    """
    from raven.agent.subagent.dag_tool import _CharteredBackend
    from raven.agent.subagent.delegate import outbound_charter

    payload = {"capability": {"tools": ["read_file"]}}
    seen: list[Any] = []

    class _Backend:
        name = "echo"

        async def run(self, *_args: Any, **_kwargs: Any) -> str:
            seen.append(outbound_charter())
            return "done"

    wrapped = _CharteredBackend(_Backend(), payload)

    assert await wrapped.run("a prompt") == "done"
    assert seen == [payload], "the call went out without its charter"
    assert outbound_charter() is None, "the charter outlived the call"


def test_a_wrapped_backend_is_the_backend_in_every_other_respect() -> None:
    """The runner reads attributes off a backend that is not `run`, and a proxy
    that answered only its own would change what the node is."""
    from raven.agent.subagent.dag_tool import _CharteredBackend

    class _Backend:
        name = "echo"
        supports_instances = True

    wrapped = _CharteredBackend(_Backend(), {})

    assert wrapped.name == "echo"
    assert wrapped.supports_instances is True


def test_only_the_nodes_a_charter_names_are_wrapped() -> None:
    """Matched on the node id exactly, which is what a round taken up again has
    to get right: its nodes carry an attempt suffix, and a charter keyed without
    one reaches nothing at all -- silently, because an unwrapped node runs."""
    from raven.agent.subagent.dag_tool import _chartered, _CharteredBackend

    backends = {"pb-r01x1-planner": object(), "pb-r01x1-developer": object()}

    bound = _chartered(backends, {"pb-r01x1-planner": {"capability": {"tools": []}}})

    assert isinstance(bound["pb-r01x1-planner"], _CharteredBackend)
    assert bound["pb-r01x1-developer"] is backends["pb-r01x1-developer"]
    assert _chartered(backends, {}) is backends, "an ordinary run rebinds nothing"
