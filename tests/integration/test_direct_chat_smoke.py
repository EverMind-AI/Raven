"""Direct chat end to end: switch in, follow up, hand off.

Uses a real ``AgentLoop``, a real ``SubagentManager``, a real registry file and
real record directories. The only stub is the LLM provider -- this is about the
wiring between the sub-agent manager, the turn pipeline and the handoff, not
about model quality.

The unit tests each stub the layer below them, so nothing else proves the four
phases fit together: a direct turn that never reaches the transcript, a record
directory the handoff can name, replayed state that makes the second turn a
continuation, and a block that lands on the next main-agent turn.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from raven.agent.loop.main import AgentLoop
from raven.providers.base import LLMResponse
from raven.spine import ChatType, Origin, Source, TurnRequest

_CONVERSATION = "tui:smoke"


class _EchoProvider:
    """Replies with a count of the user turns it was given, so a replay shows."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat_with_retry(self, *, messages, tools=None, model=None, **_kwargs):
        self.seen.append(list(messages))
        asked = [m["content"] for m in messages if m.get("role") == "user"]
        return LLMResponse(content=f"saw {len(asked)}: {asked[-1]}", finish_reason="stop")

    async def chat(self, *args, **kwargs):
        return await self.chat_with_retry(*args, **kwargs)

    async def chat_stream(self, *, messages, tools=None, model=None, **_kwargs):
        """The same reply, cut in two, so a streamed turn is visibly not one frame."""
        from raven.providers.base import StreamDelta

        response = await self.chat_with_retry(messages=messages, tools=tools, model=model)
        text = response.content or ""
        for piece in (text[:5], text[5:]):
            yield StreamDelta(content=piece)


def _req(text: str, *, target: tuple[str, str] | None = None) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="smoke", sender_id="user", chat_type=ChatType.DM),
        text=text,
        conversation=_CONVERSATION,
        direct_target=target,
    )


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentLoop:
    """A real loop whose registry and agent home live under ``tmp_path``.

    The registry is repointed rather than mocked: this test asserts on the rows
    it writes, and a developer's own ``~/.raven/subagent_instances.json`` must
    never be touched by a test run.
    """
    from raven.agent.subagent import instances as instances_mod

    monkeypatch.setattr(instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "instances.json"))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))

    return AgentLoop(
        provider=_EchoProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
    )


def _last_user(provider: _EchoProvider) -> str:
    """The newest user message of the last model call.

    Carries the loop's runtime-context header ahead of the typed text, so
    assertions here match on a suffix rather than on equality.
    """
    return [m["content"] for m in provider.seen[-1] if m.get("role") == "user"][-1]


async def _run(loop: AgentLoop, req: TurnRequest, sink: list[str]) -> None:
    async def emit(event) -> None:
        content = getattr(event, "content", None)
        if content:
            sink.append(str(content))

    # Sync, returning an iterable: the loop consumes it with `for inj in drain()`.
    def drain():
        return []

    await loop.run_turn(req, emit, drain, stream=False)


async def test_direct_chat_round_trip_leaves_records_and_a_handoff(loop: AgentLoop) -> None:
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instance_state import InstanceState, instance_state_path
    from raven.agent.subagent.instances import get_registry

    replies: list[str] = []

    await _run(loop, _req("first", target=("raven", "notes")), replies)
    await _run(loop, _req("second", target=("raven", "notes")), replies)

    # Both landed, and the second saw the first: that is the replay.
    assert len(replies) == 2
    assert replies[0].startswith("saw 1:")
    assert replies[1].startswith("saw 2:")

    # Neither reached the main agent's transcript.
    assert loop.sessions.get_or_create(_CONVERSATION).messages == []
    assert loop._direct_handoff.pending_count(_CONVERSATION) == 2

    session_dir = loop.subagents._session_dir(_CONVERSATION)
    root = direct_root(session_dir, "raven", "notes")
    calls = sorted(p for p in root.iterdir() if p.is_dir())
    assert len(calls) == 2
    for call in calls:
        assert (call / "prompt.md").is_file()
        assert (call / "out.md").is_file()

    rows = get_registry().list_instances(_CONVERSATION)
    assert ("raven", "notes") in {(r["agent"], r["handle"]) for r in rows}

    stored = InstanceState(instance_state_path(session_dir, "raven", "notes")).load()
    assert [m["content"] for m in stored if m["role"] == "user"] == ["first", "second"]

    # The next main-agent turn carries the block, prepended to what the user typed.
    provider = loop.provider
    await _run(loop, _req("what happened?"), replies)

    prompt = _last_user(provider)
    assert str(root) in prompt
    assert prompt.endswith("what happened?")
    # Pointers, never the exchange itself.
    assert "saw 1:" not in prompt
    assert loop._direct_handoff.pending_count(_CONVERSATION) == 0

    # And only once. The earlier turn keeps its block -- it is part of that user
    # message now, and the main agent is meant to still see it in history -- so
    # this reads the newest message rather than the whole prompt.
    await _run(loop, _req("and now?"), replies)
    latest = _last_user(provider)
    assert str(root) not in latest
    assert latest.endswith("and now?")


