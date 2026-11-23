"""Session revision: what names a log, how it moves, and how two writers agree on it.

``log_id`` changes on a clear (``/new``), ``log_generation`` on an undo, and both
are written into the transcript's metadata record. Two copies of one session
saved by different managers settle on one revision instead of raising: the log
id follows the most recent clear, and on a shared id the generation is the
larger one. A ref minted against a revision that has since moved on must stop
resolving.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.agent.tools.context_retrieve import ContextRetrieveTool
from raven.contracts.context import SessionRevision
from raven.session.manager import Session, SessionManager
from raven.session.refs import session_ref


def _records(manager: SessionManager, key: str) -> list[dict]:
    path = manager.session_path(key)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _last_metadata(manager: SessionManager, key: str) -> dict:
    return [row for row in _records(manager, key) if row.get("_type") == "metadata"][-1]


def _three_messages(session: Session) -> None:
    for role, content in (("user", "one"), ("assistant", "first"), ("user", "two")):
        session.add_message(role, content)


def _contents(messages: list[dict]) -> list:
    return [message.get("content") for message in messages]


def _disk_contents(manager: SessionManager, key: str) -> list:
    return [row.get("content") for row in _records(manager, key) if row.get("_type") != "metadata"]


async def _assert_live_and_disk_agree(manager: SessionManager, key: str) -> None:
    """Every ordinal of the live copy's scope names the same message in memory and on disk."""
    live = manager.get_or_create(key)
    disk = SessionManager(manager.workspace)
    assert _contents(live.messages) == _disk_contents(manager, key)
    assert disk.peek(key).revision.scope == live.revision.scope
    for index in range(len(live.messages)):
        ref = session_ref(live.revision, index).render()
        assert await _retrieve(manager, key, ref) == await _retrieve(disk, key, ref)


async def _retrieve(manager: SessionManager, key: str, ref: str) -> str:
    tool = ContextRetrieveTool(manager)
    tool.set_context(*key.split(":", 1), key)
    return await tool.execute(refs=[ref], max_tokens=32_000)


class TestLifecycle:
    def test_an_append_leaves_the_revision_alone(self) -> None:
        session = Session(key="tui:append")
        before = session.revision

        session.add_message("user", "hello")

        assert session.revision == before

    def test_a_new_session_has_a_uuid_log_id(self) -> None:
        session = Session(key="tui:fresh")

        assert len(session.log_id) == 32 and int(session.log_id, 16) >= 0
        assert session.log_generation == 0
        assert isinstance(session.revision, SessionRevision)

    def test_undo_bumps_the_generation(self) -> None:
        session = Session(key="tui:undo")
        _three_messages(session)
        before = session.revision

        assert session.undo_last_turn() == 1
        session.add_message("user", "replacement")

        assert session.revision.log_id == before.log_id
        assert session.revision.generation == before.generation + 1

    def test_an_undo_that_removes_nothing_keeps_the_revision(self) -> None:
        session = Session(key="tui:noop", messages=[{"role": "assistant", "content": "hello"}])
        before = session.revision

        assert session.undo_last_turn() == 0
        assert session.revision == before

    def test_clear_mints_a_new_log_id_and_resets_the_generation(self) -> None:
        session = Session(key="tui:clear", log_generation=4)
        session.add_message("user", "old")
        before = session.revision

        session.clear()

        assert session.revision.log_id != before.log_id
        assert session.revision.generation == 0
        assert session.log_started_at is not None


