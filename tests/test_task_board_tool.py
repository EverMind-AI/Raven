"""The ``task_board`` tool's recall face, and the per-session service it reads through."""

from __future__ import annotations

import pytest

from raven.agent.task_board.board import Board, Dispatch, UpdateRejectedError, apply_update
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.store import BOARD_FILENAME, load_board, save_board
from raven.agent.task_board.tool import REFUSED_UPDATE, TOOL_NAME, TaskBoardTool
from raven.session.manager import SessionManager

KEY = "web:alpha"
OTHER = "web:beta"


@pytest.fixture
def sessions(tmp_path):
    return SessionManager(tmp_path)


def _board_with(title: str, text: str, turn_at: str = "2026-10-29T10:02:13") -> Board:
    spawn = Dispatch("spawn:locate_login_bug", "Locate the blank-page bug")
    return apply_update(
        Board([], []),
        {"steps": [{"new_title": title, "text": text, "status": "waiting", "tasks": [spawn.handle]}]},
        [spawn],
        turn_at=turn_at,
        turn_id=None,
        now_ms=1,
    )


def _tool(sessions, key: str = KEY) -> TaskBoardTool:
    tool = TaskBoardTool(TaskBoardService(sessions))
    tool.set_context("web", key.split(":", 1)[1], key)
    return tool


def test_the_schema_is_the_small_face_both_actions_share(sessions):
    tool = _tool(sessions)
    params = tool.parameters

    assert tool.name == TOOL_NAME == "task_board"
    assert params["required"] == ["action"]
    assert params["properties"]["action"]["enum"] == ["recall", "update"]
    assert set(params["properties"]) == {"action", "query", "group_id", "include_turns", "steps", "statuses"}
    # The update's shape is the post-turn instruction's to state, once per turn,
    # not this schema's to carry on every call of every turn.
    assert params["properties"]["steps"] == {"type": "array", "items": {"type": "object"}}
    assert params["properties"]["statuses"] == {"type": "array", "items": {"type": "object"}}


def test_the_description_keeps_the_model_off_update(sessions):
    # A live glm-5.3-flash turn called update mid-turn when the description only
    # said the board was kept for it; the refusal cost an iteration and the reply
    # then claimed the board was updated.
    assert "never call action=update" in _tool(sessions).description


async def test_update_during_a_turn_is_refused(sessions):
    tool = _tool(sessions)

    result = await tool.execute(action="update", steps=[{"new_title": "x", "text": "y", "status": "active"}])

    assert result == REFUSED_UPDATE
    assert not (sessions.session_dir(KEY) / BOARD_FILENAME).exists()


async def test_recall_reads_only_the_bound_sessions_board(sessions):
    save_board(sessions.session_dir(KEY), _board_with("Fix bug A", "Found it"))
    save_board(sessions.session_dir(OTHER), _board_with("Write the release note", "Draft"))

    out = await _tool(sessions).execute(action="recall")

    assert "Fix bug A" in out and "release note" not in out


async def test_recall_on_a_session_without_a_board_says_it_is_empty(sessions):
    assert "empty" in await _tool(sessions).execute(action="recall")


async def test_recall_by_group_and_by_query(sessions):
    board = _board_with("Fix bug A", "Found the empty token")
    save_board(sessions.session_dir(KEY), board)
    tool = _tool(sessions)
    gid = board.groups[0].id

    by_id = await tool.execute(action="recall", group_id=gid)
    assert by_id.splitlines()[0] == f"{gid} [waiting] Fix bug A"
    assert "spawn:locate_login_bug (Locate the blank-page bug): unknown" in by_id
    assert (await tool.execute(action="recall", query="empty token")).splitlines()[0].startswith(gid)
    assert "No group 'g_nope'" in await tool.execute(action="recall", group_id="g_nope")


async def test_recall_with_include_turns_reads_the_session_log(sessions):
    session = sessions.get_or_create(KEY)
    session.add_message("user", "Why is the page blank after login?", timestamp="2026-10-29T10:02:13")
    session.add_message("assistant", "The restored session has an empty token.", timestamp="2026-10-29T10:02:40")
    sessions.save(session)
    board = _board_with("Fix bug A", "Found it")
    save_board(sessions.session_dir(KEY), board)

    out = await _tool(sessions).execute(action="recall", group_id=board.groups[0].id, include_turns=True)

    assert "asked: Why is the page blank after login?" in out
    assert "replied: The restored session has an empty token." in out


async def test_set_status_writes_and_announces_once(sessions):
    service = TaskBoardService(sessions)
    save_board(sessions.session_dir(KEY), _board_with("Fix bug A", "Found it"))
    gid = load_board(sessions.session_dir(KEY)).groups[0].id
    heard: list[tuple[str, dict]] = []

    async def sink(conversation: str, event: dict) -> None:
        heard.append((conversation, event))

    service.set_sink(sink)
    board = await service.set_status(KEY, gid, "completed")

    assert board.groups[0].status == "completed"
    assert load_board(sessions.session_dir(KEY)).groups[0].status == "completed"
    assert len(heard) == 1
    conversation, event = heard[0]
    assert conversation == KEY and event["type"] == "tasks.board_updated"
    assert event["payload"]["session_key"] == KEY
    assert event["payload"]["board"]["groups"][0]["status"] == "completed"
    assert event["payload"]["lineage"] == {}


async def test_set_status_refuses_other_statuses_and_unknown_groups(sessions):
    service = TaskBoardService(sessions)
    save_board(sessions.session_dir(KEY), _board_with("Fix bug A", "Found it"))
    gid = load_board(sessions.session_dir(KEY)).groups[0].id

    with pytest.raises(UpdateRejectedError):
        await service.set_status(KEY, gid, "parked")
    with pytest.raises(KeyError):
        await service.set_status(KEY, "g_nope", "completed")


async def test_a_failing_sink_does_not_fail_the_write(sessions):
    service = TaskBoardService(sessions)

    async def sink(_conversation: str, _event: dict) -> None:
        raise RuntimeError("socket gone")

    service.set_sink(sink)
    await service.write(KEY, _board_with("Fix bug A", "Found it"))

    assert load_board(sessions.session_dir(KEY)).groups[0].title == "Fix bug A"


def test_one_lock_per_session(sessions):
    service = TaskBoardService(sessions)

    assert service.lock(KEY) is service.lock(KEY)
    assert service.lock(KEY) is not service.lock(OTHER)
