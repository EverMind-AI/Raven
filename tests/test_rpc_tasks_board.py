"""``tasks.board`` / ``tasks.set_group_status``, the board event, and the history edits that move the board."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import raven.home as raven_home_module
from raven.agent.subagent.dag_store import DagRunStore, ensure_node_claimed, index_guard
from raven.agent.subagent.history import SpawnRecord, dag_root, nodes_root, session_history_root
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.task_board.board import Board, Dispatch, apply_update, set_group_status
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.store import load_board, save_board
from raven.rpc.errors import InvalidParamsError
from raven.rpc.methods.session import session_branch, session_clear, session_undo
from raven.rpc.methods.tasks import (
    tasks_board,
    tasks_boards,
    tasks_ignore_question,
    tasks_list,
    tasks_move_on,
    tasks_set_group_status,
)
from raven.rpc.models import TasksBoardResult, TasksBoardsResult, TasksIgnoreQuestionResult, TasksMoveOnResult
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


@pytest.fixture(autouse=True)
def _isolated_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the real ``~/.raven/subagent_instances.json``."""
    from raven.agent.subagent import instances as instances_mod

    monkeypatch.setattr(instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "inst.json"))


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
        {
            "steps": [
                {
                    "new_title": title,
                    "new_summary": "Review of the login fix",
                    "title": "Started the review",
                    "summary": "Read the diff; two findings so far.",
                    "status": "idle",
                    "tasks": [run.handle],
                }
            ]
        },
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

    group = answer["board"]["groups"][0]
    assert (group["title"], group["summary"]) == ("Fix bug A", "Review of the login fix")
    assert group["steps"][0]["title"] == "Started the review"
    assert group["steps"][0]["summary"] == "Read the diff; two findings so far."
    assert answer["lineage"] == {"20261029T110000Z-0badf00d": RUN}
    TasksBoardResult.model_validate(answer)


async def test_a_step_s_questions_reach_the_page(workspace):
    asked = ["Which fix should I apply?"]
    first = _board()
    board = apply_update(
        first,
        {
            "steps": [
                {
                    "group": first.groups[0].id,
                    "title": "Proposed three fixes",
                    "summary": "Asked which fix to apply.",
                    "status": "needs_you",
                    "questions": asked,
                }
            ]
        },
        [],
        turn_at="2026-10-29T10:05:00",
        turn_id=None,
        now_ms=2,
    )
    save_board(_dir(workspace), board)

    answer = await tasks_board({"session_key": KEY})

    steps = answer["board"]["groups"][0]["steps"]
    assert [s["questions"] for s in steps] == [[], asked]
    TasksBoardResult.model_validate(answer)


async def test_ignore_question_writes_and_emits_the_event(workspace):
    loop = _Loop(workspace)
    first = _board()
    gid = first.groups[0].id
    board = apply_update(
        first,
        {
            "steps": [
                {
                    "group": gid,
                    "title": "Asked two things",
                    "summary": "Asked which fix and whether to clear the session.",
                    "status": "needs_you",
                    "questions": ["Which fix?", "Clear the session too?"],
                }
            ]
        },
        [],
        turn_at="2026-10-29T10:05:00",
        turn_id=None,
        now_ms=2,
    )
    save_board(_dir(workspace), board)
    sid = board.groups[0].steps[-1].id

    answer = await tasks_ignore_question(
        {"session_key": KEY, "group_id": gid, "step_id": sid, "question": "Which fix?"}, agent_loop_factory=lambda: loop
    )

    assert answer["board"]["groups"][0]["steps"][-1]["questions"] == ["Clear the session too?"]
    assert load_board(_dir(workspace)).groups[0].steps[-1].questions == ["Clear the session too?"]
    assert [e["type"] for e in loop.heard] == ["tasks.board_updated"]
    TasksIgnoreQuestionResult.model_validate(answer)


