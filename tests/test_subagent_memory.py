"""The Memory record: what a sub-agent left in long-term memory for one call.

The module speaks the MemoryBackend contract and nothing else, so the double
here is a backend, not a transport. What a row looks like on the wire, and how
it renders, belongs to whichever backend is installed and is tested with it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import pytest

from raven.agent import subagent_memory as subagent_memory_mod
from raven.agent.subagent_memory import (
    MemoryScope,
    collect_memories,
    prime_from_turn,
    record_memories,
    scope_from_config,
    trace_session_id,
)
from raven.config.schema import SubagentMemoryConfig
from raven.contracts.memory import Memory


class _FakeBackend:
    """A memory backend that records what it was asked and answers canned rows.

    Rows are keyed by track, which is the only distinction the contract makes:
    the host names a track and reads ``Memory.text``, and everything about how
    a backend stores or renders a memory stays behind that.
    """

    def __init__(self) -> None:
        self.reads: list[tuple[str, str | None, str | None]] = []
        self.writes: list[tuple[str, list[dict], dict | None]] = []
        self.rows: dict[str, list[Memory]] = {"user_id": [], "agent_id": []}
        self.raises: Exception | None = None
        self.store_answer: bool = True

    async def recall_session(self, session_id, *, user_id=None, agent_id=None):
        self.reads.append((session_id, user_id, agent_id))
        if self.raises is not None:
            raise self.raises
        return list(self.rows["user_id" if user_id else "agent_id"])

    async def store(self, session_id, messages, *, metadata=None):
        self.writes.append((session_id, list(messages), metadata))
        if self.raises is not None:
            raise self.raises
        return self.store_answer


def _memory(text: str, kind: str = "episode") -> Memory:
    return Memory(text=text, metadata={"type": kind})


def _scope(**kw) -> MemoryScope:
    block = {
        "user_id": kw.get("user_id", "raven-code"),
        "agent_id": kw.get("agent_id", "raven-code"),
    }
    return MemoryScope(
        block={k: v for k, v in block.items() if v},
        source=kw.get("source", "agent"),
        session_prefix="cli:",
    )


def _sink() -> tuple[list[str], Callable]:
    written: list[str] = []

    async def write(text: str) -> None:
        written.append(text)

    return written, write


async def _key() -> str:
    return "cli:abc"


# --------------------------------------------------------------------------- scope


def test_no_config_means_no_scope() -> None:
    assert scope_from_config(None) is None


def test_the_block_reaches_the_backend_unread() -> None:
    """Which keys identify a memory is the backend's vocabulary. The host reads
    two: which path to run, and how the fork prefixes its session id."""
    scope = scope_from_config(
        SubagentMemoryConfig.model_validate(
            {"userId": "u", "agentId": "a", "source": "trace", "sessionPrefix": "x:", "mem0Space": "s"}
        )
    )

    assert scope is not None
    assert scope.source == "trace" and scope.session_prefix == "x:"
    assert scope.block == {"userId": "u", "agentId": "a", "mem0Space": "s"}
    assert (scope.user_id, scope.agent_id) == ("u", "a")


def test_source_defaults_to_agent() -> None:
    scope = scope_from_config(SubagentMemoryConfig.model_validate({"userId": "u"}))

    assert scope is not None and scope.source == "agent"


def test_a_declared_base_url_is_dropped_with_a_warning() -> None:
    """No config, fixture or document ever set one, and honouring it would mean
    every backend growing a per-call way to address a different server."""
    scope = scope_from_config(SubagentMemoryConfig.model_validate({"userId": "u", "baseUrl": "http://elsewhere"}))

    assert scope is not None
    assert "baseUrl" not in scope.block and "base_url" not in scope.block


# --------------------------------------------------------------------------- reading back


@pytest.mark.asyncio
async def test_both_tracks_are_asked_for_separately() -> None:
    """The contract takes one track per call, so each declared owner is its own
    question."""
    backend = _FakeBackend()
    backend.rows["user_id"] = [_memory("Ran the audit.")]
    backend.rows["agent_id"] = [_memory("Fixed the tokenizer.", "agent_case")]

    items = await collect_memories(backend, _scope(), "cli:abc")

    assert backend.reads == [("cli:abc", "raven-code", None), ("cli:abc", None, "raven-code")]
    assert [i.text for i in items] == ["Ran the audit.", "Fixed the tokenizer."]


@pytest.mark.asyncio
async def test_only_the_declared_owner_is_asked() -> None:
    backend = _FakeBackend()
    backend.rows["agent_id"] = [_memory("Fixed the tokenizer.", "agent_case")]

    await collect_memories(backend, _scope(user_id=None), "cli:abc")

    assert backend.reads == [("cli:abc", None, "raven-code")]


@pytest.mark.asyncio
async def test_a_memory_with_no_usable_text_is_dropped() -> None:
    backend = _FakeBackend()
    backend.rows["user_id"] = [_memory("   "), _memory("Ran the audit.")]

    items = await collect_memories(backend, _scope(agent_id=None), "cli:abc")

    assert [i.text for i in items] == ["Ran the audit."]


@pytest.mark.asyncio
async def test_a_backend_failure_propagates_to_the_caller() -> None:
    """The caller decides what an unreachable memory service means for the
    record; this function does not get to swallow it."""
    backend = _FakeBackend()
    backend.raises = RuntimeError("service down")

    with pytest.raises(RuntimeError):
        await collect_memories(backend, _scope(), "cli:abc")


# --------------------------------------------------------------------------- the record


@pytest.mark.asyncio
async def test_a_found_memory_is_recorded_as_settled() -> None:
    backend = _FakeBackend()
    backend.rows["user_id"] = [_memory("Ran the audit.")]
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=backend,
        scope=_scope(agent_id=None),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )

    assert json.loads(written[0]) == {
        "agent": "Raven-Code",
        "source": "agent",
        "status": "settled",
        "memories": [{"type": "episode", "text": "Ran the audit."}],
    }


@pytest.mark.asyncio
async def test_nothing_found_within_the_budget_is_pending() -> None:
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(agent_id=None),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )

    assert json.loads(written[0])["status"] == "pending"


@pytest.mark.asyncio
async def test_an_unreachable_backend_is_unavailable_not_a_raise() -> None:
    backend = _FakeBackend()
    backend.raises = RuntimeError("service down")
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=backend,
        scope=_scope(),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )

    assert json.loads(written[0])["status"] == "unavailable"


@pytest.mark.asyncio
async def test_no_join_key_writes_no_file_at_all() -> None:
    """There is nothing truthful to say about a call with no session id."""
    written, write = _sink()

    async def _none() -> None:
        return None

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(),
        resolve_session_id=_none,
        write=write,
        budget_s=0.0,
    )

    assert written == []


@pytest.mark.asyncio
async def test_polling_stops_once_the_result_stops_growing(monkeypatch) -> None:
    monkeypatch.setattr("raven.agent.subagent_memory._BACKOFF_S", (0.01, 0.02, 0.04))
    backend = _FakeBackend()
    backend.rows["user_id"] = [_memory("First.")]
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=backend,
        scope=_scope(agent_id=None),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=5.0,
    )

    # One look finds it, a second confirms it stopped growing. No third.
    assert len(backend.reads) == 2
    assert json.loads(written[0])["status"] == "settled"


@pytest.mark.asyncio
async def test_a_failing_write_never_raises_at_the_caller() -> None:
    """An audit trail written after the call already answered must not disturb
    anything by failing."""

    async def write(_: str) -> None:
        raise OSError("read-only file system")

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )


@pytest.mark.asyncio
async def test_a_failing_resolver_never_raises_at_the_caller() -> None:
    written, write = _sink()

    async def _boom() -> str:
        raise RuntimeError("registry gone")

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(),
        resolve_session_id=_boom,
        write=write,
        budget_s=0.0,
    )

    assert written == []


@pytest.mark.asyncio
async def test_a_transient_failure_after_growth_keeps_what_was_found(monkeypatch) -> None:
    monkeypatch.setattr("raven.agent.subagent_memory._BACKOFF_S", (0.01,))
    backend = _FakeBackend()
    backend.rows["user_id"] = [_memory("First.")]
    written, write = _sink()

    real = backend.recall_session
    calls = {"n": 0}

    async def flaky(session_id, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("dropped")
        return await real(session_id, **kw)

    backend.recall_session = flaky  # type: ignore[method-assign]

    await record_memories(
        agent="Raven-Code",
        backend=backend,
        scope=_scope(agent_id=None),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=5.0,
    )

    payload = json.loads(written[0])
    assert payload["status"] == "settled"
    assert [m["text"] for m in payload["memories"]] == ["First."]


@pytest.mark.asyncio
async def test_a_stalled_look_is_cut_off_by_the_remaining_budget() -> None:
    """``budget_s`` bounds the whole poll, not just the sleeps between looks.

    The first look always runs in full -- a budget of zero still means "look
    once" -- but a later one that runs past its share of the remaining budget
    must be cut off rather than left to its own, much longer, timeout, or a
    stalled backend keeps a recorder alive for minutes past what the budget
    says.
    """
    backend = _FakeBackend()
    calls = {"n": 0}

    async def first_answers_then_stalls(session_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_memory("First.")]
        await asyncio.sleep(2.0)
        return []

    backend.recall_session = first_answers_then_stalls  # type: ignore[method-assign]
    written, write = _sink()

    started = time.monotonic()
    await record_memories(
        agent="Raven-Code",
        backend=backend,
        scope=_scope(agent_id=None),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=1.0,
    )
    elapsed = time.monotonic() - started

    # 1s of slack over a 1s budget: the bound is on wall time, so the margin
    # that matters is absolute, and scaling it down with the budget is what
    # would turn a slow case into an intermittent one.
    assert elapsed < 2.0, "a stalled look must not be allowed to run past the budget"
    payload = json.loads(written[0])
    assert payload["status"] == "settled"
    assert payload["memories"] == [{"type": "episode", "text": "First."}]


@pytest.mark.asyncio
async def test_the_record_names_the_instance() -> None:
    """Unlike the identity and session id, this is something the reader can act
    on: passing it back as ``spawn``'s ``instance`` continues the same
    conversation."""
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
        instance="inst-1",
    )

    assert json.loads(written[0])["instance"] == "inst-1"


@pytest.mark.asyncio
async def test_a_call_with_no_instance_omits_the_key() -> None:
    """A key that is always present but usually empty costs every reader a
    check."""
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )

    assert "instance" not in json.loads(written[0])


@pytest.mark.asyncio
async def test_the_record_names_its_source() -> None:
    """A memory the agent wrote and one the host synthesised from its
    transcript are not equally strong evidence."""
    written, write = _sink()

    await record_memories(
        agent="Raven-Code",
        backend=_FakeBackend(),
        scope=_scope(source="trace"),
        resolve_session_id=lambda: _key(),
        write=write,
        budget_s=0.0,
    )

    assert json.loads(written[0])["source"] == "trace"


# --------------------------------------------------------------------------- priming


class TestPrimeFromTurn:
    """The host hands its own backend a conversation nobody wrote memories for."""

    @pytest.mark.asyncio
    async def test_the_turn_is_stored_with_flush_and_its_owners(self) -> None:
        """``flush`` because the conversation has already ended -- waiting for
        the backend's cadence would wait for a turn that never comes -- and the
        owners because the content is the sub-agent's, not the host's."""
        backend = _FakeBackend()

        assert await prime_from_turn(
            backend=backend,
            scope=_scope(),
            session_id="cli:abc",
            turn=[{"role": "user", "content": "do it"}],
        )

        session_id, messages, metadata = backend.writes[0]
        assert session_id == "cli:abc"
        assert [m["content"] for m in messages] == ["do it"]
        assert metadata["flush"] is True
        assert metadata["user_id"] == "raven-code" and metadata["agent_id"] == "raven-code"

    @pytest.mark.asyncio
    async def test_an_empty_turn_writes_nothing(self) -> None:
        backend = _FakeBackend()

        assert await prime_from_turn(backend=backend, scope=_scope(), session_id="s", turn=[]) is False
        assert backend.writes == []

    @pytest.mark.asyncio
    async def test_a_missing_owner_writes_nothing(self) -> None:
        """A missing owner must not fall back to a shared default: that would
        write this sub-agent's memories into the host's own track."""
        for scope in (_scope(user_id=None), _scope(agent_id=None)):
            backend = _FakeBackend()

            landed = await prime_from_turn(
                backend=backend,
                scope=scope,
                session_id="s",
                turn=[{"role": "user", "content": "x"}],
            )

            assert landed is False
            assert backend.writes == []

    @pytest.mark.asyncio
    async def test_a_refused_write_is_false_not_an_exception(self) -> None:
        backend = _FakeBackend()
        backend.store_answer = False

        assert (
            await prime_from_turn(
                backend=backend,
                scope=_scope(),
                session_id="s",
                turn=[{"role": "user", "content": "x"}],
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_a_raising_backend_is_false_not_an_exception(self) -> None:
        """The caller's next move is to record ``unavailable``, not to fail a
        run."""
        backend = _FakeBackend()
        backend.raises = RuntimeError("service down")

        assert (
            await prime_from_turn(
                backend=backend,
                scope=_scope(),
                session_id="s",
                turn=[{"role": "user", "content": "x"}],
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_the_prompt_is_never_timestamped_after_the_work(self) -> None:
        """``append_turn`` stamps the user row when the turn ends, so its clock
        is later than the work it caused; a consumer sorting by timestamp would
        read the prompt as the last thing that happened."""
        backend = _FakeBackend()

        await prime_from_turn(
            backend=backend,
            scope=_scope(),
            session_id="s",
            turn=[
                {"role": "user", "content": "ask", "timestamp": "2026-09-15T10:00:09Z"},
                {"role": "assistant", "content": "work", "timestamp": "2026-09-15T10:00:05Z"},
            ],
        )

        _, messages, _ = backend.writes[0]
        # Only the first row moves, and only backwards: it is rewritten to an
        # epoch just before the earliest of the rest.
        assert subagent_memory_mod._as_ms_epoch(messages[0]["timestamp"]) < subagent_memory_mod._as_ms_epoch(
            messages[1]["timestamp"]
        )


class TestTraceSessionId:
    def test_id_is_its_own_namespace(self) -> None:
        assert trace_session_id("Raven-Code", "t1").startswith("trace:")

    def test_id_is_unique_per_call(self) -> None:
        assert trace_session_id("a", "t1") != trace_session_id("a", "t2")


class TestMonotonic:
    def test_an_already_ordered_turn_passes_through_untouched(self) -> None:
        turn = [
            {"role": "user", "content": "a", "timestamp": "2026-09-15T10:00:00Z"},
            {"role": "assistant", "content": "b", "timestamp": "2026-09-15T10:00:05Z"},
        ]

        assert subagent_memory_mod._monotonic(turn) == turn

    def test_a_single_row_turn_passes_through(self) -> None:
        turn = [{"role": "user", "content": "a"}]

        assert subagent_memory_mod._monotonic(turn) == turn
