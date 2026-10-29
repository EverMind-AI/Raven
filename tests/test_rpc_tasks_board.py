"""``tasks.board`` / ``tasks.set_group_status``, the board event, and the history edits that move the board."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import raven.home as raven_home_module
from raven.agent.subagent.history import dag_root
from raven.agent.task_board.board import Board, Dispatch, apply_update
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.store import load_board, save_board
from raven.rpc.errors import InvalidParamsError
from raven.rpc.methods.session import session_branch, session_clear, session_undo
from raven.rpc.methods.tasks import tasks_board, tasks_set_group_status
from raven.session.manager import SessionManager

KEY = "web:alpha"
RUN = "20261029T100213Z-3eb3b470"


@pytest.fixture
def workspace(tmp_path: Path):
    import raven.config.loader as loader

    previous = raven_home_module._current_config_path
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}))
    loader.set_config_path(cfg)
    yield ws
    raven_home_module._current_config_path = previous


class _Loop:
    def __init__(self, workspace: Path) -> None:
        self.sessions = SessionManager(workspace)
        self.task_board = TaskBoardService(self.sessions, now_ms=lambda: 7)
        self.heard: list[dict] = []

        async def sink(_conversation: str, event: dict) -> None:
            self.heard.append(event)

        self.task_board.set_sink(sink)


def _board(turn_at: str = "2026-10-29T10:02:13", title: str = "Fix bug A") -> Board:
    run = Dispatch(f"dag:{RUN}", "Review fixes")
    return apply_update(
        Board([], []),
        {"steps": [{"new_title": title, "text": "Started the review", "status": "active", "tasks": [run.handle]}]},
        [run],
        turn_at=turn_at,
        turn_id=None,
        now_ms=1,
    )


def _dir(workspace: Path, key: str = KEY) -> Path:
    return SessionManager(workspace).session_dir(key)


async def test_an_unknown_session_has_an_empty_board(workspace):
    for params in ({"session_key": ""}, {"session_key": "web:nobody"}):
        answer = await tasks_board(params)
        assert answer == {"board": {"version": 1, "groups": [], "unassigned": []}, "lineage": {}}


async def test_the_board_comes_with_its_lineage(workspace):
    save_board(_dir(workspace), _board())
    run_dir = dag_root(_dir(workspace)) / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "graph.json").write_text(json.dumps({"replan": {"run_id": "20261029T110000Z-0badf00d"}}))

    answer = await tasks_board({"session_key": KEY})

    assert answer["board"]["groups"][0]["title"] == "Fix bug A"
    assert answer["lineage"] == {"20261029T110000Z-0badf00d": RUN}


async def test_set_group_status_writes_and_emits_the_event(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())
    gid = load_board(_dir(workspace)).groups[0].id

    answer = await tasks_set_group_status(
        {"session_key": KEY, "group_id": gid, "status": "completed"}, agent_loop_factory=lambda: loop
    )

    assert answer["board"]["groups"][0]["status"] == "completed"
    assert load_board(_dir(workspace)).groups[0].status == "completed"
    assert [e["type"] for e in loop.heard] == ["tasks.board_updated"]


async def test_set_group_status_refuses_anything_but_completed_and_active(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())
    gid = load_board(_dir(workspace)).groups[0].id

    with pytest.raises(InvalidParamsError):
        await tasks_set_group_status(
            {"session_key": KEY, "group_id": gid, "status": "parked"}, agent_loop_factory=lambda: loop
        )
    with pytest.raises(InvalidParamsError):
        await tasks_set_group_status(
            {"session_key": KEY, "group_id": "g_nope", "status": "completed"}, agent_loop_factory=lambda: loop
        )
    with pytest.raises(InvalidParamsError):
        await tasks_set_group_status({"session_key": KEY, "group_id": gid, "status": "completed"})


async def test_clear_empties_the_board(workspace):
    loop = _Loop(workspace)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "hi", timestamp="2026-10-29T10:02:13")
    loop.sessions.save(session)
    save_board(_dir(workspace), _board())

    await session_clear({"session_id": KEY}, agent_loop_factory=lambda: loop)

    assert load_board(_dir(workspace)).groups == []
    assert [e["type"] for e in loop.heard] == ["tasks.board_updated"]


async def test_undo_drops_the_undone_turns_steps(workspace):
    loop = _Loop(workspace)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "first", timestamp="T1")
    session.add_message("assistant", "one", timestamp="T1a")
    session.add_message("user", "second", timestamp="T2")
    session.add_message("assistant", "two", timestamp="T2a")
    loop.sessions.save(session)
    kept, undone = _board(turn_at="T1", title="Kept"), _board(turn_at="T2", title="Undone")
    save_board(_dir(workspace), Board(kept.groups + undone.groups, []))

    answer = await session_undo({"session_id": KEY}, agent_loop_factory=lambda: loop)

    assert answer["removed"] == 2
    assert [g.title for g in load_board(_dir(workspace)).groups] == ["Kept"]


async def test_branch_copies_the_board_to_the_child(workspace):
    loop = _Loop(workspace)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "hi", timestamp="2026-10-29T10:02:13")
    loop.sessions.save(session)
    save_board(_dir(workspace), _board())

    answer = await session_branch({"session_id": KEY}, agent_loop_factory=lambda: loop)

    child = answer["session_id"]
    assert child and child != KEY
    assert load_board(_dir(workspace, child)).groups[0].title == "Fix bug A"


async def test_undo_keeps_the_steps_of_turns_consolidation_archived(workspace):
    """Consolidation moves old messages out of the session; an undo after it must
    drop only the turn it removed, not every step whose turn is no longer stored."""
    loop = _Loop(workspace)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "second", timestamp="T2")
    session.add_message("assistant", "two", timestamp="T2a")
    session.add_message("user", "third", timestamp="T3")
    session.add_message("assistant", "three", timestamp="T3a")
    loop.sessions.save(session)
    archived, kept, undone = (
        _board(turn_at="T1", title="Archived"),
        _board(turn_at="T2", title="Kept"),
        _board(turn_at="T3", title="Undone"),
    )
    save_board(_dir(workspace), Board(archived.groups + kept.groups + undone.groups, []))

    await session_undo({"session_id": KEY}, agent_loop_factory=lambda: loop)

    assert [g.title for g in load_board(_dir(workspace)).groups] == ["Archived", "Kept"]