async def test_ignore_question_refuses_an_unknown_step_and_a_runtime_without_a_board(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())
    gid = load_board(_dir(workspace)).groups[0].id

    with pytest.raises(InvalidParamsError):
        await tasks_ignore_question(
            {"session_key": KEY, "group_id": gid, "step_id": "s_x", "question": "Which fix?"},
            agent_loop_factory=lambda: loop,
        )
    with pytest.raises(InvalidParamsError):
        await tasks_ignore_question({"session_key": KEY, "group_id": gid, "step_id": "s_x", "question": "Q?"})


async def test_move_on_notes_the_group_for_the_next_turn(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())
    gid = load_board(_dir(workspace)).groups[0].id

    answer = await tasks_move_on({"session_key": KEY, "group_id": gid}, agent_loop_factory=lambda: loop)

    assert TasksMoveOnResult.model_validate(answer).noted is True
    assert loop.task_board.turn_notes(KEY) == [
        f'Task board: the user\'s message moves on "Fix bug A" ({gid}); where it stands: Review of the login fix'
    ]


async def test_move_on_refuses_an_unknown_group_and_a_runtime_without_a_board(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())

    with pytest.raises(InvalidParamsError):
        await tasks_move_on({"session_key": KEY, "group_id": "g_nope"}, agent_loop_factory=lambda: loop)
    with pytest.raises(InvalidParamsError):
        await tasks_move_on({"session_key": KEY, "group_id": "g_x"})


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
    for board in (answer["board"], loop.heard[0]["payload"]["board"]):
        assert board["groups"][0]["steps"][0]["summary"] == "Read the diff; two findings so far."
    assert load_board(_dir(workspace)).groups[0].steps[0].summary == "Read the diff; two findings so far."


async def test_set_group_status_refuses_anything_but_completed_and_idle(workspace):
    loop = _Loop(workspace)
    save_board(_dir(workspace), _board())
    gid = load_board(_dir(workspace)).groups[0].id

    with pytest.raises(InvalidParamsError):
        await tasks_set_group_status(
            {"session_key": KEY, "group_id": gid, "status": "running"}, agent_loop_factory=lambda: loop
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


def _two_turns(workspace: Path) -> _Loop:
    loop = _Loop(workspace)
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "first", timestamp="T1")
    session.add_message("assistant", "one", timestamp="T1a")
    session.add_message("user", "second", timestamp="T2")
    session.add_message("assistant", "two", timestamp="T2a")
    loop.sessions.save(session)
    return loop


async def test_undo_puts_back_a_group_the_undone_turn_left_waiting(workspace):
    """The undone turn asked the user and rewrote the summary; once it is gone, the
    group must not go on asking."""
    loop = _two_turns(workspace)
    first = _board(turn_at="T1")
    group_id = first.groups[0].id
    asked = {"title": "Asked", "summary": "Asked which fix.", "status": "needs_you", "questions": ["Which fix?"]}
    save_board(
        _dir(workspace),
        apply_update(
            first,
            {"steps": [{"group": group_id, **asked}], "groups": [{"group": group_id, "summary": "Which fix?"}]},
            [],
            turn_at="T2",
            turn_id=None,
            now_ms=2,
        ),
    )

    await session_undo({"session_id": KEY}, agent_loop_factory=lambda: loop)

    group = load_board(_dir(workspace)).groups[0]
    assert (group.status, group.summary, group.updated_at) == ("idle", "Review of the login fix", 1)
    assert ([s.turn_at for s in group.steps], group.revisions) == (["T1"], [])


async def test_undo_forgets_a_revision_even_when_it_puts_nothing_back(workspace):
    """The reader completed the group after the undone turn set it running: the
    status stays the reader's, and the undone turn's record still goes."""
    loop = _two_turns(workspace)
    first = _board(turn_at="T1")
    group_id = first.groups[0].id
    running = apply_update(
        first, {"groups": [{"group": group_id, "status": "running"}]}, [], turn_at="T2", turn_id=None, now_ms=2
    )
    save_board(_dir(workspace), set_group_status(running, group_id, "completed", now_ms=3))

    await session_undo({"session_id": KEY}, agent_loop_factory=lambda: loop)

    group = load_board(_dir(workspace)).groups[0]
    assert (group.status, group.updated_at, group.revisions) == ("completed", 3, [])


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


