"""Execution side: what loading a playbook produces, and what it refuses.

"Funnel" is gone from this file along with the thing it named. The runtime is a
library plus one loader now, so what is covered is the loader's contract: params
and blanks reported together and before anything runs, ``fills`` able to complete
a playbook but never to edit one, the gap loop bounded, and the two modes going to
the party that can act on them.
"""

import json
from pathlib import Path

from raven.playbook import (
    MAX_GAP_ROUNDS,
    NodeSpec,
    ParamSpec,
    PlaybookExecutor,
    PlaybookRuntime,
    PlaybookSpec,
    PlaybookStore,
    RouterSizes,
    Triggers,
)
from raven.providers.base import ErrorClassification, LLMResponse


class FakeDagTool:
    def __init__(self):
        self.calls = []
        self.context = None

    def set_context(self, channel, chat_id, session_key=None):
        self.context = (channel, chat_id, session_key)

    async def execute(self, nodes, task_summary="", background=True, confirm=False, **_):
        self.calls.append({"nodes": nodes, "task_summary": task_summary, "background": background, "confirm": confirm})
        return "DAG run wf-1 started in the background (%d nodes)." % len(nodes)


def _dag_spec(**over):
    base = dict(
        name="weekly-feedback",
        description="weekly user-feedback analysis",
        task_summary="pull this week's feedback and write the report",
        mode="dag",
        triggers=Triggers(keywords=["user feedback"]),
        params={
            "week_of": ParamSpec(type="string", required=True, description="which week should be analyzed?"),
            "audience": ParamSpec(default="PM", description="who reads the report?"),
        },
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull the feedback for the week",
                prompt_template="pull the feedback for ${params.week_of}",
                skills=["sql-queries"],
            ),
            NodeSpec(
                id="report",
                subagent="content-raven",
                node_summary="write the weekly report from the pulled feedback",
                prompt_template="write the weekly report for ${params.audience} from {{ pull.output_path }}",
                depends_on=["pull"],
                mcps=["slack"],
            ),
        ],
    )
    base.update(over)
    return PlaybookSpec(**base)


# ---------------------------------------------------------------- executor


async def test_a_missing_param_is_a_gap_and_dispatches_nothing():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    plan = await ex.execute(_dag_spec(), params={})

    # Its own outcome, not "questions": the caller can fix a gap by calling
    # again, which is a different instruction from "this cannot proceed".
    assert plan.kind == "gaps"
    assert "which week should be analyzed?" in plan.reply
    assert tool.calls == []


async def test_the_gap_reply_names_the_argument_to_pass():
    """The reader is the caller, and it used to be the user.

    The old wording ended "include a trigger word in your reply, e.g. ..." because
    the answer had to re-enter through a stateless keyword funnel to be seen at
    all. The caller holds the conversation now and simply calls again, so what it
    needs is the argument path -- a caller that has to guess which argument a
    complaint refers to will guess wrong and spend another round.
    """
    ex = PlaybookExecutor(dag_tool=FakeDagTool())
    plan = await ex.execute(_dag_spec(), params={})

    assert "params.week_of" in plan.reply
    assert "load_playbook again" in plan.reply
    assert "trigger word" not in plan.reply


async def test_a_blank_node_field_is_a_gap_and_fills_closes_it():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(id="draft", subagent="content-raven", node_summary="draft the weekly report", prompt_template=""),
        ]
    )

    plan = await ex.execute(spec, params={"week_of": "w"})
    assert plan.kind == "gaps"
    assert "fills['draft']['promptTemplate']" in plan.reply.replace('"', "'")
    assert tool.calls == []

    plan = await ex.execute(spec, params={"week_of": "w"}, fills={"draft": {"promptTemplate": "write it up"}})
    assert plan.kind == "dag"
    assert tool.calls[0]["nodes"][0]["prompt_template"] == "write it up"


async def test_a_blank_node_summary_is_a_gap_and_fills_closes_it():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(id="draft", subagent="content-raven", node_summary="", prompt_template="draft the report"),
        ]
    )

    plan = await ex.execute(spec, params={"week_of": "w"})
    assert plan.kind == "gaps"
    assert "fills['draft']['nodeSummary']" in plan.reply.replace('"', "'")
    assert tool.calls == []

    plan = await ex.execute(spec, params={"week_of": "w"}, fills={"draft": {"nodeSummary": "draft the weekly report"}})
    assert plan.kind == "dag"
    assert tool.calls[0]["nodes"][0]["node_summary"] == "draft the weekly report"


