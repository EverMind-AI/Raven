"""Execution side: executor plans (both modes) and the runtime funnel."""

import json
from pathlib import Path

from raven.playbook import (
    NodeSpec,
    ParamSpec,
    PlaybookExecutor,
    PlaybookRuntime,
    PlaybookSpec,
    PlaybookStore,
    Triggers,
)
from raven.playbook.matcher import TriggerIndex


class FakeDagTool:
    def __init__(self):
        self.calls = []
        self.context = None

    def set_context(self, channel, chat_id, session_key=None):
        self.context = (channel, chat_id, session_key)

    async def run_with_roles(self, nodes, *, roles, role_capabilities, background=True):
        self.calls.append({"nodes": nodes, "roles": roles, "caps": role_capabilities})
        return "DAG run wf-1 started in the background (%d nodes)." % len(nodes)


def _factory_recorder(record):
    def make(build):
        record.append(build)
        return object()

    return make


def _dag_spec(**over):
    base = dict(
        name="weekly-feedback",
        description="weekly user-feedback analysis",
        mode="dag",
        triggers=Triggers(keywords=["user feedback"]),
        params={
            "week_of": ParamSpec(type="string", required=True, description="which week should be analyzed?"),
            "audience": ParamSpec(default="PM", description="who reads the report?"),
        },
        nodes=[
            NodeSpec(
                id="pull",
                agent="data-raven",
                prompt_template="pull the feedback for ${params.week_of}",
                skills=["sql-queries"],
            ),
            NodeSpec(
                id="report",
                agent="content-raven",
                prompt_template="write the weekly report for ${params.audience} from {{ pull.output_path }}",
                depends_on=["pull"],
                mcps=["slack"],
            ),
        ],
    )
    base.update(over)
    return PlaybookSpec(**base)


# ---------------------------------------------------------------- executor


async def test_executor_asks_with_the_param_description():
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool())
    plan = await ex.execute(_dag_spec(), params={})
    assert plan.kind == "questions"
    assert "which week should be analyzed?" in plan.reply


async def test_the_ask_can_be_answered_back_into_a_match():
    """Matching is stateless, so an answer only reaches the gate if it carries a
    trigger word. A bare "2026-08-11" nominates nothing and the run is lost with
    the user believing they answered -- so the ask has to model a reply that
    comes back, and this pins that the modelled reply actually does."""
    spec = _dag_spec()
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool())
    plan = await ex.execute(spec, params={})

    # The hint names both the trigger to repeat and the slot to fill.
    assert "user feedback" in plan.reply
    assert "week_of=" in plan.reply

    # A bare answer wins no nomination; the modelled phrasing does.
    index = TriggerIndex({spec.name: spec.triggers})
    assert index.match("2026-08-11") == []
    assert index.match("user feedback week_of=2026-08-11") == [spec.name]


async def test_executor_dag_builds_per_node_backends_and_fills_params():
    built = []
    tool = FakeDagTool()
    ex = PlaybookExecutor(backend_factory=_factory_recorder(built), dag_tool=tool)

    plan = await ex.execute(_dag_spec(), params={"week_of": "2026-08-11"})

    assert plan.kind == "dag"
    # Backend keys and node ids carry the run-unique namespace; the plain
    # author ids survive as the last dash segment.
    by_name = {b.name.rsplit("-", 1)[-1]: b for b in built}
    assert by_name["pull"].skills_allow == ["sql-queries"]
    assert by_name["report"].skills_allow is None
    nodes = {n["id"].rsplit("-", 1)[-1]: n for n in tool.calls[0]["nodes"]}
    assert "2026-08-11" in nodes["pull"]["prompt_template"]
    assert "PM" in nodes["report"]["prompt_template"]  # default filled
    # Runtime refs stay runtime refs, retargeted to the namespaced id.
    assert "{{ %s.output_path }}" % nodes["pull"]["id"] in nodes["report"]["prompt_template"]
    assert nodes["report"]["depends_on"] == [nodes["pull"]["id"]]
    # mcps is the one unsupported field that degrades instead of failing: the
    # node still runs, with a note saying what it lost. instance and confirm are
    # refused in validate.py instead, so they never reach dispatch.
    assert any("mcps" in n for n in plan.notes)
    assert not any("instance" in n or "confirm" in n for n in plan.notes)


