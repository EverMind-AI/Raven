"""An intercepted playbook turn must still write session history.

The interception returns before ``_save_turn``, so it records the exchange
itself, the way the personalization branch does. The follow-up flows depend
on it: a missing param answered on the next turn only works if the model can
see the question being asked (that answer carries no trigger word, so it
re-enters as a normal turn and the main agent needs the history), and
"did that run?" only works if the dispatch receipt is on record.
"""

from __future__ import annotations

from raven.agent.loop import AgentLoop
from raven.memory_engine.playbook import ExecutionPlan
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
    def __init__(self, plan: ExecutionPlan | None) -> None:
        self._plan = plan
        self.seen: list[str] = []

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
