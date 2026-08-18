"""An intercepted playbook turn must still write session history.

The interception returns before ``_save_turn``, so it records the exchange
itself, the way the personalization branch does. The follow-up flows depend
on it: a missing param answered on the next turn only works if the model can
see the question being asked (that answer carries no trigger word, so it
re-enters as a normal turn and ``run_playbook`` needs the history), and
"did that run?" only works if the dispatch receipt is on record.
"""

from __future__ import annotations

from raven.agent.loop import AgentLoop
from raven.playbook import ExecutionPlan
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

    def __init__(self, plan: ExecutionPlan | None) -> None:
        self._plan = plan
        self.seen: list[str] = []
        self.contexts: list[dict] = []
        self.resets: list[str | None] = []

    def listing(self) -> list[tuple[str, str]]:
        return []

    def set_context(self, **kwargs) -> None:
        self.contexts.append(kwargs)

    def reset_declines(self, conversation_id: str | None) -> None:
        self.resets.append(conversation_id)

    async def consider(self, message: str, **kwargs) -> ExecutionPlan | None:
        self.seen.append(message)
        return self._plan


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


async def test_intercepted_turn_records_both_sides_of_the_exchange(tmp_path):
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    question = "Running 'weekly-feedback' still needs a few details: which week?"
    loop._playbooks = _StubPlaybooks(ExecutionPlan(kind="questions", reply=question))

    reply, _ = await loop._process_message(_req("sort out this week's user feedback", conversation="web:abc"))

    assert reply == question
    assert provider.calls == 0  # the interception replaced the turn
    history = loop.sessions.get_or_create("web:abc").messages
    assert {"role": "user", "content": "sort out this week's user feedback"}.items() <= history[-2].items()
    assert {"role": "assistant", "content": question}.items() <= history[-1].items()


async def test_pass_through_leaves_recording_to_the_normal_turn(tmp_path):
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    loop._playbooks = _StubPlaybooks(None)

    reply, _ = await loop._process_message(_req("what's for lunch", conversation="web:abc"))

    assert reply == "normal turn ran"
    assert provider.calls == 1
    # Exactly one user entry: the funnel's pass-through must not double-record.
    history = loop.sessions.get_or_create("web:abc").messages
    assert sum(1 for m in history if m.get("role") == "user") == 1


async def test_every_origin_records_the_playbook_address(tmp_path):
    """The funnel only runs for user turns, but run_playbook is offered on
    every turn -- so the address must be recorded by the per-turn tool-context
    pass, or a cron-started run announces into the last human conversation
    (or the cli:direct default on a cold process)."""
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    stub = _StubPlaybooks(None)
    loop._playbooks = stub

    cron_req = TurnRequest(
        origin=Origin.CRON,
        source=Source(channel="slack", chat_id="#team", sender_id="cron", chat_type=ChatType.DM),
        text="run the weekly briefing playbook",
        conversation="cron:job-1",
    )
    await loop._process_message(cron_req, origin=Origin.CRON)

    assert {"channel": "slack", "chat_id": "#team", "session_key": "cron:job-1"} in stub.contexts
    # And the funnel itself did not run for the cron turn: interception is
    # user-origin only; the address pass is what covers everything else.
    assert stub.seen == []


async def test_an_injected_message_resets_the_conversations_declines(tmp_path):
    """The decline reset rides on consider(), and a mid-turn injected message
    never gets one -- the loop merges it straight into the running turn. So
    the merge point must reset, or "actually, go ahead and run it" sent while
    the fall-through turn is in flight is still refused by run_named."""
    provider = _Provider()
    loop = AgentLoop(provider=provider, workspace=tmp_path, model="fake/default")
    _stub_edges(loop)
    stub = _StubPlaybooks(None)
    loop._playbooks = stub

    pending = [_req("actually, go ahead and run it", conversation="web:abc")]

    def drain():
        out = list(pending)
        pending.clear()
        return out

    await loop._run_agent_loop(
        [{"role": "user", "content": "sort out user feedback"}],
        session_key="web:abc",
        drain=drain,
    )

    assert stub.resets == ["web:abc"]
