"""Execution side: store round-trip, executor plans (both modes), runtime funnel."""

import json
from pathlib import Path

from raven.memory_engine.playbook import (
    NodeSpec,
    ParamSpec,
    PlaybookExecutor,
    PlaybookRuntime,
    PlaybookSpec,
    PlaybookStore,
    Triggers,
)


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
        description="每周用户反馈分析",
        mode="dag",
        status="ready",
        triggers=Triggers(keywords=["用户反馈"]),
        params={
            "week_of": ParamSpec(type="string", required=True, description="分析哪一周？"),
            "audience": ParamSpec(default="PM", description="读者"),
        },
        nodes=[
            NodeSpec(
                id="pull", agent="data-raven", prompt_template="拉取 ${params.week_of} 的反馈", skills=["sql-queries"]
            ),
            NodeSpec(
                id="report",
                agent="content-raven",
                prompt_template="给 ${params.audience} 写周报，基于 {{ pull.output_path }}",
                depends_on=["pull"],
                mcps=["slack"],
                instance="w1",
            ),
        ],
    )
    base.update(over)
    return PlaybookSpec(**base)


# ---------------------------------------------------------------- store


def test_store_round_trips_playbook_md_and_sidecar(tmp_path: Path):
    store = PlaybookStore(tmp_path)
    spec = _dag_spec(status="draft")
    spec = spec.model_copy(
        update={"provenance": spec.provenance.model_copy(update={"specified_by_user": ["slack 数据源"]})}
    )
    path = store.save(spec)

    assert path.name == "playbook.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: weekly-feedback\n")
    assert "```yaml playbook-spec" in text
    assert "promptTemplate" in text  # camelCase on disk
    block = text.split("```yaml playbook-spec")[1]
    assert "provenance" not in block and "status" not in block

    loaded = store.load("weekly-feedback")
    assert loaded.nodes[1].depends_on == ["pull"]
    assert loaded.status == "draft"  # from the sidecar
    assert loaded.provenance.specified_by_user == ["slack 数据源"]


def test_handwritten_playbook_without_sidecar_loads_as_ready(tmp_path: Path):
    store = PlaybookStore(tmp_path)
    store.save(_dag_spec())
    (tmp_path / "weekly-feedback" / ".provenance.json").unlink()
    assert store.load("weekly-feedback").status == "ready"


# ---------------------------------------------------------------- executor


async def test_executor_asks_with_the_param_description():
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool())
    plan = await ex.execute(_dag_spec(), params={})
    assert plan.kind == "questions"
    assert "分析哪一周？" in plan.reply


async def test_executor_dag_builds_per_node_backends_and_fills_params():
    built = []
    tool = FakeDagTool()
    ex = PlaybookExecutor(backend_factory=_factory_recorder(built), dag_tool=tool)

    plan = await ex.execute(_dag_spec(), params={"week_of": "2026-08-11"})

    assert plan.kind == "dag"
    by_name = {b.name: b for b in built}
    assert by_name["pb-pull"].skills_allow == ["sql-queries"]
    assert by_name["pb-report"].skills_allow is None
    nodes = {n["id"]: n for n in tool.calls[0]["nodes"]}
    assert "2026-08-11" in nodes["pull"]["prompt_template"]
    assert "PM" in nodes["report"]["prompt_template"]  # default filled
    assert "{{ pull.output_path }}" in nodes["report"]["prompt_template"]  # runtime refs untouched
    assert nodes["report"]["depends_on"] == ["pull"]
    assert any("mcps" in n for n in plan.notes)
    assert any("instance" in n for n in plan.notes)


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
        description="对标的公司做尽调",
        mode="prompt",
        status="ready",
        triggers=Triggers(keywords=["尽调"]),
        params={"target": ParamSpec(required=True, description="标的公司名称")},
        prompts="为 ${params.target} 组一张两层的图，第一层广度扫描，第二层汇总。",
    )


GOOD_GRAPH = {
    "nodes": [
        {"id": "scan", "agent": "research-raven", "promptTemplate": "广度扫描 AcmeAI"},
        {"id": "sum", "agent": "content-raven", "promptTemplate": "汇总 {{ scan.output }}", "dependsOn": ["scan"]},
    ]
}


async def test_prompt_mode_composes_a_graph_then_dispatches():
    tool = FakeDagTool()
    provider = ComposeProvider([json.dumps(GOOD_GRAPH)])
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool, provider=provider)
    ex.set_roster({"research-raven": "调研", "content-raven": "成文"})

    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})

    assert plan.kind == "dag"
    assert len(tool.calls) == 1
    assert "AcmeAI" in provider.calls[0][0]["content"]  # params substituted into the compose prompt


async def test_prompt_mode_bad_graph_gets_one_repair_then_degrades():
    bad = {"nodes": [{"id": "a", "agent": "research-raven", "promptTemplate": "用 {{ ghost.output }}"}]}
    provider = ComposeProvider([json.dumps(bad), json.dumps(bad)])
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool(), provider=provider)
    plan = await ex.execute(_prompt_spec(), params={"target": "AcmeAI"})
    assert plan.kind == "questions"
    assert "组图失败" in plan.reply
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
    store = PlaybookStore(tmp_path)
    store.save(_dag_spec())
    tool = FakeDagTool()
    ex = PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=tool)
    rt = PlaybookRuntime(
        provider=GateProvider(
            {
                "match": "weekly-feedback",
                "confidence": "high",
                "reason": "明确要周报",
                "params": {"week_of": "2026-08-11"},
            }
        ),
        store=store,
        executor=ex,
    )

    plan = await rt.consider("这周的用户反馈整理一下", channel="cli", chat_id="direct", session_key="cli:direct")

    assert plan is not None and plan.kind == "dag"
    assert tool.context == ("cli", "direct", "cli:direct")


async def test_runtime_passes_through_on_no_hit_and_low_confidence(tmp_path: Path):
    store = PlaybookStore(tmp_path)
    store.save(_dag_spec())
    provider = GateProvider({"match": "weekly-feedback", "confidence": "low", "reason": "只是聊到"})
    rt = PlaybookRuntime(
        provider=provider,
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool()),
    )

    assert await rt.consider("中午吃什么") is None
    assert provider.calls == 0
    assert await rt.consider("用户反馈这个按钮点不动") is None


async def test_runtime_skips_drafts_by_default(tmp_path: Path):
    store = PlaybookStore(tmp_path)
    store.save(_dag_spec())
    store.save(_dag_spec(name="draft-one", status="draft"))
    rt = PlaybookRuntime(
        provider=GateProvider({"match": None, "confidence": "low", "reason": ""}),
        store=store,
        executor=PlaybookExecutor(backend_factory=_factory_recorder([]), dag_tool=FakeDagTool()),
    )
    assert rt._specs.keys() == {"weekly-feedback"}
