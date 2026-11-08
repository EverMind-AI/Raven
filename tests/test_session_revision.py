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
from datetime import datetime, timedelta, timezone
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


class TestTwoWriters:
    def _seeded(self, tmp_path: Path, key: str) -> Session:
        seed = SessionManager(tmp_path)
        session = seed.get_or_create(key)
        _three_messages(session)
        seed.save(session)
        return session

    def test_the_generation_is_the_larger_of_the_two(self, tmp_path: Path) -> None:
        seeded = self._seeded(tmp_path, "tui:two-undo")
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        undoer = first_manager.get_or_create(seeded.key)
        appender = second_manager.get_or_create(seeded.key)

        undoer.undo_last_turn()
        first_manager.save(undoer)
        appender.add_message("assistant", "a later reply")
        second_manager.save(appender)

        assert appender.log_id == undoer.log_id
        assert appender.log_generation == 1
        assert SessionManager(tmp_path).get_or_create(seeded.key).log_generation == 1

    def test_a_stale_writer_never_lowers_the_generation(self, tmp_path: Path) -> None:
        seeded = self._seeded(tmp_path, "tui:no-lowering")
        stale_manager, manager = SessionManager(tmp_path), SessionManager(tmp_path)
        stale = stale_manager.get_or_create(seeded.key)
        session = manager.get_or_create(seeded.key)
        session.undo_last_turn()
        session.add_message("user", "again")
        session.undo_last_turn()
        manager.save(session)
        assert session.log_generation == 2

        stale.add_message("assistant", "late")
        stale_manager.save(stale)

        assert stale.log_generation == 2
        assert _last_metadata(stale_manager, seeded.key)["log_generation"] == 2

    def test_the_log_id_follows_the_most_recent_clear(self, tmp_path: Path) -> None:
        seeded = self._seeded(tmp_path, "tui:two-clear")
        clearer_manager, stale_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        clearer = clearer_manager.get_or_create(seeded.key)
        stale = stale_manager.get_or_create(seeded.key)

        clearer.clear()
        clearer.add_message("user", "fresh start")
        clearer_manager.save(clearer)
        stale.add_message("assistant", "late reply to the old log")
        stale_manager.save(stale)

        assert stale.log_id == clearer.log_id != seeded.log_id
        assert SessionManager(tmp_path).get_or_create(seeded.key).log_id == clearer.log_id
        # Sharing the scope is right only because the stale copy now holds the
        # file's list, of which the clearer's is a prefix.
        assert stale.revision.scope == clearer.revision.scope
        assert (
            _contents(stale.messages)
            == _disk_contents(stale_manager, seeded.key)
            == ["fresh start", "late reply to the old log"]
        )

    def test_a_later_clear_wins_over_an_earlier_one(self, tmp_path: Path) -> None:
        seeded = self._seeded(tmp_path, "tui:clear-race")
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        first = first_manager.get_or_create(seeded.key)
        second = second_manager.get_or_create(seeded.key)

        first.clear()
        second.clear()
        second.add_message("user", "second's log")
        second_manager.save(second)
        first.add_message("user", "first's log")
        first_manager.save(first)

        assert first.log_id == second.log_id
        assert first.log_generation > second.log_generation
        assert SessionManager(tmp_path).get_or_create(seeded.key).revision == first.revision

    def test_a_writer_that_clears_after_the_other_keeps_its_own_id(self, tmp_path: Path) -> None:
        seeded = self._seeded(tmp_path, "tui:clear-after")
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        first = first_manager.get_or_create(seeded.key)
        second = second_manager.get_or_create(seeded.key)

        first.clear()
        first_manager.save(first)
        second.clear()
        second_manager.save(second)

        assert second.log_id != first.log_id
        assert _last_metadata(second_manager, seeded.key)["log_id"] == second.log_id

    def test_two_concurrent_undos_do_not_share_a_scope(self, tmp_path: Path) -> None:
        """Both rewrite the file from generation 0; equal generations would let
        the first writer's refs name the second writer's messages."""
        seeded = self._seeded(tmp_path, "tui:two-undos")
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        first = first_manager.get_or_create(seeded.key)
        second = second_manager.get_or_create(seeded.key)

        first.undo_last_turn()
        first.add_message("user", "first's replacement")
        first_manager.save(first)
        second.undo_last_turn()
        second.add_message("user", "second's replacement")
        second_manager.save(second)

        assert first.revision.scope != second.revision.scope
        assert second.log_generation == 2

    def test_two_first_writers_converge_on_one_log_id(self, tmp_path: Path) -> None:
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        first = first_manager.get_or_create("tui:race")
        second = second_manager.get_or_create("tui:race")
        assert first.log_id != second.log_id
        first.add_message("user", "first")
        second.add_message("user", "second")

        first_manager.save(first)
        second_manager.save(second)

        assert second.log_id == first.log_id
        assert SessionManager(tmp_path).get_or_create("tui:race").log_id == first.log_id

    def test_a_merge_that_changes_nothing_does_not_rescan(self, tmp_path: Path, monkeypatch) -> None:
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:no-scan")
        session.add_message("user", "one")
        manager.save(session)

        def fail_if_scanned(path):
            raise AssertionError(f"unexpected full scan of {path}")

        monkeypatch.setattr(manager, "_scan_file", fail_if_scanned)
        session.undo_last_turn()
        session.add_message("user", "two")
        manager.save(session)

        assert session.log_generation == 1