def _host_session(workspace: Path, key: str, board: Board) -> None:
    sessions = SessionManager(workspace)
    session = sessions.get_or_create(key)
    session.add_message("user", "hi", timestamp="2026-10-29T10:02:13")
    sessions.save(session)
    save_board(sessions.session_dir(key), board)


async def test_boards_lists_every_conversation_s_cards(workspace):
    _host_session(workspace, KEY, _board(title="Fix bug A"))
    _host_session(workspace, "web:beta", _board(title="Release notes"))

    answer = await tasks_boards({})

    assert sorted((c["session_key"], c["title"]) for c in answer["cards"]) == [
        ("web:alpha", "Fix bug A"),
        ("web:beta", "Release notes"),
    ]
    TasksBoardsResult.model_validate(answer)


async def test_boards_hides_completed_until_asked(workspace):
    board = _board()
    _host_session(workspace, KEY, set_group_status(board, board.groups[0].id, "completed", now_ms=2))

    hidden = await tasks_boards({})
    shown = await tasks_boards({"include_completed": True})

    assert (hidden["cards"], hidden["completed_count"]) == ([], 1)
    assert ([c["category"] for c in shown["cards"]], shown["completed_count"]) == (["completed"], 1)


_TWO_STEPS = {
    "task_summary": "two steps",
    "nodes": [
        {"id": "n1", "subagent": "Raven", "node_summary": "first", "prompt_template": "do 1", "depends_on": []},
        {"id": "n2", "subagent": "Raven", "node_summary": "second", "prompt_template": "do 2", "depends_on": ["n1"]},
    ],
}


async def _claimed_run(session_dir: Path, run_id: str = RUN) -> None:
    """A run as its start leaves it: the graph, and every node claimed ``running`` in the registry."""
    nodes_root(session_dir).mkdir(parents=True, exist_ok=True)
    store = DagRunStore(
        LocalFileBackend(),
        str(dag_root(session_dir)),
        run_id,
        nodes_root=str(nodes_root(session_dir)),
        registry_root=str(session_history_root(session_dir)),
    )
    async with index_guard(store.registry_root):
        await store.init(json.dumps(_TWO_STEPS), node_ids=["n1", "n2"])


async def _open_spawn(session_dir: Path, node_id: str = "helper_task") -> None:
    """A spawn as its start leaves it: claimed, and its record open with no outcome."""
    import time

    history_root = str(session_history_root(session_dir))
    async with index_guard(history_root):
        await ensure_node_claimed(
            LocalFileBackend(), history_root, node_id, kind="spawn", started_at_ms=int(time.time() * 1000)
        )
    SpawnRecord.open(
        session_dir,
        task_id=node_id,
        task="look into it",
        meta={"session_key": KEY, "agent": "Scribe", "instance": "review", "handle": "review", "task_summary": "Look"},
        node_id=node_id,
    )


def _spawn_board(node_id: str = "helper_task") -> Board:
    run = Dispatch(f"spawn:{node_id}", "Look into it")
    return apply_update(
        Board([], []),
        {
            "steps": [
                {
                    "new_title": "Look into it",
                    "new_summary": "Sent a helper",
                    "title": "Sent a helper",
                    "summary": "Asked a helper to look",
                    "status": "idle",
                    "tasks": [run.handle],
                }
            ]
        },
        [run],
        turn_at="2026-10-29T10:02:13",
        turn_id=None,
        now_ms=1,
    )


