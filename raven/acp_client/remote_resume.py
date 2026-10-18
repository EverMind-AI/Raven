"""Picking a turn up that a process before this one left running on a registered machine.

The run's record (:mod:`raven.acp_client.remote_runs`) says which machine,
which task its daemon runs the agent under, which ACP session the turn is in,
which prompt is awaited and how many frames, and how many stderr lines, that
process had read when the prompt went out. This module turns that into the
reply the turn owes: the machine's daemon replays the turn from there, whole,
with the stderr the turn wrote, and hands the rest over as it comes
(``agent/attach``),
a connection over that channel is taken into the pool as if started here, the
turn's collector reads the agent's messages off it, and the answer to the
prompt -- sent by the other process, awaited by this one -- ends the wait. What
to do with the reply is the manager's business (:mod:`raven.agent.subagent.resume_remote`),
and a cli entry's run is read by the entry itself (``CliAgentBackend.pick_up``).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.acp_client import remote
from raven.acp_client.acp_agent import AcpAgentBackend, _prompt_deadline, _TurnCollector
from raven.acp_client.acp_dialects import dialect_for
from raven.acp_client.pool import get_pool
from raven.acp_client.protocol import AcpConnectionError, AcpTimeoutError
from raven.acp_client.remote_pickup import PICKED_UP_NOTE, PickedUp, UnreachableError
from raven.acp_client.remote_runs import RemoteRun
from raven.ops import remote_path


class _Span:
    """What :meth:`AcpAgentBackend._finished` tells about a turn cut short; there is no trace here."""

    def error(self, said: str) -> None:
        logger.warning("picked-up turn: {}", said)


async def pick_up(run: RemoteRun, backend: AcpAgentBackend, *, pool: Any = None) -> PickedUp:
    """The reply the turn in ``run`` owes, read off the machine; or why there is none.

    Raises :class:`UnreachableError` when the machine cannot be asked at all,
    or when this computer stops under the pick-up, so the caller keeps the
    record for a later start; every other outcome is a :class:`PickedUp`,
    and the record is the caller's to remove.
    """
    link = pool if pool is not None else remote_path.nodes()
    if link is None:
        raise UnreachableError(
            f"this process does not reach registered machines, so the run on machine {run.machine} waits"
        )
    try:
        target = remote.machine(run.machine)
    except remote.RemoteMachineError as exc:
        return PickedUp("error", f"the run of {run.agent!r} cannot be picked up: {exc}")
    try:
        rows = await link.tasks(run.machine)
    except remote_path.NodeCallError as exc:
        raise UnreachableError(str(exc)) from None
    if not any(row.get("task") == run.task for row in rows):
        return PickedUp(
            "error",
            f"machine {target.label} no longer has the run of {run.agent!r} (task {run.task}); "
            "its record there was removed, or the machine's node directory was",
        )
    if run.prompt_id <= 0:
        return PickedUp("error", f"the run of {run.agent!r} on machine {target.label} ended before its prompt was sent")
    try:
        endpoint, attached = await link.attach(
            run.machine, run.task, name=run.agent, after=run.received, stderr_after=run.stderr_received
        )
    except remote_path.NodeCallError as exc:
        if exc.reason in ("unknown_task", None) and "no longer runs" in str(exc):
            return PickedUp("error", str(exc))
        raise UnreachableError(str(exc)) from None
    connection, held = await get_pool().adopt(
        name=run.agent,
        endpoint=endpoint,
        initialize=run.initialize,
        launch=run.launch_key,
        config=run.config_key,
        # Past the ids the other process used, with room: a replayed answer to
        # one of its requests must resolve nothing sent from here.
        next_id=run.next_id + _ID_ROOM,
    )
    client = connection.client
    collector = _TurnCollector(dialect=dialect_for(run.initialize), limit=backend.max_output_chars, track_files=False)
    try:
        async with connection.session_lock(run.session_id):
            connection.router.reserve(run.session_id)
            await connection.router.take_over(run.session_id, collector)
            try:
                result = await client.await_response(
                    run.prompt_id,
                    method="session/prompt",
                    session=run.session_id,
                    timeout=_prompt_deadline(backend.timeout),
                )
            except AcpConnectionError as exc:
                if remote.leaving():
                    # This computer is stopping under the pick-up: the agent
                    # stays with its machine, and the run is the next start's.
                    raise UnreachableError(
                        f"this computer stopped while the run of {run.agent!r} on machine {target.label} was being "
                        "picked up; it waits for the next start"
                    ) from None
                rc = attached.get("rc")
                ended = f" (exit {rc})" if isinstance(rc, int) else ""
                return PickedUp(
                    "error",
                    f"the run of {run.agent!r} on machine {target.label} ended while this computer was away{ended}: {exc}",
                )
            except AcpTimeoutError as exc:
                return PickedUp("error", f"the run of {run.agent!r} on machine {target.label} was waited out: {exc}")
            finally:
                connection.router.detach(run.session_id, collector)
        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
        if not collector.text:
            return PickedUp(
                "error",
                f"acp agent {run.agent!r} ended its turn with no content (stopReason={stop_reason!r}); "
                f"its frames are on machine {target.label}, task {run.task}",
                stop_reason,
            )
        reply = await backend._finished(collector.reply, stop_reason=stop_reason, span=_Span(), sink=None)
        reply += (
            f"\n\n[raven] Ran on machine {target.label}, in {remote.shown(target, run.session_cwd)}, "
            f"{PICKED_UP_NOTE}; the files it wrote are there, not on this computer -- read_file, list_dir, "
            "find, grep, write_file and edit_file take that path as it is, and deliver_files fetches one by it."
        )
        return PickedUp("ok", reply, stop_reason)
    finally:
        if not held:
            # Another connection of the name is live: this one served the pick-up only.
            await client.close()


#: How far past the other process's last request id this one starts.
_ID_ROOM = 1000

__all__ = ["PICKED_UP_NOTE", "PickedUp", "UnreachableError", "pick_up"]
