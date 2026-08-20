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
from raven.agent.tools.base import Tool
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
