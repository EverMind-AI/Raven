"""A playbook is reached through a tool, never by intercepting the turn.

This file used to assert the opposite: a funnel ran ahead of the turn, and on a
hit it returned the playbook's reply *instead of* running the model, recording
both sides of the exchange itself because it had skipped ``_save_turn``. What is
guarded now is that no such branch exists -- every message reaches the model, and
what the loop does per turn is give the tool the material it needs to describe
itself.
"""

from __future__ import annotations

from raven.agent.loop import AgentLoop
from raven.contracts.tool import Tool
from raven.providers.base import LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="normal turn ran")

    def get_default_model(self) -> str:
        return "fake/default"


class _StubPlaybooks:
    empty = False

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def listing(self, message: str = "") -> list[tuple[str, str]]:
        return [("weekly-feedback", "weekly user-feedback analysis")]

    def names(self) -> list[str]:
        return ["weekly-feedback"]

    def set_context(self, **kwargs) -> None:
        self.contexts.append(kwargs)


def _stub_edges(loop: AgentLoop) -> None:
    async def _noop() -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop


def _req(text: str, *, conversation: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="web", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation=conversation,
    )


async def test_a_message_that_names_a_playbook_still_reaches_the_model(tmp_path):
    """The decision belongs to the turn, so the turn has to happen.

    A message matching a playbook's vocabulary used to be answered without the
    model running at all -- one gate call decided, on this message alone, with no
    conversation history. Now the model sees the message and the playbook in the
    same breath and picks.
    """
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop._playbooks = _StubPlaybooks()

    reply, _ = await loop._process_message(_req("sort out this week's user feedback", conversation="web:abc"))

    assert reply == "normal turn ran"
    assert provider.calls == 1
    # One user entry: nothing records the exchange twice now that no branch
    # returns ahead of _save_turn.
    history = loop.sessions.get_or_create("web:abc").messages
    assert sum(1 for m in history if m.get("role") == "user") == 1


async def test_the_turn_message_reaches_the_tool_so_its_listing_can_be_ranked(tmp_path):
    """The description is rendered per turn and narrowed to what fits the request.

    Without this hand-off the tool would rank against nothing, and a library too
    large to list whole would show an arbitrary slice of itself on every turn.
    """
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop._playbooks = _StubPlaybooks()

    seen: list[str] = []

    class _Tool(Tool):
        @property
        def name(self) -> str:
            return "load_playbook"

        @property
        def description(self) -> str:
            return "stub"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs) -> str:
            return "not called"

        def set_turn_message(self, message: str) -> None:
            seen.append(message)

    loop.tools.register(_Tool())

    await loop._process_message(_req("sort out this week's user feedback", conversation="web:abc"))

    assert seen == ["sort out this week's user feedback"]


async def test_every_origin_records_the_playbook_address(tmp_path):
    """A cron turn can call load_playbook too, and its graph announces somewhere.

    The address comes from the per-turn tool-context pass rather than from a
    user-only branch, so a cron-started run reports into its own conversation
    instead of the last human one (or the cli:direct default on a cold process).
    """
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    stub = _StubPlaybooks()
    loop._playbooks = stub

    cron_req = TurnRequest(
        origin=Origin.CRON,
        source=Source(channel="slack", chat_id="#team", sender_id="cron", chat_type=ChatType.DM),
        text="run the weekly briefing playbook",
        conversation="cron:job-1",
    )
    await loop._process_message(cron_req, origin=Origin.CRON)

    assert {"channel": "slack", "chat_id": "#team", "session_key": "cron:job-1"} in stub.contexts


def test_a_playbook_config_that_is_on_registers_both_entries_even_empty(tmp_path) -> None:
    """The one path production takes: construct the loop *with* a playbook config.

    Every other test in the suite assembles the playbook runtime directly, which
    is why an ordering bug in ``AgentLoop.__init__`` survived a full green run:
    the build read ``self._mcp_servers`` several assignments before that field
    existed, raised ``AttributeError`` into the guard that turns a build failure
    into "feature disabled", and left ``playbooks.enabled: true`` doing nothing
    but writing one warning line. Asserting on the registry is what makes that
    reachable -- a runtime that fails to build registers no tools.

    Both tools register whenever the feature is on, and the empty library is the
    case that decides it. The loader used to wait for content, on the grounds
    that a tool answering nothing but "unknown" should not be offered -- but
    ``create_playbook`` writes the first one *into this conversation*, and the
    tool that loads it did not exist until the next process. Its description
    says there is nothing installed when there is nothing installed, which costs
    a sentence and keeps the pair reachable together.
    """
    from raven.config.schema import PlaybookConfig

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )

    assert loop._playbooks is not None, "enabled: true must leave a runtime behind"
    assert loop.tools.has("create_playbook")
    assert loop.tools.has("load_playbook")
    loader = loop.tools.get("load_playbook")
    assert "No playbooks are installed" in loader.description
    # The schema an empty library produces has to be one a provider will take and
    # a call will survive. `"enum": None` is neither: it is not a JSON Schema, and
    # `Tool._validate` tests `val not in schema["enum"]` on the key being present,
    # so every call raised `argument of type 'NoneType' is not iterable` before it
    # ran. Unreachable while the loader was withheld over an empty library; the
    # fresh-install state once it is not.
    name_schema = loader.to_schema()["function"]["parameters"]["properties"]["name"]
    assert "enum" not in name_schema, name_schema


