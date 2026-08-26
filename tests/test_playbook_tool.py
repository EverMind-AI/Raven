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


def _runtime(tmp_path, specs, dag_tool=None, disabled_source=None, disabled=()):
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_no_builtin")
    for spec in specs:
        store.save(spec)
    executor = PlaybookExecutor(
        dag_tool=dag_tool or FakeDagTool(),
    )
    return PlaybookRuntime(store=store, executor=executor, disabled=disabled, disabled_source=disabled_source)


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


def test_enable_takes_effect_without_a_restart_too(tmp_path):
    """The direction the first version of this fix left broken.

    A snapshot of `playbooks.disabled` was passed in beside the live source and
    the two were unioned, so every name disabled at start stayed disabled
    whatever the file later said: `disable` applied on the next call and `enable`
    waited for the next process. The previous tests missed it because their
    helper never supplied `disabled=` -- this one does, the way the loop used to.
    """
    live = {"weekly-scan"}
    runtime = _runtime(
        tmp_path,
        [_spec(name="weekly-scan")],
        disabled=["weekly-scan"],
        disabled_source=lambda: frozenset(live),
    )

    assert runtime.names() == []

    live.clear()

    assert runtime.names() == ["weekly-scan"], "a name disabled at start must still be enableable"


def test_a_source_that_cannot_be_read_keeps_the_list_the_loop_started_with(tmp_path):
    """The conservative direction: a torn config file must not start offering the
    model something the user switched off."""

    def _explode() -> frozenset[str]:
        raise OSError("config is mid-write")

    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")], disabled=["weekly-scan"], disabled_source=_explode)

    assert runtime.names() == []


def test_a_directory_written_by_anything_becomes_visible(tmp_path):
    """The library is the directory's answer, not the creating tool's.

    A hand-written directory, a `git pull`, an edit -- none of them can call
    `adopt`, and the library used to be read once at construction, so all of
    them waited for the next process. The read is cheap because it pays for what
    changed: digests over the files, and a parse only where the bytes moved.
    """
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")

    assert runtime.names() == ["weekly-scan"]

    store.save(_spec(name="hand-written"))

    assert runtime.names() == ["hand-written", "weekly-scan"]


def test_an_edit_to_a_playbook_already_loaded_is_picked_up(tmp_path):
    """The name set is unchanged, so a check that only listed names would miss
    this -- which is why the fingerprints are over content."""
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")
    assert "weekly user-feedback analysis" in runtime.listing()[0][1]

    edited = _spec(name="weekly-scan")
    store.save(edited.model_copy(update={"description": "Rewritten by hand."}), overwrite=True)

    assert "Rewritten by hand." in runtime.listing()[0][1]


def test_a_playbook_that_leaves_the_directory_leaves_the_library(tmp_path):
    """Otherwise the model keeps being offered a name whose file is gone, and
    finds out by calling it."""
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    assert runtime.names() == ["weekly-scan"]

    (tmp_path / "weekly-scan" / "playbook.md").unlink()

    assert runtime.names() == []


def test_a_playbook_naming_an_agent_that_does_not_exist_is_not_offered(tmp_path):
    """The gate this library actually has.

    Writing to the directory cannot be made hard -- `write_file` is a general
    capability and it is an ordinary directory -- so what a gate can do is make
    what lands there checked and visible. `store.load` does the schema and
    nothing else, so a graph naming an agent that is not on the table used to
    load fine and be offered, and the turn that called it found out.
    """
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    runtime._executor.set_roster({"Raven": "the generalist"})
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")

    # A name the store's own migration will not rewrite: `data-raven` in `_spec`
    # is a retired builtin and gets repointed at `Raven`, which the roster has.
    store.save(
        _spec(
            name="hand-written",
            nodes=[
                NodeSpec(
                    id="pull",
                    subagent="no-such-agent",
                    node_summary="pull the week's feedback",
                    prompt_template="pull ${params.week_of}",
                )
            ],
        )
    )

    assert "hand-written" not in runtime.names()
    # And the one that was already loaded is unaffected: this refuses a file, not
    # the library.
    assert runtime.names() == ["weekly-scan"]


def test_a_field_left_for_the_caller_is_not_read_as_a_defect(tmp_path):
    """The collision the gate hit on its first run, as a test.

    `validate_structure` answers two questions at once -- is this sound, is it
    complete -- and a field an author deliberately left blank makes it
    incomplete without making it unsound: `load_playbook` asks for those values
    by name and the run proceeds. Refusing them at the door would refuse the
    hand-written shape this refresh exists to make visible.
    """
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    runtime._executor.set_roster({"Raven": "the generalist"})
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")

    store.save(
        _spec(
            name="left-blank",
            nodes=[NodeSpec(id="pull", subagent="Raven", node_summary="pull it", prompt_template="")],
        )
    )

    assert "left-blank" in runtime.names()
    assert "left for you" in dict(runtime.listing())["left-blank"]


def test_a_library_nobody_touched_is_not_reparsed(tmp_path):
    """The performance invariant, pinned because nothing else would notice it
    going away. The refresh runs before every read, so a version that parsed the
    library each time would be correct and quietly expensive -- and the cost is
    36x, measured: reading fifty files is about 1.4ms, parsing them 21ms."""
    runtime = _runtime(tmp_path, [_spec(name=f"pb-{i}") for i in range(5)])
    store = runtime._store
    calls: list[str] = []
    original = store.load

    def _counted(name: str):
        calls.append(name)
        return original(name)

    store.load = _counted  # type: ignore[method-assign]

    runtime.names()
    runtime.listing()
    runtime.names()

    assert calls == [], f"an untouched library was parsed: {calls}"

    (tmp_path / "pb-2" / "playbook.md").write_text(
        (tmp_path / "pb-2" / "playbook.md").read_text(encoding="utf-8").replace("analysis", "analysis (edited)"),
        encoding="utf-8",
    )
    runtime.names()

    assert calls == ["pb-2"], f"only the changed file should be parsed, got {calls}"


def test_adopt_does_not_wait_for_the_next_read(tmp_path):
    """What `adopt` is still for. The refresh above happens on the next read;
    the tool that just wrote a file hands it over directly, so the reply it is
    about to send can say the playbook is usable without that being a guess."""
    runtime = _runtime(tmp_path, [_spec(name="weekly-scan")])
    store = PlaybookStore(tmp_path, builtin_root=tmp_path / "_builtin")
    store.save(_spec(name="monthly-scan"))

    assert runtime.adopt("monthly-scan") is True

    # Read off the loaded library rather than through `names()`, which would
    # refresh and pass whether or not `adopt` did anything.
    assert "monthly-scan" in runtime._specs


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