class TestStaleRefsAcrossWriters:
    async def test_a_ref_minted_before_another_writers_undo_is_refused(self, tmp_path: Path) -> None:
        seed = SessionManager(tmp_path)
        session = seed.get_or_create("tui:ref-undo")
        _three_messages(session)
        seed.save(session)
        stale = session_ref(session.revision, 2).render()

        writer = SessionManager(tmp_path)
        copy = writer.get_or_create(session.key)
        copy.undo_last_turn()
        copy.add_message("user", "a different message at index two")
        writer.save(copy)
        reader = SessionManager(tmp_path)

        out = await _retrieve(reader, session.key, stale)

        assert "was rewritten" in out
        assert "a different message" not in out

    async def test_a_ref_minted_before_another_writers_clear_is_refused(self, tmp_path: Path) -> None:
        seed = SessionManager(tmp_path)
        session = seed.get_or_create("tui:ref-clear")
        _three_messages(session)
        seed.save(session)
        stale = session_ref(session.revision, 0).render()

        writer = SessionManager(tmp_path)
        copy = writer.get_or_create(session.key)
        copy.clear()
        copy.add_message("user", "the new log's first message")
        writer.save(copy)

        session.add_message("assistant", "the stale copy saves after the clear")
        seed.save(session)

        out = await _retrieve(seed, session.key, stale)

        assert "not part of this conversation" in out
        assert "the new log's first message" not in out
        assert session.log_id == copy.log_id

    async def test_a_ref_minted_after_the_merge_resolves(self, tmp_path: Path) -> None:
        seed = SessionManager(tmp_path)
        session = seed.get_or_create("tui:ref-fresh")
        _three_messages(session)
        seed.save(session)
        other = SessionManager(tmp_path)
        copy = other.get_or_create(session.key)
        copy.undo_last_turn()
        other.save(copy)

        session.add_message("assistant", "after the merge")
        seed.save(session)

        await _assert_live_and_disk_agree(seed, session.key)
        assert _contents(session.messages) == ["one", "first", "after the merge"]


