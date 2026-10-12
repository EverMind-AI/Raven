"""The episodic annotate step that runs after a user turn.

Pins the arithmetic the step does around ``MemoryConsolidator`` -- when it
seeds, when it counts enough, where it slices, what it does with the
consolidator's answer -- against a fake consolidator that records every call.
The consolidator itself (annotate -> ``episodes.md``, hot tags -> ``user.md``)
is exercised on a live gateway, not here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.config.raven import MemoryConfig
from raven.contracts.llm_provider import LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def chat_with_retry(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _FakeChatProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0

    async def chat_with_retry(self, **kwargs: Any) -> LLMResponse:
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r

    def get_default_model(self) -> str:
        return "fake/model"


class _FakeConsolidator:
    """Records what the step hands it; answers whatever the test scripted."""

    def __init__(self, *, result: bool = True, hold: asyncio.Event | None = None) -> None:
        self.result = result
        self.hold = hold
        self.tails: list[list[dict[str, Any]]] = []
        self.refreshes = 0
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, Any]]) -> bool:
        self.tails.append(list(messages))
        if self.hold is not None:
            await self.hold.wait()
        return self.result

    async def maybe_refresh_hot_tags(self) -> int:
        self.refreshes += 1
        return 0


class _FakeBackend:
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def feedback(self, signals: Any) -> None: ...

    async def recall(self, query: Any, *, user_id: Any = None, agent_id: Any = None, top_k: int = 5) -> list:
        return []

    async def store(self, session_id: Any, messages: Any, *, metadata: Any = None) -> None: ...


def _make_loop(workspace: Path, *, provider: Any | None = None, every: int = 3, backend: Any = None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider or _StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(backend=backend, memory_config=MemoryConfig(annotate_every_turns=every)),
    )
    return loop


def _turn(i: int) -> list[dict[str, Any]]:
    return [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]


def _session_with(loop: AgentLoop, key: str, turns: int):
    session = loop.sessions.get_or_create(key)
    for i in range(turns):
        session.messages.extend(_turn(i))
    loop.sessions.save(session)
    return session


async def _settle(loop: AgentLoop) -> None:
    if loop._consolidation_tasks:
        await asyncio.gather(*list(loop._consolidation_tasks), return_exceptions=True)


@pytest.fixture
def log_lines() -> Any:
    lines: list[str] = []
    handle = logger.add(lambda m: lines.append(m), level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(handle)


def _cursor_on_disk(loop: AgentLoop, key: str) -> Any:
    """The cursor as the next process would read it: drop the cached copy, reload."""
    loop.sessions._cache.pop(key, None)
    return loop.sessions.get_or_create(key).metadata.get("annotated_through")


class TestSeedAndCount:
    async def test_first_sight_seeds_the_cursor_and_writes_nothing(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:seed", turns=6)

        loop._maybe_annotate_tail(session, prev_len=10)
        await _settle(loop)

        assert fake.tails == []
        assert session.metadata["annotated_through"] == 10
        assert _cursor_on_disk(loop, "cli:seed") == 10

    async def test_the_fifth_turn_hands_the_tail_to_the_annotator(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=5)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:five", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(5):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert len(fake.tails) == 1
        tail = fake.tails[0]
        assert tail[0] == {"role": "user", "content": "q0"}
        assert tail[-1] == {"role": "assistant", "content": "a4"}
        assert session.metadata["annotated_through"] == 10
        assert _cursor_on_disk(loop, "cli:five") == 10
        assert fake.refreshes == 1

    async def test_three_turns_since_the_cursor_call_nothing(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=5)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:three", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert fake.tails == []
        assert session.metadata["annotated_through"] == 0

    async def test_zero_turns_the_step_off(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=0)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:off", turns=20)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert fake.tails == []
        assert "annotated_through" not in session.metadata
        assert _cursor_on_disk(loop, "cli:off") is None


class TestCursorEdges:
    async def test_undo_past_the_cursor_is_clamped(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=3)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:undo", turns=4)
        session.metadata["annotated_through"] = 8
        loop.sessions.save(session)
        before = session.last_consolidated

        assert session.undo_last_turn(2) == 4
        for i in (4, 5, 6):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert len(fake.tails) == 1
        assert fake.tails[0][0] == {"role": "user", "content": "q4"}
        assert session.metadata["annotated_through"] == 10
        assert session.last_consolidated == before == 0

    async def test_a_declined_annotation_keeps_the_cursor(self, tmp_path: Path, log_lines) -> None:
        loop = _make_loop(tmp_path, every=3)
        fake = _FakeConsolidator(result=False)
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:declined", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert len(fake.tails) == 1
        assert session.metadata["annotated_through"] == 0
        assert fake.refreshes == 0
        assert any("WARNING" in line and "declined" in line for line in log_lines)

    async def test_a_failing_patch_is_logged_not_raised(self, tmp_path: Path, log_lines, monkeypatch) -> None:
        loop = _make_loop(tmp_path, every=3)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:boom", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        def _boom(*_a: Any, **_k: Any) -> bool:
            raise OSError("disk says no")

        monkeypatch.setattr(loop.sessions, "append_metadata_patch", _boom)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert any("ERROR" in line and "failed" in line for line in log_lines)
        # the same loop still takes the next call
        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)


class TestConcurrency:
    async def test_the_reply_does_not_wait_for_the_annotator(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=3)
        gate = asyncio.Event()
        fake = _FakeConsolidator(hold=gate)
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:async", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await asyncio.sleep(0)
        assert len(fake.tails) == 1
        assert session.metadata["annotated_through"] == 0

        gate.set()
        await _settle(loop)
        assert session.metadata["annotated_through"] == 6

    async def test_a_waiting_task_recomputes_and_does_nothing(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path, every=3)
        gate = asyncio.Event()
        fake = _FakeConsolidator(hold=gate)
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:lock", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await asyncio.sleep(0)
        session.messages.extend(_turn(3))
        loop.sessions.save(session)
        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await asyncio.sleep(0)

        gate.set()
        await _settle(loop)

        assert len(fake.tails) == 1
        assert session.metadata["annotated_through"] == 6


class TestOrigins:
    @pytest.mark.parametrize("origin", [Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT, Origin.SUBAGENT])
    async def test_machine_turns_are_ignored(self, tmp_path: Path, origin: Origin) -> None:
        loop = _make_loop(
            tmp_path, provider=_FakeChatProvider([LLMResponse(content="ok", finish_reason="stop")]), every=1
        )
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        _stub_edges(loop)

        await loop._process_message(_req("hello", origin=origin), origin=origin)
        await _settle(loop)

        assert fake.tails == []
        session = loop.sessions.get_or_create("cli:c")
        assert "annotated_through" not in session.metadata

    async def test_a_user_turn_reaches_the_step(self, tmp_path: Path) -> None:
        loop = _make_loop(
            tmp_path, provider=_FakeChatProvider([LLMResponse(content="ok", finish_reason="stop")]), every=1
        )
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        _stub_edges(loop)

        await loop._process_message(_req("hello", origin=Origin.USER), origin=Origin.USER)
        await _settle(loop)
        session = loop.sessions.get_or_create("cli:c")
        # a new session seeds at 0 and counts its first turn in the same pass
        assert len(fake.tails) == 1
        assert fake.tails[0][0]["role"] == "user"
        assert session.metadata.get("annotated_through") == len(session.messages)

        await loop._process_message(_req("again", origin=Origin.USER), origin=Origin.USER)
        await _settle(loop)
        assert len(fake.tails) == 2
        assert fake.tails[1][0] == {k: v for k, v in fake.tails[1][0].items() if k in ("role", "content", "timestamp")}
        assert fake.tails[1][0]["content"] == "again"


def _req(text: str, *, origin: Origin = Origin.USER) -> TurnRequest:
    return TurnRequest(
        origin=origin,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        media=(),
    )


def _stub_edges(loop: AgentLoop) -> None:
    async def _noop(**_kw: Any) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop


class TestABackendChangesNothingHere:
    async def test_a_wired_backend_still_gets_the_tail_annotated(self, tmp_path: Path) -> None:
        """The host's log is written whether or not a plugin owns memory.

        Every acceptance run for this step drove a gateway with no backend,
        while the shipped default wires one. The step reads no backend, so the
        claim is structural -- pinned here so a later gate on one reddens
        rather than silently stopping the log on the configuration almost
        everyone runs.
        """
        loop = _make_loop(tmp_path, every=3, backend=_FakeBackend())
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:wired", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert loop.backend is not None
        assert len(fake.tails) == 1
        assert session.metadata["annotated_through"] == 6


class TestWhatHappensDuringTheAwait:
    async def test_a_truncation_during_the_await_does_not_claim_the_turns_that_replaced_it(
        self, tmp_path: Path
    ) -> None:
        """The reader can undo mid-annotation and then keep talking.

        The task sliced against indices that no longer mean anything:
        ``undo_last_turn`` moved the cursor itself, and the messages the
        write-back would claim are the new turns that arrived after it. A
        clamp to the live length is not enough -- the session is longer than
        the stale arithmetic, so the patch lands inside the replacements and
        they are never annotated.
        """
        loop = _make_loop(tmp_path, every=3)
        gate = asyncio.Event()
        fake = _FakeConsolidator(hold=gate)
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:race", turns=0)
        for i in range(6):
            session.messages.extend(_turn(i))
        session.metadata["annotated_through"] = 6
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=10)
        await asyncio.sleep(0)
        assert len(fake.tails) == 1  # sliced messages 6..12, now holding

        assert session.undo_last_turn(4) == 8
        assert session.metadata["annotated_through"] == 4  # pulled back to the cut
        for i in range(10, 13):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        gate.set()
        await _settle(loop)

        assert session.metadata["annotated_through"] == 4, "the stale write-back claimed the new turns"

        fake.tails.clear()
        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)
        assert [m["content"] for m in fake.tails[0]] == ["q10", "a10", "q11", "a11", "q12", "a12"]

    async def test_the_tail_ends_on_the_turn_that_asked_for_it(self, tmp_path: Path) -> None:
        """A task that waited must not slice into the turn that overtook it.

        The task is held on the lock, not inside the annotator, so the slice
        happens after the next turn's question has landed. Sliced to the live
        end it would hand over a user message whose answer has not arrived.
        """
        loop = _make_loop(tmp_path, every=3)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:boundary", turns=0)
        session.metadata["annotated_through"] = 0
        for i in range(3):
            session.messages.extend(_turn(i))
        loop.sessions.save(session)

        held = fake.get_lock(session.key)
        await held.acquire()
        loop._maybe_annotate_tail(session, prev_len=4)
        await asyncio.sleep(0)
        assert fake.tails == []  # still waiting on the lock
        session.messages.append({"role": "user", "content": "still unanswered"})
        held.release()
        await _settle(loop)

        assert len(fake.tails) == 1
        assert fake.tails[0][-1] == {"role": "assistant", "content": "a2"}


class TestMachineTurnsInsideTheTail:
    async def test_a_sentinel_turn_between_two_user_turns_is_not_annotated(self, tmp_path: Path) -> None:
        """Sentinel reads this file; its own turns must not come back to it.

        The origin check gates the trigger, not the content: a Sentinel turn
        lands in the reader's own session, between their turns, and would ride
        into the tail the next user turn hands over.
        """
        loop = _make_loop(tmp_path, every=2)
        fake = _FakeConsolidator()
        loop.memory_consolidator = fake
        session = _session_with(loop, "cli:mixed", turns=0)
        session.metadata["annotated_through"] = 0
        session.messages.extend(_turn(0))
        session.messages.extend(
            [
                {"role": "user", "content": "SENTINEL NOTICE", "origin": "sentinel"},
                {"role": "assistant", "content": "noted", "origin": "sentinel"},
            ]
        )
        session.messages.extend(_turn(1))
        loop.sessions.save(session)

        loop._maybe_annotate_tail(session, prev_len=len(session.messages) - 2)
        await _settle(loop)

        assert len(fake.tails) == 1
        contents = [m["content"] for m in fake.tails[0]]
        assert "SENTINEL NOTICE" not in contents
        assert contents == ["q0", "a0", "q1", "a1"]