async def test_a_direct_turn_registers_the_instance_it_addressed(loop: AgentLoop) -> None:
    """The chip strip reads the registry, so a direct turn has to write one."""
    from raven.agent.subagent.instances import get_registry

    await _run(loop, _req("hello", target=("raven", "scratch")), [])

    row = next(r for r in get_registry().list_instances(_CONVERSATION) if r["handle"] == "scratch")
    assert row["status"] == "completed"
    assert row["agent"] == "raven"


class _LockTakingBackend:
    """Shaped like the cli backend: it takes the handle lock itself.

    The built-in backend takes no lock, which is why every earlier test passed
    through a deadlock that only a third-party agent could hit. This is that
    configuration, driven through the whole turn path.
    """

    kind = "cli"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_kwargs) -> str:
        from raven.agent.subagent.instances import hold_handle

        async with hold_handle(session_key or "default", "Coder", instance or task_id):
            self.asked.append(task)
            return f"Coder: {task}"


async def test_a_third_party_instance_answers_and_lands_in_the_handoff(loop: AgentLoop) -> None:
    import asyncio

    from raven.agent.subagent.direct_chat import direct_root

    backend = _LockTakingBackend()
    loop.subagents._backends["Coder"] = backend

    replies: list[str] = []
    await asyncio.wait_for(_run(loop, _req("who are you", target=("Coder", "greet")), replies), timeout=5)

    assert replies == ["Coder: who are you"]
    assert backend.asked == ["who are you"]

    root = direct_root(loop.subagents._session_dir(_CONVERSATION), "Coder", "greet")
    call = next(p for p in root.iterdir() if p.is_dir())
    assert (call / "out.md").read_text(encoding="utf-8") == "Coder: who are you"
    assert loop._direct_handoff.pending_count(_CONVERSATION) == 1


async def test_a_disabled_agent_is_refused_rather_than_answered_by_raven(loop: AgentLoop) -> None:
    """Toggling a sub-agent off must not silently hand the turn to the built-in
    backend under that agent's name."""
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="disabled or no longer configured"):
        await _run(loop, _req("still there?", target=("GoneAgent", "h")), [])


async def test_a_streamed_direct_turn_arrives_as_deltas_and_is_not_delivered_twice(loop: AgentLoop) -> None:
    """The real assembly: loop, manager, built-in backend, record directory.

    Every unit test here stubs the layer below it, and the defects this feature
    actually shipped all lived in the joins -- so this is the one place that
    proves a delta leaving the backend reaches the turn's outlet, and that the
    reply is then not delivered a second time as a closing Text.
    """
    from raven.agent.subagent.direct_chat import direct_root
    from raven.spine.events import StreamDelta, Text

    events: list[object] = []

    async def emit(event) -> None:
        events.append(event)

    await loop.run_turn(_req("first", target=("raven", "notes")), emit, lambda: [], stream=True)

    streamed = "".join(e.delta for e in events if isinstance(e, StreamDelta))
    assert streamed.startswith("saw 1:")
    assert not [e for e in events if isinstance(e, Text)]

    # What the client rendered and what the next switch-in replays are one text.
    root = direct_root(loop.subagents._session_dir(_CONVERSATION), "raven", "notes")
    call = next(p for p in root.iterdir() if p.is_dir())
    assert (call / "out.md").read_text(encoding="utf-8") == streamed


async def test_a_turn_on_an_instance_lane_still_records_against_the_session(loop: AgentLoop) -> None:
    """A direct chat runs on its own lane so it can be concurrent, but its
    records, its instance row and its handoff belong to the *session*. Keyed by
    the lane they would scatter one instance's history into a directory of its
    own and land the handoff on a conversation nobody reads.
    """
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instances import get_registry
    from raven.spine import direct_lane

    lane = direct_lane(_CONVERSATION, "raven", "notes")
    assert lane != _CONVERSATION

    req = _req("on a lane", target=("raven", "notes"))
    await _run(loop, replace(req, conversation=lane), [])

    session_dir = loop.subagents._session_dir(_CONVERSATION)
    calls = [p for p in direct_root(session_dir, "raven", "notes").iterdir() if p.is_dir()]
    assert len(calls) == 1
    assert (calls[0] / "out.md").is_file()

    rows = get_registry().list_instances(_CONVERSATION)
    assert ("raven", "notes") in {(r["agent"], r["handle"]) for r in rows}

    # And the handoff reaches the main agent's next turn, which runs on the
    # session's own lane.
    assert loop._direct_handoff.pending_count(_CONVERSATION) == 1
    await _run(loop, _req("what happened?"), [])
    assert str(direct_root(session_dir, "raven", "notes")) in _last_user(loop.provider)
