"""``tasks.list`` -- a conversation's delegated work as tasks.

One read that answers what the desk's tasks tab draws: a run-level row per
``spawn`` call and per ``run_subagent_dag`` run (a playbook run is one), each
carrying its nodes. ``subagent.list`` lists the same work as one row per node
and knows nothing of a run's title or edges; ``dag.get`` knows both but needs
the live graph tool. The rows are read off the stores a task leaves behind by
``raven.agent.subagent.task_rows``, the reader the Task Board's recall shares;
what this adds is the loop's word on what it is still running.

Bodies stay where they are: a node's messages, output and rendered prompt are
``dag.node`` / ``subagent.context``'s. The design is
``docs/specs/2026-09-18-desk-tasks-list-design.md``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent.instances import get_registry
from raven.agent.subagent.task_rows import dag_rows, live_spawn_handles, spawn_rows
from raven.agent.task_board.board import Board, UpdateRejectedError, board_to_dict
from raven.agent.task_board.lineage import board_lineage
from raven.agent.task_board.overview import Rows, Session
from raven.agent.task_board.overview import answer as board_answer
from raven.agent.task_board.overview import scan as scan_boards
from raven.agent.task_board.store import load_board
from raven.config.loader import load_config
from raven.rpc.errors import InvalidParamsError
from raven.rpc.methods.session import _safe_invoke_factory
from raven.rpc.methods.subagent import (
    _live_dag_run_ids,
    _session_dir,
)
from raven.session.resolve import manager_for

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

    from .turn import AgentLoopFactory


_RUN_ID_STAMP_RE = re.compile(r"^(\d{8}T\d{6})(\d{6})Z-")


def _run_id_epoch_ms(run_id: str) -> int | None:
    """The moment a run id was minted, from its UTC prefix; None for any other id."""
    match = _RUN_ID_STAMP_RE.match(run_id)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(stamp.timestamp() * 1000) + int(match.group(2)) // 1000


def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
    """Newest first. A dag row no node has started yet -- the first seconds of
    every run -- is placed by the moment its id was minted, so a just-dispatched
    run heads the list instead of falling below every finished one; a spawn row
    with no clock at all sorts last. Ties break on the id."""
    started = row.get("started_at")
    if started is None and row.get("kind") == "dag":
        started = _run_id_epoch_ms(str(row["id"]))
    return (started if isinstance(started, int) else 0, row["id"])


def _live_spawn_handles(agent_loop_factory: "AgentLoopFactory | None", session_key: str) -> set[tuple[str, str]]:
    """The (agent, handle) pairs this gateway process is still running for one session."""
    return live_spawn_handles(_safe_invoke_factory(agent_loop_factory), session_key)


async def tasks_list(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Every task one conversation started, newest first.

    An absent or unknown session is an empty list rather than an error: a
    conversation that delegated nothing and one that does not exist draw the
    same panel.
    """
    session_key = str(params.get("session_key") or "")
    if not session_key:
        return {"tasks": []}

    try:
        session_dir = _session_dir(session_key, agent_loop_factory)
    except OSError:
        return {"tasks": []}

    kind = params.get("kind")
    want = kind if kind in ("spawn", "dag") else None
    raw_id = params.get("id")
    task_id = str(raw_id) if want is not None and raw_id else None

    tasks: list[dict[str, Any]] = []
    if want in (None, "spawn"):
        live = _live_spawn_handles(agent_loop_factory, session_key)
        tasks.extend(spawn_rows(session_dir, task_id if want == "spawn" else None, live=live))
    if want in (None, "dag"):
        live_runs = _live_dag_run_ids(agent_loop_factory)
        tasks.extend(dag_rows(session_dir, session_key, task_id if want == "dag" else None, live_runs=live_runs))

    tasks.sort(key=_sort_key, reverse=True)
    return {"tasks": tasks}


def _board_answer(session_dir: Path, board: Board) -> dict[str, Any]:
    return {"board": board_to_dict(board), "lineage": board_lineage(session_dir, board)}


