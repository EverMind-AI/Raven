"""The ``task_board`` tool's recall face, and the per-session service it reads through."""

from __future__ import annotations

import pytest

from raven.agent.task_board.board import Board, Dispatch, ProgressStep, TaskGroup, UpdateRejectedError, apply_update
from raven.agent.task_board.notes import NOTES_FILENAME
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.store import BOARD_FILENAME, copy_board, load_board, reset_board, save_board
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
        {
            "steps": [
                {
                    "new_title": title,
                    "new_summary": f"Where {title} stands",
                    "title": text,
                    "summary": f"In detail: {text}",
                    "status": "needs_you",
                    "questions": [f"Is {text} right?"],
                    "tasks": [spawn.handle],
                }
            ]
        },
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
    assert set(params["properties"]) == {"action", "query", "group_id", "include_turns", "steps", "groups"}
    # The update's shape is the post-turn instruction's to state, once per turn,
    # not this schema's to carry on every call of every turn.
    assert params["properties"]["steps"] == {"type": "array", "items": {"type": "object"}}
    assert params["properties"]["groups"] == {"type": "array", "items": {"type": "object"}}


def test_the_description_keeps_the_model_off_update(sessions):
    # A live glm-5.3-flash turn called update mid-turn when the description only
    # said the board was kept for it; the refusal cost an iteration and the reply
    # then claimed the board was updated.
    assert "never call action=update" in _tool(sessions).description


def test_the_description_says_recall_lists_the_questions_still_open(sessions):
    assert "questions" in _tool(sessions).description


async def test_update_during_a_turn_is_refused(sessions):
    tool = _tool(sessions)

    result = await tool.execute(
        action="update",
        steps=[{"new_title": "x", "new_summary": "x", "title": "y", "summary": "y", "status": "idle"}],
    )

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
    assert by_id.splitlines()[0] == f"{gid} [needs_you] Fix bug A -- Where Fix bug A stands"
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


async def test_recall_lists_a_question_until_the_user_sets_it_aside(sessions):
    board = _board_with("Fix bug A", "Found it")
    save_board(sessions.session_dir(KEY), board)
    tool = _tool(sessions)
    group = board.groups[0]

    assert "  1. Is Found it right?" in await tool.execute(action="recall")
    assert "\n1. Is Found it right?" in await tool.execute(action="recall", group_id=group.id)

    await TaskBoardService(sessions).ignore_question(KEY, group.id, group.steps[-1].id, "Is Found it right?")

    out = await tool.execute(action="recall")
    assert "Is Found it right?" not in out and "Unanswered" not in out


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
        await service.set_status(KEY, gid, "running")
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


def _waiting(gid: str, title: str, questions: list[str]) -> TaskGroup:
    step = ProgressStep(f"s_{gid}", "2026-10-29T10:02:13", None, "Asked", "Asked the user", questions=list(questions))
    return TaskGroup(gid, title, "", "needs_you", 1, 1, [step])


async def test_a_question_set_aside_is_noted_for_the_next_turn_until_a_turn_has_shown_it(sessions):
    service = TaskBoardService(sessions)
    save_board(
        sessions.session_dir(KEY), Board([_waiting("g_a", "Fix bug A", ["Which fix?", "Ship it\n\ntoday?"])], [])
    )

    await service.ignore_question(KEY, "g_a", "s_g_a", "Ship it\n\ntoday?")
    await service.ignore_question(KEY, "g_a", "s_g_a", "Not on the step?")

    notes = service.turn_notes(KEY)
    assert notes == [
        'Task board: the user set aside "Ship it today?" (Fix bug A) without answering it; it is declined, not open.'
    ]
    assert service.turn_notes(KEY) == notes, "read again within the same turn, the notes stay"
    await service.ack_turn_notes(KEY)
    assert service.turn_notes(KEY) == []
    assert not (sessions.session_dir(KEY) / NOTES_FILENAME).exists()


async def test_setting_a_waiting_group_aside_is_noted_and_marking_or_reopening_is_not(sessions):
    service = TaskBoardService(sessions)
    groups = [_waiting("g_a", "Fix bug A", []), _waiting("g_b", "Release notes", ["Which date?"])]
    save_board(sessions.session_dir(KEY), Board(groups, []))

    await service.set_status(KEY, "g_a", "idle")
    await service.set_status(KEY, "g_b", "completed")
    await service.set_status(KEY, "g_b", "idle")

    assert service.turn_notes(KEY) == [
        'Task board: the user set aside what "Fix bug A" asked, without answering it; it is declined, not open.'
    ]


async def test_a_note_made_after_a_turn_read_the_notes_waits_for_the_next_turn(sessions):
    service = TaskBoardService(sessions)
    save_board(sessions.session_dir(KEY), Board([_waiting("g_a", "Fix bug A", ["Which fix?", "Which date?"])], []))
    await service.ignore_question(KEY, "g_a", "s_g_a", "Which fix?")
    assert len(service.turn_notes(KEY)) == 1

    await service.ignore_question(KEY, "g_a", "s_g_a", "Which date?")
    await service.ack_turn_notes(KEY)

    assert service.turn_notes(KEY) == [
        'Task board: the user set aside "Which date?" (Fix bug A) without answering it; it is declined, not open.'
    ]


async def test_clearing_a_conversation_drops_its_notes_and_a_branch_takes_them_along(sessions):
    service = TaskBoardService(sessions)
    save_board(sessions.session_dir(KEY), Board([_waiting("g_a", "Fix bug A", ["Which fix?"])], []))
    await service.ignore_question(KEY, "g_a", "s_g_a", "Which fix?")

    copy_board(sessions.session_dir(KEY), sessions.session_dir(OTHER))
    assert len(service.turn_notes(OTHER)) == 1
    await service.reset(KEY)
    assert service.turn_notes(KEY) == []
    reset_board(sessions.session_dir(OTHER))
    assert service.turn_notes(OTHER) == []


async def test_moving_a_group_on_tells_the_next_turn_what_the_message_is_about(sessions):
    service = TaskBoardService(sessions)
    fix = _waiting("g_a", "Fix bug A", [])
    fix.summary = "Cause found:\n the restored session's token is empty."
    save_board(sessions.session_dir(KEY), Board([fix, _waiting("g_b", "Release notes", [])], []))

    await service.move_on(KEY, "g_b")
    assert service.turn_notes(KEY) == ['Task board: the user\'s message moves on "Release notes" (g_b).']
    await service.move_on(KEY, "g_a")

    assert service.turn_notes(KEY) == [
        'Task board: the user\'s message moves on "Fix bug A" (g_a); '
        "where it stands: Cause found: the restored session's token is empty."
    ], "a later move-on replaces the one not yet shown"
    await service.ack_turn_notes(KEY)
    assert service.turn_notes(KEY) == []


async def test_moving_on_a_group_the_board_lacks_is_refused(sessions):
    service = TaskBoardService(sessions)
    save_board(sessions.session_dir(KEY), Board([_waiting("g_a", "Fix bug A", [])], []))

    with pytest.raises(KeyError):
        await service.move_on(KEY, "g_nope")
    assert service.turn_notes(KEY) == []
