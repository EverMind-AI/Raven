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
        task_summary="pull this week's feedback and write the report",
        mode="dag",
        triggers=Triggers(keywords=["user feedback"]),
        params={"week_of": ParamSpec(required=True, description="which week should be analyzed?")},
        nodes=[
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull the week's feedback",
                prompt_template="pull ${params.week_of}",
            )
        ],
    )
    base.update(over)
    return PlaybookSpec(**base)


def _runtime(tmp_path, specs, dag_tool=None, disabled_source=None):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    for spec in specs:
        store.save(spec)
    executor = PlaybookExecutor(
        dag_tool=dag_tool or FakeDagTool(),
    )
    return PlaybookRuntime(store=store, executor=executor, disabled_source=disabled_source)


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


def _create_tool(tmp_path, generator=None, *, adopt_ok=True):
    from raven.agent.tools.create_playbook import CreatePlaybookTool

    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")
    switched: list[tuple[str, bool]] = []
    adopted: list[str] = []

    def _adopt(name: str) -> bool:
        adopted.append(name)
        return adopt_ok

    tool = CreatePlaybookTool(
        generator or FakeGenerator(),
        store,
        lambda n, d: switched.append((n, d)) or True,
        adopt=_adopt,
    )
    return tool, store, switched, adopted


def test_switching_one_off_takes_effect_without_a_restart(tmp_path):
    """The deny list is read, not remembered.

    It used to be copied into the runtime at construction, so `raven playbook
    disable x` changed a file that nothing would read again until the process
    restarted -- and the user had no way to know that the command they had just
    run was waiting on one.
    """
    deny: set[str] = set()
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")], disabled_source=lambda: frozenset(deny))

    assert runtime.names() == ["weekly-scan"]
    assert runtime.empty is False

    deny.add("weekly-scan")

    assert runtime.names() == []
    assert runtime.empty is True
    # Still loaded, so the user naming it outright on the CLI still resolves.
    assert runtime._specs["weekly-scan"].name == "weekly-scan"

    deny.clear()

    assert runtime.names() == ["weekly-scan"]


def test_a_playbook_written_mid_conversation_is_adopted_without_a_restart(tmp_path):
    """The library was read once at construction, so a playbook created in a
    conversation was invisible to the tool whose job is to load it until the
    next process. Loading the one file that changed rather than rescanning: the
    caller knows the name, and a rescan would parse every other file to learn
    nothing."""
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")
    store.save(_spec(name="monthly-scan"))

    assert runtime.names() == ["weekly-scan"]

    assert runtime.adopt("monthly-scan") is True

    assert runtime.names() == ["monthly-scan", "weekly-scan"]


def test_adopting_a_name_that_was_never_written_is_reported_not_raised(tmp_path):
    """The creating tool turns this into a reply that says which half happened;
    raising here would fail the turn that produced the file instead."""
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])

    assert runtime.adopt("never-written") is False
    assert runtime.names() == ["weekly-scan"]


async def test_create_lands_in_the_user_layer_and_is_usable_at_once(tmp_path):
    """It used to be written onto the deny list for review. That read as caution
    and behaved as a dead end: the only way off the list was a CLI command, and
    the runtime had read the list once at start, so even that did nothing until
    the next process. Disabling stays available; it is not the starting state."""
    tool, store, switched, adopted = _create_tool(tmp_path)

    out = await tool.execute("weekly-scan", "every monday pull feedback then summarize")

    assert store.origin_of("weekly-scan") == "user"
    assert switched == []
    assert adopted == ["weekly-scan"]
    assert "available now" in out
    assert "disabled" not in out
    # The generator's open questions reach the user through the reply too.
    assert "Assumption: weekly cadence" in out
    # The stored name is the tool argument, not whatever the model drafted.
    assert store.load("weekly-scan").name == "weekly-scan"


async def test_a_playbook_that_cannot_be_loaded_back_says_which_half_happened(tmp_path):
    """ "Created" on its own would send the caller to load a name that does not
    resolve, and the next thing it reads is the tool saying that name is not in
    the library -- two rounds to learn one thing."""
    tool, store, _switched, adopted = _create_tool(tmp_path, adopt_ok=False)

    out = await tool.execute("weekly-scan", "every monday pull feedback then summarize")

    assert adopted == ["weekly-scan"]
    assert "could not be loaded back" in out
    assert "not available in this conversation" in out
    # Written all the same: the file is the deliverable, and hiding that it
    # landed would leave a name that cannot be created again either.
    assert store.origin_of("weekly-scan") == "user"


async def test_create_refuses_an_existing_name_without_generating(tmp_path):
    generator = FakeGenerator()
    tool, store, switched, _adopted = _create_tool(tmp_path, generator)
    store.save(_spec(name="weekly-scan"))

    out = await tool.execute("weekly-scan", "whatever")

    assert out.startswith("Error")
    assert generator.calls == []
    assert switched == []


async def test_create_degrades_generation_failure_to_an_error_reply(tmp_path):
    tool, store, switched, _adopted = _create_tool(tmp_path, FakeGenerator(fail=True))

    out = await tool.execute("weekly-scan", "whatever")

    assert out.startswith("Error")
    assert store.origin_of("weekly-scan") is None
    assert switched == []


async def test_create_refuses_a_traversal_name_before_generating(tmp_path):
    generator = FakeGenerator()
    tool, store, switched, _adopted = _create_tool(tmp_path, generator)

    for bad in ("../escape", "/tmp/absolute", "UPPER"):
        out = await tool.execute(bad, "whatever")
        assert out.startswith("Error"), bad

    assert generator.calls == []
    assert switched == []
    assert list(tmp_path.rglob("playbook.md")) == []