def _live(*, runs: frozenset = frozenset(), spawns: frozenset = frozenset()):
    """A loop that answers only the liveness questions ``tasks.list`` asks."""

    class _Manager:
        def live_handles(self, _session_key: str) -> set[tuple[str, str]]:
            return set(spawns)

    class _LiveLoop:
        subagents = _Manager()

        def active_dag_run_ids(self) -> set[str]:
            return set(runs)

    loop = _LiveLoop()
    return lambda: loop


async def test_boards_reads_a_dead_runs_claimed_nodes_as_not_running(workspace):
    """Every node of a run is claimed ``running`` at its start; a run this gateway no longer executes is not running,
    which is what the sidebar's ``tasks.list`` says of it too."""
    _host_session(workspace, KEY, _board())
    await _claimed_run(_dir(workspace))

    (card,) = (await tasks_boards({}))["cards"]
    (row,) = (await tasks_list({"session_key": KEY}))["tasks"]

    assert row["status"] == "interrupted"
    assert (card["category"], card["running_since_ms"], card["sub_agents"]) == ("idle", None, 2)


async def test_boards_reads_a_live_run_as_running_since_the_sidebar_says(workspace):
    from raven.agent.subagent.instances import get_registry

    _host_session(workspace, KEY, _board())
    await _claimed_run(_dir(workspace))
    await get_registry().upsert_dag_node(KEY, RUN, "n1", "Raven", "running")
    factory = _live(runs=frozenset({RUN}))

    (card,) = (await tasks_boards({}, agent_loop_factory=factory))["cards"]
    (row,) = (await tasks_list({"session_key": KEY}, agent_loop_factory=factory))["tasks"]

    assert row["status"] == "running"
    assert (card["category"], card["running_since_ms"]) == ("running", row["started_at"])


async def test_boards_reads_a_spawn_the_way_the_sidebar_does(workspace):
    _host_session(workspace, KEY, _spawn_board())
    await _open_spawn(_dir(workspace))

    (orphaned,) = (await tasks_boards({}))["cards"]
    live = _live(spawns=frozenset({("Scribe", "review")}))
    (running,) = (await tasks_boards({}, agent_loop_factory=live))["cards"]
    (row,) = (await tasks_list({"session_key": KEY}, agent_loop_factory=live))["tasks"]

    assert (orphaned["category"], orphaned["running_since_ms"], orphaned["sub_agents"]) == ("idle", None, 1)
    assert (running["category"], running["running_since_ms"]) == ("running", row["started_at"])


async def test_boards_reads_what_runs_on_the_loop_thread_and_files_off_it(workspace, monkeypatch):
    """The instance registry and the task index belong to the event loop; the page reads them there and builds the
    rows from files on a worker, never the other way round."""
    import threading

    from raven.agent.subagent.instances import get_registry

    _host_session(workspace, KEY, _board())
    _host_session(workspace, "web:beta", _spawn_board())
    await _claimed_run(_dir(workspace))
    await _open_spawn(_dir(workspace, "web:beta"))
    loop_thread = threading.current_thread()
    readers: list[tuple[str, bool]] = []
    registry = get_registry()
    real_list = registry.list_instances

    def recording_list(*args, **kwargs):
        readers.append(("instances", threading.current_thread() is loop_thread))
        return real_list(*args, **kwargs)

    monkeypatch.setattr(registry, "list_instances", recording_list)

    class _Manager:
        def live_handles(self, _session_key: str) -> set[tuple[str, str]]:
            readers.append(("spawns", threading.current_thread() is loop_thread))
            return set()

    class _LiveLoop:
        subagents = _Manager()

        def active_dag_run_ids(self) -> set[str]:
            readers.append(("runs", threading.current_thread() is loop_thread))
            return set()

    loop = _LiveLoop()
    cards = (await tasks_boards({}, agent_loop_factory=lambda: loop))["cards"]

    assert len(cards) == 2
    assert {what for what, _ in readers} == {"instances", "spawns", "runs"}
    assert all(on_loop for _, on_loop in readers), readers
