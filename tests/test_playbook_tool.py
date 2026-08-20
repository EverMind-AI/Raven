"""``load_playbook``: the one entry into the library, and what it refuses.

There is no passive funnel left for this to stay in step with -- it *is* the
path -- so what these cover instead is the contract that makes the entry safe to
give a model: the listing it chooses from, the enum that bounds the choice, and
``fills`` being able to complete a playbook but never to edit one.
"""

import pytest

from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.playbook import (
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

    async def execute(self, nodes, background=True, confirm=False, **_):
        self.calls.append({"nodes": nodes, "background": background, "confirm": confirm})
        return "started in the background, run_id=abc123"


def _spec(name="weekly-feedback", **over):
    base = dict(
        name=name,
        description="weekly user-feedback analysis",
        mode="dag",
        triggers=Triggers(keywords=["user feedback"]),
        params={"week_of": ParamSpec(required=True, description="which week should be analyzed?")},
        nodes=[NodeSpec(id="pull", subagent="data-raven", prompt_template="pull ${params.week_of}")],
    )
    base.update(over)
    return PlaybookSpec(**base)


def _runtime(tmp_path, specs, dag_tool=None):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    for spec in specs:
        store.save(spec)
    executor = PlaybookExecutor(
        dag_tool=dag_tool or FakeDagTool(),
    )
    return PlaybookRuntime(store=store, executor=executor)


@pytest.fixture
def runtime(tmp_path):
    return _runtime(tmp_path, [_spec()])


def test_description_lists_the_same_library_the_funnel_matches(runtime):
    """The tool advertises what it can actually run. Listing anything else would
    let the model name a playbook the funnel does not hold."""
    assert "weekly-feedback" in runtime.listing()[0]
    assert "weekly-feedback" in LoadPlaybookTool(runtime).description
    assert "weekly user-feedback analysis" in LoadPlaybookTool(runtime).description


def test_name_is_constrained_to_installed_playbooks(runtime):
    """An enum rather than a free string: a misspelled name is otherwise only
    caught after the call, and a model cannot be handed a way to name a
    playbook that does not exist."""
    schema = LoadPlaybookTool(runtime).parameters
    assert schema["properties"]["name"]["enum"] == ["weekly-feedback"]
    assert schema["required"] == ["name"]


async def test_running_by_name_dispatches_the_same_graph(tmp_path):
    """The point of the tool: it joins the passive path at the executor, so a
    named run produces the dispatch a matched run would."""
    dag = FakeDagTool()
    rt = _runtime(tmp_path, [_spec()], dag_tool=dag)
    out = await LoadPlaybookTool(rt).execute("weekly-feedback", {"week_of": "2026-08-11"})

    assert "weekly-feedback" in out
    assert [n["id"].rsplit("-", 1)[-1] for n in dag.calls[0]["nodes"]] == ["pull"]
    # Params are filled before dispatch, exactly as on the passive path.
    assert "2026-08-11" in dag.calls[0]["nodes"][0]["prompt_template"]


async def test_a_missing_param_comes_back_as_a_question(tmp_path):
    """The case the tool exists for: the user answers a question, the answer
    carries no trigger word, and the agent -- which has the conversation --
    calls this instead. Without the param it must ask rather than dispatch."""
    dag = FakeDagTool()
    rt = _runtime(tmp_path, [_spec()], dag_tool=dag)
    out = await LoadPlaybookTool(rt).execute("weekly-feedback", {})

    assert "which week should be analyzed?" in out
    assert dag.calls == []


async def test_an_unknown_name_names_the_alternatives(tmp_path):
    rt = _runtime(tmp_path, [_spec()])
    out = await LoadPlaybookTool(rt).execute("no-such-playbook", {})
    assert "no-such-playbook" in out
    assert "weekly-feedback" in out


# --- create_playbook: the conversational half of the creation story.


class FakeGenerator:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def generate(self, workflow, skills=None):
        from raven.playbook import GeneratedPlaybook, PlaybookGenerationError

        self.calls.append((workflow, skills))
        if self.fail:
            raise PlaybookGenerationError("no valid spec within the repair budget")
        return GeneratedPlaybook(spec=_spec(name="placeholder"), notes=["Assumption: weekly cadence"])


def _create_tool(tmp_path, generator=None):
    from raven.agent.tools.create_playbook import CreatePlaybookTool

    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")
    switched = []
    tool = CreatePlaybookTool(generator or FakeGenerator(), store, lambda n, d: switched.append((n, d)) or True)
    return tool, store, switched


async def test_create_lands_in_user_layer_disabled(tmp_path):
    tool, store, switched = _create_tool(tmp_path)

    out = await tool.execute("weekly-scan", "every monday pull feedback then summarize")

    assert store.origin_of("weekly-scan") == "user"
    assert switched == [("weekly-scan", True)]
    assert "disabled" in out
    assert "enable weekly-scan" in out
    # The generator's open questions reach the user through the reply too.
    assert "Assumption: weekly cadence" in out
    # The stored name is the tool argument, not whatever the model drafted.
    assert store.load("weekly-scan").name == "weekly-scan"


async def test_create_refuses_an_existing_name_without_generating(tmp_path):
    generator = FakeGenerator()
    tool, store, switched = _create_tool(tmp_path, generator)
    store.save(_spec(name="weekly-scan"))

    out = await tool.execute("weekly-scan", "whatever")

    assert out.startswith("Error")
    assert generator.calls == []
    assert switched == []


async def test_create_degrades_generation_failure_to_an_error_reply(tmp_path):
    tool, store, switched = _create_tool(tmp_path, FakeGenerator(fail=True))

    out = await tool.execute("weekly-scan", "whatever")

    assert out.startswith("Error")
    assert store.origin_of("weekly-scan") is None
    assert switched == []


async def test_create_refuses_a_traversal_name_before_generating(tmp_path):
    generator = FakeGenerator()
    tool, store, switched = _create_tool(tmp_path, generator)

    for bad in ("../escape", "/tmp/absolute", "UPPER"):
        out = await tool.execute(bad, "whatever")
        assert out.startswith("Error"), bad

    assert generator.calls == []
    assert switched == []
    assert list(tmp_path.rglob("playbook.md")) == []