def test_a_user_playbook_in_the_library_registers_the_loader(tmp_path) -> None:
    """The flip side of the empty check: content is what gates the loader.

    A hand-written directory under the user layer is a playbook -- no generator
    involved -- and one file is enough to open the entry point.
    """
    from raven.config.schema import PlaybookConfig

    target = tmp_path / "playbooks" / "weekly-feedback"
    target.mkdir(parents=True)
    (target / "playbook.md").write_text(
        "---\nname: weekly-feedback\ndescription: weekly user-feedback analysis\n---\n\n"
        "body\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: prompt\ntaskSummary: weekly user-feedback analysis for the team\nconfirm: true\n"
        "triggers:\n  keywords: [weekly feedback]\n"
        "prompts: one research node\n"
        "```\n",
        encoding="utf-8",
    )

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )

    assert loop.tools.has("load_playbook")


def test_a_playbook_config_that_is_off_tells_the_model_nothing(tmp_path) -> None:
    """Off, neither entry exists -- the library is invisible rather than refused."""
    from raven.config.schema import PlaybookConfig

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=False),
    )

    assert loop._playbooks is None
    assert not loop.tools.has("create_playbook")
    assert not loop.tools.has("load_playbook")


def test_both_graph_tools_are_one_surface_to_a_consumer(tmp_path) -> None:
    """Who dispatched a run must not be observable from outside.

    A ``mode: dag`` playbook is dispatched by the engine's own graph tool, which
    is deliberately unregistered -- the model never calls it. Every consumer that
    asks a graph tool something, though, has to get the same answer for that run
    as for one the model composed: where its progress goes, whether it is live,
    whether a cancel lands. Asking the registered instance alone answered "not
    happening" to all three, which on the page meant a playbook's graph never
    appeared in the conversation and its stop button reported nothing to stop.
    """
    from raven.config.schema import PlaybookConfig

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )

    tools = loop.dag_tools()
    assert len(tools) == 2, "the registered instance and the engine's private one"
    assert tools[0] is loop.tools.get("run_subagent_dag")
    assert tools[1] is not tools[0], "the private one is a second instance, not the same object"

    sink = object()
    loop.set_dag_progress_sink(sink)
    assert [t._sink for t in tools] == [sink, sink], "progress reaches the page from either"

    # Cancel consults both. Neither owns this id, so the honest answer is False --
    # what matters is that the private one was asked at all.
    assert loop.cancel_dag_run("nope") is False
    tools[1]._cancels["run-x"] = _Flag()
    assert loop.cancel_dag_run("run-x") is True, "a run only the engine owns is still cancellable"


def test_resolve_dag_node_reaches_a_node_the_playbook_engine_owns(tmp_path) -> None:
    """A suspended node's desk lives on whichever tool dispatched its run.

    A mode: dag playbook's run is dispatched by the engine's private tool, so
    the desk holding its open nodes lives there too, never on the registered
    instance. resolve_dag_node has to consult both the way cancel_dag_run
    already does, or the model would be told a node is done waiting while
    dag_status -- which reads liveness through the same dag_tools() union --
    still shows it open.
    """
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.config.schema import PlaybookConfig

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )
    registered, private = loop.dag_tools()

    desk = AdjudicationDesk()
    desk.open("a")
    private._desks["run-x"] = desk

    assert loop.resolve_dag_node("run-x", "missing", "abandon", None) is False
    assert loop.resolve_dag_node("run-x", "a", "continue", "use staging") is True


class _Flag:
    def __init__(self) -> None:
        self.set_called = False

    def set(self) -> None:
        self.set_called = True


def test_liveness_is_the_union_across_graph_tools(tmp_path) -> None:
    """A run either tool owns is in flight; a broken tool is not fatal.

    Advisory by design: what this answer decides is whether a row is drawn as
    running, and a graph tool that cannot say must not turn that into an error
    the panel shows instead of a list.
    """
    from raven.config.schema import PlaybookConfig

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )
    registered, private = loop.dag_tools()

    registered._cancels["from-the-model"] = _Flag()
    private._cancels["from-a-playbook"] = _Flag()
    assert loop.active_dag_run_ids() == {"from-the-model", "from-a-playbook"}

    class _Broken:
        def active_run_ids(self):
            raise RuntimeError("gone")

    loop.dag_tools = lambda: [_Broken(), private]
    assert loop.active_dag_run_ids() == {"from-a-playbook"}, "one broken tool must not hide the other"


def test_a_runtime_that_cannot_build_leaves_the_feature_off(tmp_path, monkeypatch) -> None:
    """A library that fails to load is not a reason to refuse every turn.

    This guard is also what hid the ordering bug for a release: it turned an
    AttributeError into a warning line and an agent that quietly had no
    playbooks. Kept, because the alternative is a broken library taking down the
    loop -- but now asserted, so "off" is a tested outcome rather than a
    side effect nobody looked at.
    """
    from raven.config.schema import PlaybookConfig

    def _boom(self, cfg):
        raise RuntimeError("library is a rock")

    monkeypatch.setattr(AgentLoop, "_build_playbook_runtime", _boom)
    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path,
        model="fake/default",
        max_iterations=2,
        playbook_config=PlaybookConfig(enabled=True),
    )

    assert loop._playbooks is None
    assert not loop.tools.has("load_playbook") and not loop.tools.has("create_playbook")
    assert loop.dag_tools() == [loop.tools.get("run_subagent_dag")], "only the registered one is left"