class ComposeProvider:
    """Scripted graph-composition responses."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls.append(messages)

        class _TC:
            def __init__(self, args):
                self.arguments = args

        class _R:
            def __init__(self, args):
                self.has_tool_calls = args is not None
                self.tool_calls = [_TC(args)] if args is not None else []

        return _R(self._payloads.pop(0))


def _prompt_spec():
    return PlaybookSpec(
        name="due-diligence",
        description="run due diligence on a target company",
        mode="prompt",
        triggers=Triggers(keywords=["due diligence"]),
        params={"target": ParamSpec(required=True, description="which company is the target?")},
        prompts="compose a two-layer graph for ${params.target}: a breadth scan, then a summary.",
    )


GOOD_GRAPH = {
    "nodes": [
        {"id": "scan", "agent": "research-raven", "promptTemplate": "breadth-scan AcmeAI"},
        {
            "id": "sum",
            "agent": "content-raven",
            "promptTemplate": "summarize {{ scan.output }}",
            "dependsOn": ["scan"],
        },
    ]
}


async def test_prompt_mode_composes_a_graph_then_dispatches():
    tool = FakeDagTool()
    provider = ComposeProvider([json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool, provider=provider)
    ex.set_roster({"research-raven": "research", "content-raven": "writing"})

    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "dag"
    assert len(tool.calls) == 1
    assert "AcmeAI" in provider.calls[0][0]["content"]  # params substituted into the compose prompt


async def test_prompt_mode_bad_graph_gets_one_repair_then_degrades():
    bad = {"nodes": [{"id": "a", "agent": "research-raven", "promptTemplate": "use {{ ghost.output }}"}]}
    provider = ComposeProvider([json.dumps(bad), json.dumps(bad)])
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool(), provider=provider)
    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})
    assert plan.kind == "questions"
    assert "Graph assembly" in plan.reply
    assert len(provider.calls) == 2  # one repair round happened


# ---------------------------------------------------------------- runtime


class GateProvider:
    def __init__(self, verdict):
        self._verdict = verdict
        self.calls = 0

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls += 1

        class _TC:
            def __init__(self, args):
                self.arguments = args

        class _R:
            has_tool_calls = True

            def __init__(self, args):
                self.tool_calls = [_TC(args)]

        return _R(json.dumps(self._verdict))


async def test_runtime_full_funnel_hits_and_dispatches(tmp_path: Path):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec())
    tool = FakeDagTool()
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool)
    rt = PlaybookRuntime(
        provider=GateProvider(
            {
                "match": "weekly-feedback",
                "confidence": "high",
                "reason": "clearly wants the weekly report",
                "params": {"week_of": "2026-08-11"},
            }
        ),
        store=store,
        executor=ex,
    )

    plan = await rt.consider(
        "sort out this week's user feedback", channel="cli", chat_id="direct", session_key="cli:direct"
    )

    assert plan is not None and plan.kind == "dag"
    assert tool.context == ("cli", "direct", "cli:direct")


async def test_runtime_passes_through_on_no_hit_and_low_confidence(tmp_path: Path):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec())
    provider = GateProvider({"match": "weekly-feedback", "confidence": "low", "reason": "merely mentioned"})
    rt = PlaybookRuntime(
        provider=provider,
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool()),
    )

    assert await rt.consider("what's for lunch") is None
    assert provider.calls == 0
    assert await rt.consider("the user feedback button is stuck") is None


async def test_runtime_disabled_names_leave_matching_but_stay_runnable(tmp_path: Path):
    """The switch is config, not file content, and it only mutes the passive
    funnel: a disabled playbook is out of L1, still loads, and still runs by
    name -- the explicit entries (run_playbook tool, CLI run) keep working."""
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec())
    store.save(_dag_spec(name="switched-off", triggers=Triggers(keywords=["nightly digest"])))
    provider = GateProvider({"match": None, "confidence": "low", "reason": ""})

    def build(disabled):
        return PlaybookRuntime(
            provider=provider,
            store=store,
            executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool()),
            disabled=disabled,
        )

    rt = build(["switched-off"])
    assert set(rt._specs) == {"switched-off", "weekly-feedback"}
    # Its trigger no longer nominates: the message passes through without
    # even reaching the gate.
    assert await rt.consider("run the nightly digest please") is None
    assert provider.calls == 0
    # But the name still resolves, and the listing says why it is special.
    plan = await rt.run_named("switched-off", {"week_of": "w33"})
    assert plan is not None and plan.kind == "dag"
    assert dict(rt.listing())["switched-off"].endswith("[disabled]")

    assert build([])._disabled == set()


# --- Run-unique node ids: the runner holds ids unique per session, authors
# write plain stable ones, and the executor's rewrite bridges the two.


class SessionUniqueDagTool(FakeDagTool):
    """Rejects a node id it has already seen, like the real runner's
    per-session registry."""

    def __init__(self):
        super().__init__()
        self.seen = set()

    async def run_with_roles(self, nodes, *, roles, role_capabilities, background=True):
        clashes = [n["id"] for n in nodes if n["id"] in self.seen]
        if clashes:
            return "Error: node ids already used in this session: %s" % ", ".join(clashes)
        self.seen.update(n["id"] for n in nodes)
        return await super().run_with_roles(nodes, roles=roles, role_capabilities=role_capabilities)


async def test_same_playbook_runs_twice_in_one_session():
    tool = SessionUniqueDagTool()
    executor = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool)

    first = await executor.execute(_dag_spec(), {"week_of": "2026-08-10"})
    second = await executor.execute(_dag_spec(), {"week_of": "2026-08-17"})

    assert first.kind == "dag"
    assert second.kind == "dag"
    assert len(tool.calls) == 2


async def test_node_ids_and_references_are_rewritten_in_step():
    tool = FakeDagTool()
    executor = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool)

    plan = await executor.execute(_dag_spec(), {"week_of": "2026-08-10"})

    assert plan.kind == "dag"
    by_plain = {n["id"].rsplit("-", 1)[-1]: n for n in tool.calls[0]["nodes"]}
    pull, report = by_plain["pull"], by_plain["report"]
    assert pull["id"] != "pull" and pull["id"].startswith("weekly-feedback-")
    # One rewrite table per dispatch: both nodes carry the same run tag.
    assert pull["id"].rsplit("-", 1)[0] == report["id"].rsplit("-", 1)[0]
    assert report["depends_on"] == [pull["id"]]
    assert ("{{ %s.output_path }}" % pull["id"]) in report["prompt_template"]
    assert "{{ pull.output_path }}" not in report["prompt_template"]
    # Params were already compiled in; the rewrite must not disturb them.
    assert "pull the feedback for 2026-08-10" == pull["prompt_template"]


async def test_rewrite_leaves_foreign_references_alone():
    tool = FakeDagTool()
    executor = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="pull",
                agent="data-raven",
                prompt_template="read {{ ref:notes/summary.md }} then {{ elsewhere.output }}",
            )
        ]
    )

    await executor.execute(spec, {"week_of": "2026-08-10"})

    template = tool.calls[0]["nodes"][0]["prompt_template"]
    assert "{{ ref:notes/summary.md }}" in template
    # An id outside this graph is not ours to rewrite; the runner's own
    # whitelist decides what happens to it.
    assert "{{ elsewhere.output }}" in template


async def test_context_is_recorded_even_when_the_funnel_passes_through(tmp_path: Path):
    """run_named reuses the last address consider() saw, and the turns where
    the agent reaches for the tool are exactly the ones the funnel did not
    claim -- so the address must be recorded before any early return, or a
    tool-started run announces to the previous conversation (or the cold
    default)."""
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec())
    dag = FakeDagTool()
    rt = PlaybookRuntime(
        provider=GateProvider({"match": None, "confidence": "low", "reason": ""}),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=dag),
    )

    assert (
        await rt.consider("nothing to do with feedback", channel="telegram", chat_id="42", session_key="telegram:42")
        is None
    )

    await rt.run_named("weekly-feedback", {"week_of": "2026-08-10"})
    assert dag.context == ("telegram", "42", "telegram:42")


# --- The spec-level confirm gate: a passive match asks before dispatching.


def _funnel(tmp_path, ask, confirm=True):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec(confirm=confirm))
    dag = FakeDagTool()
    rt = PlaybookRuntime(
        provider=GateProvider(
            {
                "match": "weekly-feedback",
                "confidence": "high",
                "reason": "clearly wants the weekly report",
                "params": {"week_of": "2026-08-11"},
            }
        ),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=dag),
        ask=ask,
    )
    return rt, dag


class Asker:
    def __init__(self, answer):
        self._answer = answer
        self.asked = []

    async def __call__(self, prompt, choices, cid):
        self.asked.append((prompt, tuple(choices or ()), cid))
        return self._answer


async def test_confirm_asks_and_consent_dispatches(tmp_path: Path):
    ask = Asker("Run it")
    rt, dag = _funnel(tmp_path, ask)

    plan = await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7")

    assert plan is not None and plan.kind == "dag"
    assert len(ask.asked) == 1
    prompt, choices, cid = ask.asked[0]
    assert "weekly-feedback" in prompt and "week_of=2026-08-11" in prompt
    assert choices == ("Run it", "Not now") and cid == "tg:7"


async def test_confirm_decline_and_timeout_fall_through(tmp_path: Path):
    for i, answer in enumerate(("Not now", "")):
        ask = Asker(answer)
        rt, dag = _funnel(tmp_path / str(i), ask)
        plan = await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7")
        assert plan is None, answer
        assert dag.calls == [], answer


async def test_confirm_false_skips_the_ask(tmp_path: Path):
    ask = Asker("irrelevant")
    rt, dag = _funnel(tmp_path, ask, confirm=False)
    plan = await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7")
    assert plan is not None and plan.kind == "dag"
    assert ask.asked == []


async def test_no_ask_channel_keeps_the_pre_confirm_behaviour(tmp_path: Path):
    rt, dag = _funnel(tmp_path, ask=None)
    plan = await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7")
    assert plan is not None and plan.kind == "dag"


async def test_torn_gate_offers_the_contenders_and_runs_the_pick(tmp_path: Path):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(
        _dag_spec(
            name="weekly-feedback",
            params={},
            nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})],
            confirm=True,
        )
    )
    store.save(
        _dag_spec(
            name="monthly-report",
            triggers=Triggers(keywords=["monthly report"]),
            params={},
            nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})],
        )
    )
    ask = Asker("monthly-report")
    dag = FakeDagTool()
    rt = PlaybookRuntime(
        provider=GateProvider(
            {
                "match": None,
                "confidence": "low",
                "reason": "too close to call",
                "contenders": ["weekly-feedback", "monthly-report", "ghost-entry"],
            }
        ),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=dag),
        ask=ask,
    )

    plan = await rt.consider("pull the user feedback report", channel="tg", chat_id="7", session_key="tg:7")

    assert plan is not None and plan.kind == "dag"
    # The unknown contender was filtered out of the offer; picking is the
    # consent, so no second confirm happened (one ask total).
    assert len(ask.asked) == 1
    assert ask.asked[0][1] == ("weekly-feedback", "monthly-report", "None of these")


async def test_torn_gate_none_of_these_falls_through(tmp_path: Path):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec(params={}, nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})]))
    store.save(
        _dag_spec(
            name="monthly-report",
            triggers=Triggers(keywords=["monthly report"]),
            params={},
            nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})],
        )
    )
    ask = Asker("None of these")
    dag = FakeDagTool()
    rt = PlaybookRuntime(
        provider=GateProvider(
            {"match": None, "confidence": "low", "reason": "torn", "contenders": ["weekly-feedback", "monthly-report"]}
        ),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=dag),
        ask=ask,
    )
    plan = await rt.consider("pull the user feedback report", channel="tg", chat_id="7", session_key="tg:7")
    assert plan is None
    assert len(ask.asked) == 1  # the offer really happened; None is not a vacuous pass
    assert dag.calls == []


async def test_a_declined_confirm_blocks_run_named_for_the_turn(tmp_path: Path):
    """The fall-through turn is exactly where run_playbook is live, and its
    instructions cannot distinguish a trigger miss from the user's "Not now"
    -- so the refusal must outlive consider() or the model re-runs what the
    user just declined."""
    ask = Asker("Not now")
    rt, dag = _funnel(tmp_path, ask)

    assert await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7") is None

    plan = await rt.run_named("weekly-feedback", {"week_of": "2026-08-11"})
    assert plan is not None and plan.kind == "questions"
    assert "declined" in plan.reply
    assert dag.calls == []


async def test_the_next_user_message_resets_a_decline(tmp_path: Path):
    ask = Asker("Not now")
    rt, dag = _funnel(tmp_path, ask)
    await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7")

    # The next message from the same conversation misses L1 entirely -- and
    # that alone lifts the refusal, so "actually, run it" can work via the tool.
    assert await rt.consider("unrelated message", channel="tg", chat_id="7", session_key="tg:7") is None
    plan = await rt.run_named("weekly-feedback", {"week_of": "2026-08-11"})
    assert plan is not None and plan.kind == "dag"
    assert len(dag.calls) == 1


async def test_reset_declines_lifts_a_refusal_without_a_consider_call(tmp_path: Path):
    """The injected-message path: a mid-turn message merges into the running
    turn without a consider() call, so the loop resets through this entry --
    otherwise an explicit re-request inside the fall-through turn is refused."""
    ask = Asker("Not now")
    rt, dag = _funnel(tmp_path, ask)
    assert await rt.consider("sort out user feedback", channel="tg", chat_id="7", session_key="tg:7") is None

    rt.reset_declines("tg:7")

    plan = await rt.run_named("weekly-feedback", {"week_of": "2026-08-11"})
    assert plan is not None and plan.kind == "dag"
    assert len(dag.calls) == 1


async def test_none_of_these_blocks_the_offered_picks(tmp_path: Path):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    store.save(_dag_spec(params={}, nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})]))
    store.save(
        _dag_spec(
            name="monthly-report",
            triggers=Triggers(keywords=["monthly report"]),
            params={},
            nodes=[_dag_spec().nodes[0].model_copy(update={"prompt_template": "pull"})],
        )
    )
    dag = FakeDagTool()
    ask = Asker("None of these")
    rt = PlaybookRuntime(
        provider=GateProvider(
            {"match": None, "confidence": "low", "reason": "torn", "contenders": ["weekly-feedback", "monthly-report"]}
        ),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=dag),
        ask=ask,
    )

    assert await rt.consider("pull the user feedback report", channel="tg", chat_id="7", session_key="tg:7") is None
    assert len(ask.asked) == 1

    plan = await rt.run_named("monthly-report", {})
    assert plan is not None and plan.kind == "questions"
    assert dag.calls == []