class TestPersistence:
    def test_the_revision_is_written_into_the_metadata_record(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:record")
        session.add_message("user", "one")
        manager.save(session)

        record = _last_metadata(manager, session.key)

        assert (record["log_id"], record["log_generation"]) == (session.log_id, 0)

    def test_reload_keeps_log_id_and_generation(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:persist")
        _three_messages(session)
        manager.save(session)
        session.undo_last_turn()
        manager.save(session)
        session.clear()
        session.add_message("user", "after the clear")
        session.add_message("assistant", "reply")
        session.add_message("user", "undo me")
        session.undo_last_turn()
        manager.save(session)
        saved = session.revision

        loaded = SessionManager(tmp_path).get_or_create(session.key)

        assert loaded.revision == saved
        assert loaded.log_started_at == session.log_started_at
        loaded.add_message("assistant", "more")
        assert loaded.revision.scope == saved.scope

    def test_undo_then_same_length_append_keeps_the_new_generation(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:replace")
        session.add_message("user", "keep")
        session.add_message("assistant", "old answer")
        manager.save(session)

        assert session.undo_last_turn() == 2
        session.add_message("user", "replacement")
        session.add_message("assistant", "new answer")
        manager.save(session)

        loaded = SessionManager(tmp_path).get_or_create(session.key)
        assert [message["content"] for message in loaded.messages] == ["replacement", "new answer"]
        assert loaded.log_generation == 1

    def test_an_old_format_file_gets_a_new_log_id_written_on_the_next_save(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions" / "tui" / "legacy.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {"_type": "metadata", "key": "tui:legacy", "metadata": {}},
            {"role": "user", "content": "old"},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

        manager = SessionManager(tmp_path)
        loaded = manager.get_or_create("tui:legacy")
        minted = loaded.log_id
        assert len(minted) == 32 and loaded.log_generation == 0
        loaded.add_message("assistant", "new")
        manager.save(loaded)

        assert _last_metadata(manager, "tui:legacy")["log_id"] == minted
        assert SessionManager(tmp_path).get_or_create("tui:legacy").log_id == minted

    @pytest.mark.parametrize(
        "revision_fields",
        [
            {"log_id": "bad#id", "log_generation": 0},
            {"log_id": "a" * 32},
            {"log_id": "a" * 32, "log_generation": -1},
            {"log_id": "a" * 32, "log_generation": True},
            {"log_id": "A" * 32, "log_generation": 0},
        ],
    )
    def test_a_malformed_stored_revision_is_replaced(self, tmp_path: Path, revision_fields: dict) -> None:
        path = tmp_path / "sessions" / "tui" / "malformed.jsonl"
        path.parent.mkdir(parents=True)
        record = {"_type": "metadata", "key": "tui:malformed", "metadata": {}, **revision_fields}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        manager = SessionManager(tmp_path)
        loaded = manager.get_or_create("tui:malformed")
        manager.save(loaded)

        assert loaded.log_id != revision_fields["log_id"]
        assert SessionManager(tmp_path).get_or_create(loaded.key).log_id == loaded.log_id

    def test_the_latest_record_speaks_even_when_an_older_one_was_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions" / "tui" / "latest-malformed.jsonl"
        path.parent.mkdir(parents=True)
        old_log_id = "a" * 32
        records = [
            {
                "_type": "metadata",
                "key": "tui:latest-malformed",
                "metadata": {},
                "log_id": old_log_id,
                "log_generation": 6,
            },
            {"role": "user", "content": "persisted"},
            {
                "_type": "metadata",
                "key": "tui:latest-malformed",
                "metadata": {},
                "log_id": old_log_id,
                "log_generation": -1,
            },
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

        loaded = SessionManager(tmp_path).get_or_create("tui:latest-malformed")

        assert loaded.log_id != old_log_id
        assert loaded.log_generation == 0

    def test_a_metadata_patch_keeps_the_revision(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:patched")
        session.add_message("user", "one")
        manager.save(session)

        assert SessionManager(tmp_path).append_metadata_patch(session.key, {"archived": True})

        loaded = SessionManager(tmp_path).get_or_create(session.key)
        assert loaded.revision == session.revision

    def test_a_fork_starts_its_own_log(self, tmp_path: Path) -> None:
        manager = SessionManager(tmp_path)
        parent = manager.get_or_create("cli:parent")
        parent.add_message("user", "question")
        manager.save(parent)

        child = manager.fork(parent.key)

        assert child is not None
        assert child.log_id != parent.log_id
        assert child.log_generation == 0


def test_a_metadata_patch_alone_neither_reloads_nor_retires_the_scope(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("tui:patch-only")
    session.add_message("user", "one")
    manager.save(session)
    scope, truncations = session.revision.scope, session.truncations

    SessionManager(tmp_path).append_metadata_patch(session.key, {"archived": True})
    session.add_message("assistant", "two")
    manager.save(session)

    assert session.revision.scope == scope
    assert session.truncations == truncations


def test_a_clear_stamps_its_start_in_utc() -> None:
    session = Session(key="tui:utc")
    session.clear()

    assert datetime.fromisoformat(session.log_started_at).utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"log_id": ""},
        {"log_id": "bad#scope"},
        {"generation": -1},
        {"generation": True},
    ],
)
def test_invalid_revision_values_are_rejected(kwargs: dict) -> None:
    values = {"log_id": "a" * 32, "generation": 0} | kwargs
    with pytest.raises(ValueError):
        SessionRevision(**values)


async def test_a_turn_hands_the_history_strategy_the_sessions_revision(tmp_path: Path, monkeypatch) -> None:
    """One writer fills ``TurnContext.session_revision`` (the loop) and one copies
    it into ``AssemblyContext`` (the assembler). Without it the default strategy
    takes its unscoped branch every turn -- trim only, no prune, fold or refs --
    and nothing else would notice."""
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import ToolWiring, TurnPolicy
    from raven.context_engine.history.compacting import CompactingHistoryBuilder
    from raven.providers.base import LLMProvider, LLMResponse
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    class _Stub(LLMProvider):
        def get_default_model(self) -> str:
            return "stub"

        async def chat(self, *args, **kwargs):
            return LLMResponse(content="ok", finish_reason="stop")

    seen: list[SessionRevision | None] = []
    original = CompactingHistoryBuilder.build

    async def recording_build(self, ctx):
        seen.append(ctx.session_revision)
        return await original(self, ctx)

    monkeypatch.setattr(CompactingHistoryBuilder, "build", recording_build)
    loop = AgentLoop(
        provider=_Stub(api_key="test"),
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    await loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="hello",
        ),
        session_key="s1",
    )

    assert seen, "the default strategy's builder never ran"
    assert seen[0] is not None
    assert seen[0] == loop.sessions.get_or_create("s1").revision
