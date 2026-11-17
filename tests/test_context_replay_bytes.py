"""A turn's user message replays byte for byte.

The provider's cache is a prefix cache: the next turn reuses what the previous
one sent only when the bytes match. Three pieces of the user message carry
per-turn text -- the runtime-context block, the skill menu inside it, and the
turn scratch at its end. This drives the real assembler, the real session
write and the real history projection over two turns, and compares the bytes.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from raven.agent.context import ContextBuilder
from raven.agent.loop.main import AgentLoop
from raven.config.raven import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.history.compacting import CompactingHistoryBuilder
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
)
from raven.contracts.assembled import TokenBudget
from raven.contracts.memory import Memory
from raven.session.manager import SessionManager

WINDOW = 400_000


def _budget() -> TokenBudget:
    return TokenBudget(400_000, 4_000, 2_000, 1_000, 393_000)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.datetime(2026, 11, 17, 10, 0)

    def __call__(self) -> datetime.datetime:
        return self.now


class _Backend:
    """A recall that moves with the query, as the real one does."""

    async def feedback(self, signals: Any) -> None:
        pass

    async def recall(self, query: str, *, user_id=None, agent_id=None, top_k: int = 5) -> list[Memory]:
        if user_id is None:
            return []
        return [Memory(text=f"RECALL<{query}> " + "r" * 500, score=1.0)]


class _Provider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    def wire_model_id(self, model: str) -> str:
        return model


class _Scent:
    async def build(self, message: str, session_messages: list[dict[str, Any]]) -> Any:
        return SimpleNamespace(text="Possibly relevant skills:\n- one: d", skill_ids=["one"])


def _loop_stub():
    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: datetime.datetime(2026, 11, 17, 10, 0)
    return loop


def _engine(tmp_path: Path, clock: _Clock, *, scent: bool = False) -> ContextAssembler:
    (tmp_path / "TOOLS.md").write_text("bootstrap rules", encoding="utf-8")
    always = tmp_path / "skills" / "always-on"
    always.mkdir(parents=True)
    (always / "SKILL.md").write_text(
        '---\nname: always-on\ndescription: d\nmetadata: {"raven": {"always": true}}\n---\nbody\n',
        encoding="utf-8",
    )
    history = CompactingHistoryBuilder(
        tmp_path,
        ContextConfig(),
        _Provider(),  # type: ignore[arg-type]
        "stub",
        WINDOW,
        lambda: [],
    )
    builders: list[Any] = [
        IdentitySegmentBuilder(tmp_path),
        BootstrapSegmentBuilder(tmp_path),
        MemorySegmentBuilder(ContextBuilder(workspace=tmp_path).memory, _Backend()),
        ActiveSkillsSegmentBuilder(ContextBuilder(workspace=tmp_path).skills),
        history,
    ]
    assembler = ContextAssembler(
        builders,
        lambda: [],
        now_fn=clock,
        get_tool_notices=lambda: [],
        get_session_notices=lambda key: None,
    )
    if scent:
        assembler._scent = _Scent()  # type: ignore[attr-defined]
    return assembler


def _turn(engine: ContextAssembler, session: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    import asyncio

    async def _go() -> list[dict[str, Any]]:
        assembled = await engine.assemble(
            "cli:x",
            session,
            _budget(),
            turn=TurnContext(current_message=text, channel="cli", chat_id="x"),
        )
        return assembled.messages

    return asyncio.run(_go())


def _save(session: SessionManager, messages: list[dict[str, Any]]) -> None:
    AgentLoop._save_turn(_loop_stub(), session, messages, 0)


def _user(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next(m for m in messages if m.get("role") == "user")


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [str(b.get("text", "")) for b in content or [] if isinstance(b, dict)]


class TestTheQuestionReplaysByteForByte:
    def test_the_second_turn_replays_the_first_turns_question_as_it_was_sent(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)
        second = _turn(engine, session.messages, "the second question")

        sent = _user(first)["content"]
        replayed = _user(second[:-1])["content"]

        assert replayed == sent

    def test_the_replay_carries_the_skill_menu(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock, scent=True)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)
        second = _turn(engine, session.messages, "the second question")

        assert "Possibly relevant skills" in _user(first)["content"]
        assert _user(second[:-1])["content"] == _user(first)["content"]

    def test_the_replay_carries_the_turn_scratch(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)
        second = _turn(engine, session.messages, "the second question")

        assert "RECALL<the first question>" in _user(first)["content"]
        assert "RECALL<the first question>" in _user(second[:-1])["content"]

    def test_a_recovery_notice_replays_with_the_scratch_still_behind_the_words(self, tmp_path: Path) -> None:
        """Recovery injection prepends its notice to a string message, so the
        session write leaves the block in the content and files none; the
        scratch is still filed and must still come back."""
        from raven.agent.loop.turn_path import TurnPathMixin

        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        question = _user(first)
        recovering = SimpleNamespace(_pending_recovery={"cli:probe": {"files": ["a.py"]}})
        TurnPathMixin._inject_recovery_block(recovering, "cli:probe", first)  # type: ignore[arg-type]
        assert "previous turn was interrupted" in str(question["content"])
        _save(session, first)
        second = _turn(engine, session.messages, "the second question")

        assert _user(second[:-1])["content"] == question["content"]

    def test_a_note_landed_on_the_message_after_the_scratch_replays_where_it_stood(self, tmp_path: Path) -> None:
        """A hook note lands on the message the model is about to read, which is
        the question, and it lands after the scratch the question carries."""
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        note = "a hook note"
        question = _user(first)
        question["content"] = f"{question['content']}\n\n{note}"
        _save(session, first)
        second = _turn(engine, session.messages, "the second question")

        assert _user(second[:-1])["content"] == question["content"]

    def test_the_stored_entry_keeps_the_users_words_alone(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)
        manager.save(session)

        stored = next(m for m in session.messages if m.get("role") == "user")
        assert stored["content"] == "the first question"
        assert "RECALL<" not in stored["content"]
        # The recall rides in its own field, so the replay can put it back.
        assert "RECALL<the first question>" in stored["turn_scratch"]

    def test_the_transcript_the_client_reads_shows_the_question_not_the_recall(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)

        history = session.get_history()
        assert [m["content"] for m in history] == ["the first question"]

    def test_the_stored_entry_holds_no_private_key(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _engine(tmp_path, clock)
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")

        first = _turn(engine, session.messages, "the first question")
        _save(session, first)
        manager.save(session)

        on_disk = manager.session_path("cli:x").read_text(encoding="utf-8")
        assert "_turn_scratch" not in on_disk
        assert "_runtime_context" not in on_disk
        for line in on_disk.splitlines():
            entry = json.loads(line)
            if entry.get("role") != "user":
                continue
            assert not any(k.startswith("_") for k in entry), entry
