"""Runs left on registered machines by a process before this one, picked up and reported to their sessions.

Once per process, soon after start: every record a run on a machine left
behind (:mod:`raven.acp_client.remote_runs`) is a turn this computer still
owes an answer for. Its reply is read off the machine
(:func:`raven.acp_client.remote_resume.pick_up`), the dispatch's record under
the session is finished with it, the registry row and the node index are
closed, and the result is announced into the conversation that asked, as a
turn that ended here would have been. A machine that cannot be asked keeps
its record for the next start; so does a stop of this computer that lands
under a pick-up, a conclusion that fails here, or an announce the scheduler
refuses: the record goes last, once the outcome has reached its conversation.
A cli entry's run is picked up the same way, its command's whole output being
its reply (``CliAgentBackend.pick_up``). Each run is picked up under a claim on its record
(:func:`raven.acp_client.remote_runs.claim`): two processes of one Raven home
that start together pick it up once, not once each.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from raven.acp_client import remote, remote_resume, remote_runs
from raven.acp_client.acp_agent import AcpAgentBackend
from raven.agent.subagent.backends.cli_agent import CliAgentBackend
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
    """Every run left on a machine, picked up and reported; how many were concluded here.

    A run's record goes only once its outcome has reached its conversation:
    a machine that cannot be asked, a stop of this computer under the
    pick-up, a conclusion that fails here or an announce the scheduler
    refuses all leave the record, its claim released, for the next start to
    try again.
    """
    concluded = 0
    for run in remote_runs.pending():
        if remote.leaving():
            logger.info("this computer is stopping: the runs left on machines wait for the next start")
            break
        if not remote_runs.claim(run.task):
            logger.info(
                "the run of {!r} on machine {} is being picked up by another process of this computer",
                run.agent,
                run.machine,
            )
            continue
        backend = _backend_of(manager, run)
        if backend is None:
            picked = remote_resume.PickedUp(
                "error",
                f"the run of {run.agent!r} left on machine {run.machine} cannot be picked up: the agent is no "
                f"longer on the agent table as an {run.kind} agent on that machine; its frames are on the machine, "
                f"task {run.task}",
            )
        else:
            try:
                picked = await _pick_up(run, backend)
            except remote_resume.UnreachableError as exc:
                remote_runs.release(run.task)
                logger.warning("the run of {!r} on machine {} waits for a later start: {}", run.agent, run.machine, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - one run's failure is reported as its own, the next is tried
                logger.opt(exception=True).warning(
                    "picking up the run of {!r} on machine {} failed", run.agent, run.machine
                )
                picked = remote_resume.PickedUp("error", f"the run of {run.agent!r} could not be picked up: {exc}")
        try:
            taken = await _conclude(manager, run, picked.status, picked.reply)
        except Exception:  # noqa: BLE001 - the outcome is recovered and not yet reported: the next start reports it
            remote_runs.release(run.task)
            logger.opt(exception=True).warning(
                "concluding the run of {!r} on machine {} failed; its record stays for the next start",
                run.agent,
                run.machine,
            )
            continue
        except BaseException:
            # A stop under the conclusion: the record stays, and the claim
            # names a process that is about to be gone.
            remote_runs.release(run.task)
            raise
        if not taken:
            remote_runs.release(run.task)
            logger.warning(
                "the run of {!r} on machine {} was not announced, the scheduler is draining; "
                "its record stays for the next start",
                run.agent,
                run.machine,
            )
            continue
        remote_runs.forget(run.task)
        concluded += 1
    return concluded


async def _pick_up(run: remote_runs.RemoteRun, backend: Any) -> remote_resume.PickedUp:
    """The reply the run owes, read the way its kind is read: a cli entry its command's output, an acp one its turn."""
    if run.kind == "cli":
        return await backend.pick_up(run)
    return await remote_resume.pick_up(run, backend)


def _backend_of(manager: Any, run: remote_runs.RemoteRun) -> Any:
    registry = getattr(manager, "registry", None)
    backend = registry.backend(run.agent) if registry is not None else None
    wanted = CliAgentBackend if run.kind == "cli" else AcpAgentBackend
    if not isinstance(backend, wanted) or backend.machine != run.machine:
        return None
    return backend


async def _conclude(manager: Any, run: remote_runs.RemoteRun, status: str, reply: str) -> bool:
    """Close what the dispatch left open here and announce the outcome into its conversation; whether it was taken.

    The record is the caller's to remove, after this: a conclusion cut short
    leaves it for the next start.
    """
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
        if ok and run.stateful and run.session_id:
            # Where nothing is bound yet: a dispatch to the same named
            # instance that ran while the pick-up waited bound its own
            # session, and that newer binding stays (``bind_if_absent``;
            # the derived case is ``CliAgentBackend._bind_picked_up``).
            await get_registry().bind_if_absent(run.session_key, run.agent, run.handle, run.session_id, kind=run.kind)
        else:
            await get_registry().upsert_spawn(run.session_key, run.agent, run.handle, "completed" if ok else "failed")
    except Exception:  # noqa: BLE001 - the registry row must not keep the result from its reader
        logger.opt(exception=True).warning("the registry row of {}/{} could not be closed", run.agent, run.handle)
    channel, sep, chat_id = run.session_key.partition(":")
    if not (sep and channel and chat_id):
        logger.warning("the run of {!r} has no conversation to report to (session {!r})", run.agent, run.session_key)
        return True
    manager.remember_session(run.session_key)
    origin = _origin_of(run, record, meta, channel=channel, chat_id=chat_id)
    summary = str(run.task_summary or meta.get("task_summary") or f"{run.agent} on machine {run.machine}")
    # The task as the model wrote it, or the summary: never the rendered
    # prompt on disk, which a `{{ ref:... }}` spawn expands a whole file into.
    asked = str(run.authored_task or origin.get("authored_task") or summary)
    taken = await manager._announce_result(
        run.task_id,
        summary,
        asked,
        reply if ok else f"Error: {reply}",
        origin,
        "ok" if ok else "error",
        record_path=str(record.file("out.md")) if record is not None else None,
    )
    return taken is not False


def _origin_of(
    run: remote_runs.RemoteRun, record: SpawnRecord | None, meta: dict[str, Any], *, channel: str, chat_id: str
) -> dict[str, Any]:
    """The dispatch's origin for the announce: as the manager added it to the record, or made up from what is left.

    What ``spawn`` builds -- instance, handle, node, tool call, the task as
    authored -- the manager adds to the record when the run is left
    (:func:`raven.acp_client.remote_runs.annotate`). A host killed outright
    never did: then the spawn record names the node, a handle that is not
    the dispatch's id is the instance, and the conversation is read off the
    session key.
    """
    origin: dict[str, Any] = dict(run.origin) if isinstance(run.origin, dict) else {}
    origin.setdefault("agent", run.agent)
    origin.setdefault("handle", run.handle)
    origin.setdefault("instance", run.handle if run.handle != run.task_id else None)
    origin.setdefault("node_id", (record.node_id if record is not None else None) or meta.get("call_id") or run.task_id)
    if run.authored_task:
        origin.setdefault("authored_task", run.authored_task)
    origin.update({"channel": channel, "chat_id": chat_id, "session_key": run.session_key})
    return origin


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