async def test_fills_cannot_touch_a_field_the_playbook_already_wrote():
    """The check that makes "the playbook's values are fixed" true.

    Without it ``fills`` is a general-purpose field editor: the caller could
    rewrite any prompt, repoint a node at another agent, or drop its skills, and
    the file in git would stop describing what ran.
    """
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)

    plan = await ex.execute(
        _dag_spec(),
        params={"week_of": "w"},
        fills={"pull": {"promptTemplate": "do something else entirely"}},
    )

    assert plan.kind == "questions"
    assert "already specifies" in plan.reply
    assert tool.calls == []  # refused before dispatch, not repaired around


async def test_fills_naming_an_unknown_node_is_refused_by_name():
    ex = PlaybookExecutor(dag_tool=FakeDagTool())
    plan = await ex.execute(_dag_spec(), params={"week_of": "w"}, fills={"ghost": {"promptTemplate": "x"}})

    assert plan.kind == "questions"
    assert "ghost" in plan.reply
    assert "pull" in plan.reply  # the ids it could have meant


async def test_an_empty_skills_list_is_written_not_blank():
    """``skills: []`` means "no skills at all" -- a written instruction.

    Treating it as an invitation would let a caller quietly widen what a step may
    reach, which is the opposite of what the author asked for.
    """
    ex = PlaybookExecutor(dag_tool=FakeDagTool())
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull the week's feedback with no extra skills",
                prompt_template="go",
                skills=[],
            )
        ]
    )

    plan = await ex.execute(spec, params={"week_of": "w"}, fills={"pull": {"skills": ["anything"]}})

    assert plan.kind == "questions"
    assert "already specifies" in plan.reply


async def test_executor_dag_hands_nodes_to_the_graph_tool_with_params_filled():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)

    plan = await ex.execute(_dag_spec(), params={"week_of": "2026-08-11"})

    assert plan.kind == "dag"
    # Node ids carry the run-unique namespace; the plain author ids survive as
    # the last dash segment.
    nodes = {n["id"].rsplit("-", 1)[-1]: n for n in tool.calls[0]["nodes"]}
    assert "2026-08-11" in nodes["pull"]["prompt_template"]
    assert "PM" in nodes["report"]["prompt_template"]  # default filled
    # Runtime refs stay runtime refs, retargeted to the namespaced id.
    assert "{{ %s.output_path }}" % nodes["pull"]["id"] in nodes["report"]["prompt_template"]
    assert nodes["report"]["depends_on"] == [nodes["pull"]["id"]]
    # The agent the author named reaches the graph tool as the node's own field:
    # no backend is built here and no synthetic per-node agent name is invented.
    assert nodes["pull"]["subagent"] == "data-raven"
    assert nodes["report"]["subagent"] == "content-raven"
    # skills is a playbook-only field now: the graph tool has no such parameter,
    # so the engine spends it into the step's prompt and passes neither it nor
    # mcps on. A node that declared none must not gain a sentence about them.
    assert "skills" not in nodes["pull"] and "mcps" not in nodes["report"]
    assert "sql-queries" in nodes["pull"]["prompt_template"]
    assert "skills" not in nodes["report"]["prompt_template"].lower()
    # mcps cannot be honoured at all, so it is reported rather than written into
    # the prompt as an instruction the sub-agent has no tool to follow.
    assert any("mcp" in n and "slack" in n for n in plan.notes)


async def test_executor_namespaces_the_instance_handle_per_run():
    """Two runs of one playbook must not pour their steps into one session."""
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="a",
                subagent="data-raven",
                node_summary="run the first step of the shared session",
                prompt_template="one",
                instance="shared",
            ),
            NodeSpec(
                id="b",
                subagent="data-raven",
                node_summary="run the second step of the shared session",
                prompt_template="two",
                depends_on=["a"],
                instance="shared",
            ),
        ]
    )

    await ex.execute(spec, params={"week_of": "w"})
    await ex.execute(spec, params={"week_of": "w"})

    first = {n["instance"] for n in tool.calls[0]["nodes"]}
    second = {n["instance"] for n in tool.calls[1]["nodes"]}
    # One handle within a run (that is what the author asked for), a different
    # one between runs.
    assert len(first) == 1 and len(second) == 1
    assert first != second


async def test_executor_asks_the_graph_tool_to_confirm_unless_the_caller_already_did():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)

    await ex.execute(_dag_spec(), params={"week_of": "w"})
    assert tool.calls[0]["confirm"] is True

    await ex.execute(_dag_spec(), params={"week_of": "w"}, confirmed=True)
    assert tool.calls[1]["confirm"] is False


