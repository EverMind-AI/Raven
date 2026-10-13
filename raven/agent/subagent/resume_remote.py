"""Runs left on registered machines by a process before this one, picked up and reported to their sessions.

Once per process, soon after start: every record a run on a machine left
behind (:mod:`raven.acp_client.remote_runs`) is a turn this computer still
owes an answer for. Its reply is read off the machine
(:func:`raven.acp_client.remote_resume.pick_up`), the dispatch's record under
the session is finished with it, the registry row and the node index are
closed, and the result is announced into the conversation that asked, as a
turn that ended here would have been. A machine that cannot be asked keeps
its record for the next start.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from raven.acp_client import remote_resume, remote_runs
from raven.acp_client.acp_agent import AcpAgentBackend
from raven.agent.subagent.history import SpawnRecord, nodes_root
from raven.agent.subagent.instances import get_registry

#: How long after start the pick-up begins: the channels and the page are up by then, so an announce has a reader.
START_DELAY_S = 2.0

_SCHEDULED = False
_TASKS: set[asyncio.Task[Any]] = set()


def schedule_remote_resume(manager: Any, *, delay_s: float = START_DELAY_S) -> asyncio.Task[Any] | None:
    """Pick up the runs left on machines, once per process, in the background."""
    global _SCHEDULED
    if _SCHEDULED or not hasattr(manager, "session_dir_for"):
        return None
    _SCHEDULED = True

    async def _later() -> None:
        await asyncio.sleep(delay_s)
        try:
            await resume_remote_runs(manager)
        except Exception:  # noqa: BLE001 - a pick-up that fails must not take the host down
            logger.opt(exception=True).warning("picking up the runs left on machines failed")

    task = asyncio.create_task(_later(), name="remote-resume")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def resume_remote_runs(manager: Any) -> int:
    """Every run left on a machine, picked up and reported; how many were concluded here."""
    concluded = 0
    for run in remote_runs.pending():
        backend = _backend_of(manager, run)
        if backend is None:
            await _conclude(
                manager,
                run,
                "error",
                f"the run of {run.agent!r} left on machine {run.machine} cannot be picked up: the agent is no "
                "longer on the agent table as an acp agent on that machine; its frames are on the machine, "
                f"task {run.task}",
            )
            concluded += 1
            continue
        try:
            picked = await remote_resume.pick_up(run, backend)
        except remote_resume.UnreachableError as exc:
            logger.warning("the run of {!r} on machine {} waits for a later start: {}", run.agent, run.machine, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one run's failure is reported as its own, the next is tried
            logger.opt(exception=True).warning(
                "picking up the run of {!r} on machine {} failed", run.agent, run.machine
            )
            picked = remote_resume.PickedUp("error", f"the run of {run.agent!r} could not be picked up: {exc}")
        await _conclude(manager, run, picked.status, picked.reply)
        concluded += 1
    return concluded


def _backend_of(manager: Any, run: remote_runs.RemoteRun) -> AcpAgentBackend | None:
    registry = getattr(manager, "registry", None)
    backend = registry.backend(run.agent) if registry is not None else None
    if not isinstance(backend, AcpAgentBackend) or backend.machine != run.machine:
        return None
    return backend


async def _conclude(manager: Any, run: remote_runs.RemoteRun, status: str, reply: str) -> None:
    """Close what the dispatch left open here and announce the outcome into its conversation."""
    remote_runs.forget(run.task)
    ok = status == "ok"
    session_dir = Path(manager.session_dir_for(run.session_key))
    record, meta = _spawn_record(session_dir, run)
    if record is not None:
        if ok:
            record.finish(status="completed", output=reply)
        else:
            record.finish(status="failed", error=f"Error: {reply}")
        await manager._finalize_node_claim(run.session_key, record)
    try:
        if ok and run.stateful:
            await get_registry().commit(run.session_key, run.agent, run.handle, run.session_id, kind="acp")
        else:
            await get_registry().upsert_spawn(run.session_key, run.agent, run.handle, "completed" if ok else "failed")
    except Exception:  # noqa: BLE001 - the registry row must not keep the result from its reader
        logger.opt(exception=True).warning("the registry row of {}/{} could not be closed", run.agent, run.handle)
    channel, sep, chat_id = run.session_key.partition(":")
    if not (sep and channel and chat_id):
        logger.warning("the run of {!r} has no conversation to report to (session {!r})", run.agent, run.session_key)
        return
    manager.remember_session(run.session_key)
    origin = {"session_key": run.session_key, "channel": channel, "chat_id": chat_id}
    summary = str(meta.get("task_summary") or f"{run.agent} on machine {run.machine}")
    task_text = ""
    if record is not None:
        try:
            task_text = record.file("prompt.md").read_text(encoding="utf-8")
        except OSError:
            task_text = ""
    await manager._announce_result(
        run.task_id,
        summary,
        task_text,
        reply if ok else f"Error: {reply}",
        origin,
        "ok" if ok else "error",
        record_path=str(record.file("out.md")) if record is not None else None,
    )


def _spawn_record(session_dir: Path, run: remote_runs.RemoteRun) -> tuple[SpawnRecord | None, dict[str, Any]]:
    """The dispatch's record under the session, still open: the latest of this agent and handle left running."""
    root = nodes_root(session_dir)
    best: tuple[int, str, dict[str, Any]] | None = None
    for path in root.glob("*.meta.json") if root.is_dir() else []:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get("status") != "running":
            continue
        if meta.get("agent") != run.agent or str(meta.get("handle") or "") != run.handle:
            continue
        started = int(meta.get("started_at_ms") or 0)
        if best is None or started > best[0]:
            best = (started, path.name[: -len(".meta.json")], meta)
    if best is None:
        return None, {}
    _, node_id, meta = best
    task = ""
    try:
        task = (root / f"{node_id}.prompt.md").read_text(encoding="utf-8")
    except OSError:
        pass
    return SpawnRecord(root, session_dir=session_dir, task=task, node_id=node_id), meta


__all__ = ["START_DELAY_S", "resume_remote_runs", "schedule_remote_resume"]