async def tasks_board(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """One conversation's Task Board and the lineage of the runs it places.

    Read off disk, so a page reloaded with no agent loop up still draws it; an
    absent or unknown session is an empty board, as ``tasks.list`` answers one.
    """
    session_key = str(params.get("session_key") or "")
    empty = {"board": board_to_dict(Board([], [])), "lineage": {}}
    if not session_key:
        return empty
    try:
        session_dir = _session_dir(session_key, agent_loop_factory)
    except OSError:
        return empty
    return _board_answer(session_dir, load_board(session_dir))


@dataclass(frozen=True)
class _Running:
    """What this process is running, read on the event loop for a worker thread to build rows with.

    The task index and the instance registry belong to the loop -- the registry's
    writes are serialised by an ``asyncio.Lock``, and a worker reloading its cache
    could hand a later flush a map without that write -- so they are read here,
    once per answer, and the rows are built from files alone.
    """

    runs: set[str]
    spawns: dict[str, set[tuple[str, str]]]
    instances: dict[str, list[dict[str, Any]]]

    @classmethod
    def capture(cls, agent_loop_factory: "AgentLoopFactory | None", keys: list[str]) -> "_Running":
        registry = get_registry()
        instances: dict[str, list[dict[str, Any]]] = {}
        for key in keys:
            try:
                instances[key] = registry.list_instances(key)
            except Exception:  # noqa: BLE001 - the overlay is an improvement, not a dependency
                instances[key] = []
        return cls(
            runs=_live_dag_run_ids(agent_loop_factory),
            spawns={key: _live_spawn_handles(agent_loop_factory, key) for key in keys},
            instances=instances,
        )

    def rows_of(self, session: Session) -> Rows:
        """One session's rows as ``tasks.list`` builds them, keyed ``(kind, id)``."""
        rows = spawn_rows(session.session_dir, None, live=self.spawns.get(session.key, set()))
        rows += dag_rows(
            session.session_dir,
            session.key,
            None,
            live_runs=self.runs,
            instance_rows=self.instances.get(session.key, []),
        )
        return {(row["kind"], row["id"]): row for row in rows}


async def tasks_boards(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """Every host conversation's Task Groups as cards, for the Task Board page.

    The boards are read on a worker thread (a walk plus any changed transcript,
    and the page asks every few seconds); what is running is read here, on the
    loop that owns it; and each linked run is built on a worker exactly as
    ``tasks.list`` builds it, so a card and the sidebar never disagree about
    whether something still runs.
    """
    sessions_dir = manager_for(_safe_invoke_factory(agent_loop_factory), load_config()).sessions_dir
    sessions = await asyncio.to_thread(scan_boards, sessions_dir)
    running = _Running.capture(agent_loop_factory, [s.key for s in sessions if s.links])
    return await asyncio.to_thread(
        board_answer, sessions, running.rows_of, include_completed=bool(params.get("include_completed"))
    )


async def tasks_set_group_status(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """The reader's two actions on a Task Group: mark it completed, or reopen it."""
    session_key = str(params.get("session_key") or "")
    group_id = str(params.get("group_id") or "")
    status = str(params.get("status") or "")
    loop = _safe_invoke_factory(agent_loop_factory)
    service = getattr(loop, "task_board", None) if loop is not None else None
    if service is None:
        raise InvalidParamsError("this runtime keeps no task board")
    try:
        board = await service.set_status(session_key, group_id, status)
    except KeyError:
        raise InvalidParamsError(f"no group {group_id!r} on this conversation's board") from None
    except UpdateRejectedError as exc:
        raise InvalidParamsError(str(exc)) from None
    return _board_answer(service.session_dir(session_key), board)


async def tasks_ignore_question(
    params: dict[str, Any],
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict[str, Any]:
    """The reader sets one of a step's questions aside; a group asking nothing more goes idle."""
    session_key = str(params.get("session_key") or "")
    group_id = str(params.get("group_id") or "")
    step_id = str(params.get("step_id") or "")
    question = str(params.get("question") or "")
    loop = _safe_invoke_factory(agent_loop_factory)
    service = getattr(loop, "task_board", None) if loop is not None else None
    if service is None:
        raise InvalidParamsError("this runtime keeps no task board")
    try:
        board = await service.ignore_question(session_key, group_id, step_id, question)
    except KeyError:
        raise InvalidParamsError(f"no step {step_id!r} of group {group_id!r} on this conversation's board") from None
    return _board_answer(service.session_dir(session_key), board)


def register_tasks_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    async def _list(params: dict) -> dict:
        return await tasks_list(params, agent_loop_factory=agent_loop_factory)

    async def _board(params: dict) -> dict:
        return await tasks_board(params, agent_loop_factory=agent_loop_factory)

    async def _boards(params: dict) -> dict:
        return await tasks_boards(params, agent_loop_factory=agent_loop_factory)

    async def _set_group_status(params: dict) -> dict:
        return await tasks_set_group_status(params, agent_loop_factory=agent_loop_factory)

    async def _ignore_question(params: dict) -> dict:
        return await tasks_ignore_question(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("tasks.list", _list)
    dispatcher.register("tasks.board", _board)
    dispatcher.register("tasks.boards", _boards)
    dispatcher.register("tasks.set_group_status", _set_group_status)
    dispatcher.register("tasks.ignore_question", _ignore_question)


__all__ = [
    "register_tasks_methods",
    "tasks_board",
    "tasks_boards",
    "tasks_ignore_question",
    "tasks_list",
    "tasks_set_group_status",
]