class TestLiveCopyMatchesTheFileAfterAMerge:
    """After any save, the copy that saved and the file agree on every ordinal of
    its scope, and no scope names two different messages."""

    async def test_an_append_after_another_writers_undo(self, tmp_path: Path) -> None:
        seed = SessionManager(tmp_path)
        appender = seed.get_or_create("tui:undo-append")
        _three_messages(appender)
        seed.save(appender)
        other = SessionManager(tmp_path)
        undoer = other.get_or_create(appender.key)
        undoer.undo_last_turn()
        other.save(undoer)
        before = appender.revision

        appender.add_message("assistant", "after the merge")
        seed.save(appender)

        assert _contents(appender.messages) == ["one", "first", "after the merge"]
        assert appender.truncations > 0, "a reader holding indices into the old list has to notice"
        await _assert_live_and_disk_agree(seed, appender.key)
        assert before.scope != appender.revision.scope

        undoer.add_message("user", "the undoer's next turn")
        other.save(undoer)

        # Every ordinal the undoer had persisted still names the same message,
        # so it keeps the scope; the appender's view is a prefix of the file.
        await _assert_live_and_disk_agree(other, appender.key)
        assert undoer.revision.scope == appender.revision.scope
        assert _contents(undoer.messages) == ["one", "first", "after the merge", "the undoer's next turn"]
        assert _contents(undoer.messages)[: len(appender.messages)] == _contents(appender.messages)

    async def test_an_append_after_another_writers_clear(self, tmp_path: Path) -> None:
        seed = SessionManager(tmp_path)
        stale = seed.get_or_create("tui:clear-append")
        _three_messages(stale)
        seed.save(stale)
        clearer_manager = SessionManager(tmp_path)
        clearer = clearer_manager.get_or_create(stale.key)
        clearer.clear()
        clearer.add_message("user", "fresh start")
        clearer_manager.save(clearer)

        stale.add_message("assistant", "late")
        seed.save(stale)

        assert _contents(stale.messages) == ["fresh start", "late"]
        await _assert_live_and_disk_agree(seed, stale.key)
        out = await _retrieve(clearer_manager, stale.key, session_ref(stale.revision, 0).render())
        assert out.split("\n", 1)[1] == "fresh start"

    async def test_two_plain_appenders(self, tmp_path: Path) -> None:
        first_manager = SessionManager(tmp_path)
        first = first_manager.get_or_create("tui:appenders")
        first.add_message("user", "u0")
        first_manager.save(first)
        second_manager = SessionManager(tmp_path)
        second = second_manager.get_or_create(first.key)
        first.add_message("assistant", "A1")
        first_manager.save(first)
        minted_by_second = second.revision

        second.add_message("assistant", "B1")
        second_manager.save(second)

        assert _contents(second.messages) == ["u0", "A1", "B1"]
        assert second.truncations > 0, "the unsaved tail moved, and a reader holding its index has to notice"
        await _assert_live_and_disk_agree(second_manager, first.key)
        # Refs are minted only into persisted history, and ordinal 0 still
        # names u0, so the scope is kept and a ref from before the save holds.
        assert second.revision.scope == minted_by_second.scope
        out = await _retrieve(second_manager, first.key, session_ref(minted_by_second, 0).render())
        assert out.split("\n", 1)[1] == "u0"

    async def test_a_ref_into_persisted_history_survives_a_concurrent_question(self, tmp_path: Path) -> None:
        mine = SessionManager(tmp_path)
        session = mine.get_or_create("tui:keep-refs")
        for index in range(4):
            session.add_message("user" if index % 2 == 0 else "assistant", f"m{index}")
        mine.save(session)
        theirs_manager = SessionManager(tmp_path)
        theirs = theirs_manager.get_or_create(session.key)
        theirs.add_message("user", "their question")
        theirs_manager.save(theirs)
        ref = session_ref(session.revision, 1).render()

        session.add_message("user", "my question")
        mine.save(session)

        assert _contents(session.messages) == ["m0", "m1", "m2", "m3", "their question", "my question"]
        assert (await _retrieve(mine, session.key, ref)).split("\n", 1)[1] == "m1"
        await _assert_live_and_disk_agree(mine, session.key)

    async def test_a_persisted_ordinal_that_changed_under_the_same_scope_retires_it(self, tmp_path: Path) -> None:
        """Nothing in this store rewrites the file without moving the revision,
        but a hand edit or an older writer can, and then the scope must go."""
        manager = SessionManager(tmp_path)
        session = manager.get_or_create("tui:hand-edit")
        _three_messages(session)
        manager.save(session)
        before = session.revision
        path = manager.session_path(session.key)
        rows = _records(manager, session.key)
        rows[2]["content"] = "edited by hand"
        del rows[3]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        session.add_message("assistant", "reply")
        manager.save(session)

        assert session.revision.scope != before.scope
        assert _contents(session.messages) == ["one", "edited by hand", "reply"]
        await _assert_live_and_disk_agree(manager, session.key)

    async def test_an_append_that_only_extends_keeps_its_scope(self, tmp_path: Path) -> None:
        first_manager = SessionManager(tmp_path)
        first = first_manager.get_or_create("tui:extend")
        first.add_message("user", "u0")
        first_manager.save(first)
        reader_manager = SessionManager(tmp_path)
        reader = reader_manager.get_or_create(first.key)
        first.add_message("assistant", "A1")
        first_manager.save(first)
        scope = reader.revision.scope

        reader_manager.save(reader)

        assert reader.revision.scope == scope
        assert _contents(reader.messages) == ["u0", "A1"]
        await _assert_live_and_disk_agree(reader_manager, first.key)

    async def test_two_loaders_of_a_legacy_file(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions" / "tui" / "old.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {"_type": "metadata", "key": "tui:old", "created_at": "2026-01-01T10:00:00", "metadata": {}},
            {"role": "user", "content": "old"},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        first_manager, second_manager = SessionManager(tmp_path), SessionManager(tmp_path)
        first = first_manager.get_or_create("tui:old")
        second = second_manager.get_or_create("tui:old")
        assert first.log_id == second.log_id

        first.add_message("assistant", "first's reply")
        first_manager.save(first)
        second.add_message("assistant", "second's reply")
        second_manager.save(second)

        assert second.revision.scope == first.revision.scope
        assert _contents(second.messages) == ["old", "first's reply", "second's reply"]
        await _assert_live_and_disk_agree(second_manager, "tui:old")

    def test_a_metadata_patch_alone_neither_reloads_nor_retires_the_scope(self, tmp_path: Path) -> None:
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


class TestLoadRace:
    def test_a_rewrite_landing_while_a_copy_loads_is_merged_on_its_next_save(self, tmp_path: Path, monkeypatch) -> None:
        """The stamp is the file this copy read, not whatever replaced it a moment
        later: otherwise the next save skips the merge and writes the old
        generation back over the newer one."""
        import builtins
        import shutil

        import raven.session.manager as manager_module

        seed = SessionManager(tmp_path)
        session = seed.get_or_create("tui:load-race")
        for role, content in (("user", "one"), ("assistant", "first"), ("user", "two"), ("assistant", "second")):
            session.add_message(role, content)
        seed.save(session)
        writer = SessionManager(tmp_path)
        rewriting = writer.get_or_create(session.key)
        fired: list[bool] = []

        snapshot = tmp_path / "opened-before-the-rewrite.jsonl"

        def racing_open(file, *args, **kwargs):
            if fired:
                return builtins.open(file, *args, **kwargs)
            fired.append(True)
            # The file as this copy opened it, size and mtime kept: what an open
            # handle on POSIX goes on reading after the path is replaced. Read
            # from a copy because Windows refuses to replace a file that has an
            # open handle, which would turn the race into a failed rewrite.
            shutil.copy2(file, snapshot)
            rewriting.undo_last_turn()
            rewriting.add_message("user", "replaced")
            writer.save(rewriting)
            return builtins.open(snapshot, *args, **kwargs)

        monkeypatch.setattr(manager_module, "open", racing_open, raising=False)
        loader = SessionManager(tmp_path)
        loaded = loader.get_or_create(session.key)
        monkeypatch.undo()
        assert fired and loaded.log_generation == 0

        loaded.add_message("assistant", "late")
        loader.save(loaded)

        generations = [row["log_generation"] for row in _records(loader, session.key) if row.get("_type") == "metadata"]
        assert generations == sorted(generations)
        assert loaded.log_generation >= rewriting.log_generation
        assert _contents(loaded.messages) == _disk_contents(loader, session.key)


def test_a_clear_stamps_its_start_in_utc() -> None:
    session = Session(key="tui:utc")
    session.clear()

    assert datetime.fromisoformat(session.log_started_at).utcoffset() == timedelta(0)


def test_a_naive_start_from_an_earlier_write_still_compares() -> None:
    from raven.session.manager import _started

    assert _started("2026-10-20T10:00:00") < _started(datetime.now(timezone.utc).isoformat())
    assert _started(None) < _started("2026-10-20T10:00:00+00:00")
    assert _started("not a time") == _started(None)


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