async def test_executor_passes_the_playbooks_own_task_summary_to_the_graph_tool():
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec()

    await ex.execute(spec, params={"week_of": "w"})

    assert tool.calls[0]["task_summary"] == spec.task_summary
    assert tool.calls[0]["task_summary"] != spec.description


class ComposeProvider:
    """Scripted graph-composition responses."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls.append(messages)

        payload = self._payloads.pop(0)
        if isinstance(payload, LLMResponse):
            return payload

        class _TC:
            def __init__(self, args):
                self.name = "emit_graph"
                self.arguments = args

        class _R:
            def __init__(self, args):
                self.has_tool_calls = args is not None
                self.tool_calls = [_TC(args)] if args is not None else []

        return _R(payload)


def _prompt_spec():
    return PlaybookSpec(
        name="due-diligence",
        description="run due diligence on a target company",
        task_summary="scan the target company and summarize what was found",
        mode="prompt",
        triggers=Triggers(keywords=["due diligence"]),
        params={"target": ParamSpec(required=True, description="which company is the target?")},
        prompts="compose a two-layer graph for ${params.target}: a breadth scan, then a summary.",
    )


GOOD_GRAPH = {
    "nodes": [
        {
            "id": "scan",
            "subagent": "research-raven",
            "nodeSummary": "breadth-scan the target company",
            "promptTemplate": "breadth-scan AcmeAI",
        },
        {
            "id": "sum",
            "subagent": "content-raven",
            "nodeSummary": "summarize the scan findings",
            "promptTemplate": "summarize {{ scan.output }}",
            "dependsOn": ["scan"],
        },
    ]
}


async def test_prompt_mode_returns_the_guidance_to_a_caller_that_can_compose():
    """In a conversation the caller is a model, so composing for it is worse.

    A private composition call sees the guidance text and a cached roster and
    nothing else; the caller has the whole conversation, and a graph it builds
    wrong comes back as an ordinary tool error it can fix. Flexibility is the only
    thing prompt mode offers over dag mode -- pinning the graph at load time would
    make it a worse dag mode.
    """
    tool = FakeDagTool()
    provider = ComposeProvider([json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(dag_tool=tool, provider=provider)

    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "guidance"
    assert "AcmeAI" in plan.reply  # params substituted into the guidance
    assert "run_subagent_dag" in plan.reply  # and it says where to take it
    assert provider.calls == []  # no composition call spent
    assert tool.calls == []  # and nothing dispatched behind the caller's back


async def test_prompt_mode_composes_when_there_is_no_caller_to_hand_it_to():
    """The CLI's shape. Without it, ``raven playbook run`` could not run a
    prompt-mode playbook at all -- there is no model in the room to compose."""
    tool = FakeDagTool()
    provider = ComposeProvider([json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(dag_tool=tool, provider=provider, compose_prompt_mode=True)
    ex.set_roster({"research-raven": "research", "content-raven": "writing"})

    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "dag"
    assert len(tool.calls) == 1
    assert "AcmeAI" in provider.calls[0][0]["content"]


async def test_prompt_mode_bad_graph_gets_one_repair_then_degrades():
    bad = {"nodes": [{"id": "a", "subagent": "research-raven", "promptTemplate": "use {{ ghost.output }}"}]}
    provider = ComposeProvider([json.dumps(bad), json.dumps(bad)])
    ex = PlaybookExecutor(dag_tool=FakeDagTool(), provider=provider, compose_prompt_mode=True)
    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})
    assert plan.kind == "questions"
    assert "Graph assembly" in plan.reply
    assert len(provider.calls) == 2  # one repair round happened


async def test_prompt_mode_provider_error_does_not_enter_content_repair():
    classification = ErrorClassification(
        "upstream_transport_failure",
        retryable=True,
        should_fallback=True,
    )
    response = LLMResponse(
        content="upstream did not process the request",
        finish_reason="error",
        error_classification=classification,
    )
    provider = ComposeProvider([response, json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(dag_tool=FakeDagTool(), provider=provider, compose_prompt_mode=True)
    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "questions"
    assert "upstream_transport_failure" in plan.reply
    assert len(provider.calls) == 1


async def test_prompt_mode_missing_tool_does_not_enter_content_repair():
    provider = ComposeProvider([None, json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(dag_tool=FakeDagTool(), provider=provider, compose_prompt_mode=True)
    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "questions"
    assert "required_tool_missing" in plan.reply
    assert len(provider.calls) == 1


# ---------------------------------------------------------------- runtime


def _library(tmp_path: Path, specs) -> PlaybookStore:
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    for spec in specs:
        store.save(spec)
    return store


def _runtime(tmp_path: Path, specs, *, dag_tool=None, disabled=(), router=None) -> PlaybookRuntime:
    executor = PlaybookExecutor(dag_tool=dag_tool or FakeDagTool())
    return PlaybookRuntime(
        store=_library(tmp_path, specs),
        executor=executor,
        disabled=disabled,
        router=router,
    )


async def test_load_dispatches_a_complete_playbook_in_one_call(tmp_path: Path):
    """No gate call, no confirm round trip, no second tool call.

    A playbook whose graph is fully specified is the common case, and the caller
    should never see its node contents -- it decided from the description and the
    engine did the rest.
    """
    tool = FakeDagTool()
    rt = _runtime(tmp_path, [_dag_spec()], dag_tool=tool)

    plan = await rt.load("weekly-feedback", {"week_of": "2026-08-11"})

    assert plan.kind == "dag"
    assert len(tool.calls) == 1


async def test_an_unknown_name_is_none_so_the_tool_can_name_the_alternatives(tmp_path: Path):
    rt = _runtime(tmp_path, [_dag_spec()])
    assert await rt.load("no-such-thing", {}) is None


async def test_the_gap_loop_is_bounded(tmp_path: Path):
    """ "Cannot fill it -> ask again -> still cannot" is a loop a caller can spend a
    whole turn in, one tool call per pass, with no progress. Past the bound it is
    told to stop rather than asked again."""
    tool = FakeDagTool()
    rt = _runtime(tmp_path, [_dag_spec()], dag_tool=tool)
    rt.set_context(channel="web", chat_id="c", session_key="web:c")

    for _ in range(MAX_GAP_ROUNDS):
        plan = await rt.load("weekly-feedback", {})
        assert plan.kind == "gaps"

    final = await rt.load("weekly-feedback", {})
    assert final.kind == "questions"
    assert "do not call this again" in final.reply
    assert tool.calls == []


async def test_filling_the_gap_restores_the_budget(tmp_path: Path):
    """The bound is per stuck attempt, not per playbook for the session: a second,
    genuinely new run of the same playbook must not start already exhausted."""
    rt = _runtime(tmp_path, [_dag_spec()])
    rt.set_context(channel="web", chat_id="c", session_key="web:c")

    assert (await rt.load("weekly-feedback", {})).kind == "gaps"
    assert (await rt.load("weekly-feedback", {"week_of": "w"})).kind == "dag"
    # Budget reset by the dispatch, so a later stuck call gets its full allowance.
    for _ in range(MAX_GAP_ROUNDS):
        assert (await rt.load("weekly-feedback", {})).kind == "gaps"


async def test_a_disabled_playbook_is_invisible_to_the_model_and_runnable_by_hand(tmp_path: Path):
    """Disabling can only mean "not listed" now.

    There is no passive matcher left to mute, so the enforcement is that the model
    never sees it -- and cannot name it, since the enum comes from the same list.
    An explicit ``raven playbook run`` is the user's own hand and still resolves it.
    """
    rt = _runtime(tmp_path, [_dag_spec()], disabled=["weekly-feedback"])

    assert rt.names() == []
    assert rt.listing() == []
    assert rt.empty is True
    assert await rt.load("weekly-feedback", {"week_of": "w"}) is None
    assert (await rt.load("weekly-feedback", {"week_of": "w"}, allow_disabled=True)).kind == "dag"


async def test_the_listing_carries_what_a_caller_cannot_guess(tmp_path: Path):
    """Deleting the gate deleted the only reader of the param table.

    Without it in the description the caller can only guess key names, and a
    guessed key is dropped silently and comes back as the same question.
    """
    rt = _runtime(tmp_path, [_dag_spec()])
    detail = dict(rt.listing())["weekly-feedback"]

    assert "week_of" in detail
    assert "which week should be analyzed?" in detail
    assert "required" in detail
    assert "default='PM'" in detail


async def test_the_listing_names_the_fields_left_blank(tmp_path: Path):
    rt = _runtime(
        tmp_path,
        [
            _dag_spec(
                nodes=[
                    NodeSpec(
                        id="draft", subagent="content-raven", node_summary="draft the weekly report", prompt_template=""
                    )
                ]
            )
        ],
    )
    detail = dict(rt.listing())["weekly-feedback"]

    assert "left for you to fill" in detail
    assert "draft.prompt_template" in detail


async def test_a_library_bigger_than_the_router_window_is_narrowed_but_fully_nameable(tmp_path: Path):
    """The two costs are handled differently on purpose.

    A description plus a param table is the expensive part, so it is what gets
    narrowed. A name is a handful of tokens, and an enum missing one would turn a
    retrieval miss into "the model cannot reach it" even when the user just said
    its name out loud.
    """
    specs = [_dag_spec(name=f"book-{i}", triggers=Triggers(keywords=[f"topic{i}"])) for i in range(6)]
    rt = _runtime(tmp_path, specs, router=RouterSizes(top_k=2))

    assert len(rt.listing("please do topic4")) == 2
    assert dict(rt.listing("please do topic4"))  # ranked, and topic4 is in it
    assert "book-4" in dict(rt.listing("please do topic4"))
    assert len(rt.names()) == 6


async def test_prompt_mode_reaches_the_caller_as_guidance(tmp_path: Path):
    tool = FakeDagTool()
    rt = _runtime(tmp_path, [_prompt_spec()], dag_tool=tool)

    plan = await rt.load("due-diligence", {"target": "AcmeAI"})

    assert plan.kind == "guidance"
    assert tool.calls == []


async def test_context_is_recorded_for_the_dispatch_address(tmp_path: Path):
    """A tool call arrives without an address, so the last one the loop set is
    what the dispatch announces into."""
    tool = FakeDagTool()
    rt = _runtime(tmp_path, [_dag_spec()], dag_tool=tool)

    rt.set_context(channel="web", chat_id="room-7", session_key="web:room-7")
    await rt.load("weekly-feedback", {"week_of": "w"})

    assert tool.context == ("web", "room-7", "web:room-7")


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
    executor = PlaybookExecutor(dag_tool=tool)

    first = await executor.execute(_dag_spec(), {"week_of": "2026-08-10"})
    second = await executor.execute(_dag_spec(), {"week_of": "2026-08-17"})

    assert first.kind == "dag"
    assert second.kind == "dag"
    assert len(tool.calls) == 2


async def test_node_ids_and_references_are_rewritten_in_step():
    tool = FakeDagTool()
    executor = PlaybookExecutor(dag_tool=tool)

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
    # Params were already compiled in; the rewrite must not disturb them. The
    # skills sentence the engine folds in sits after the author's prompt, so the
    # prompt still starts with exactly what the playbook wrote.
    assert pull["prompt_template"].startswith("pull the feedback for 2026-08-10")


async def test_rewrite_leaves_foreign_references_alone():
    tool = FakeDagTool()
    executor = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="read the summary file and an external reference",
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


async def test_an_empty_skills_list_is_the_same_input_as_no_list():
    """``skills: []`` adds nothing to the prompt, and is not worth a note either.

    The field points at skills that may help; an empty list points at none, which
    is what leaving the field out already says. Nothing was declared and dropped
    -- there is no instruction here to lose -- so the step's prompt is the
    author's prompt, unchanged and unannotated.
    """
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull the week's feedback with no extra skills",
                prompt_template="go",
                skills=[],
            )
        ]
    )

    plan = await ex.execute(spec, params={"week_of": "w"})

    assert plan.kind == "dag"
    assert tool.calls[0]["nodes"][0]["prompt_template"] == "go"
    assert not [n for n in plan.notes if "skill" in n.lower()]


async def test_mcps_are_reported_rather_than_written_into_the_prompt():
    """A step cannot be told to use a tool it will not have.

    The sub-agent registry has no mcp path at all, so a sentence naming a server
    would be an instruction with nothing behind it -- worse than the note,
    because the sub-agent would either refuse or invent. Reported so a wrong
    result stays attributable.
    """
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)
    spec = _dag_spec(
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull data that needs a github mcp",
                prompt_template="go",
                mcps=["github"],
            )
        ]
    )

    plan = await ex.execute(spec, params={"week_of": "w"})

    assert plan.kind == "dag"
    node = tool.calls[0]["nodes"][0]
    assert "mcps" not in node
    assert "github" not in node["prompt_template"]
    assert any("github" in n and "not implemented" in n for n in plan.notes)


async def test_the_dispatch_receipt_names_the_run() -> None:
    """A client restoring the card from history has no events, only this line.

    It recovers the run from the result text, in the graph tool's own shape, so a
    receipt naming only the playbook left a reopened conversation showing a
    dispatch it could not connect to any run: no node states, no way back to the
    graph. The model gets a handle on the run out of the same line.
    """
    tool = FakeDagTool()
    ex = PlaybookExecutor(dag_tool=tool)

    plan = await ex.execute(_dag_spec(), params={"week_of": "w"})

    assert plan.kind == "dag"
    assert plan.reply.startswith("DAG "), plan.reply
    assert "weekly-feedback" in plan.reply and "2 steps" in plan.reply
