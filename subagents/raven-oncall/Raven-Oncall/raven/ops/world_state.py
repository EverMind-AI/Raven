"""Persist a scripted on-call world so a watch can span a process exit.

The scripted world lives in memory, which is fine for an eval that runs inside
one process and useless for the product claim that matters most: that the loop
picks its watch back up after its own process is gone. Nothing measured so far
touches that -- SentinelBench never exits the process, and Recovery-Bench keeps
every container side effect -- so it has never been shown at all.

Two decisions carry this file:

  - **the world keeps moving while the loop is not running.** ``downtime_ms`` on
    resume advances campaign time without the loop observing any of it. Without
    that, campaign time freezes on exit and the hard case disappears: things
    happened, nobody was watching, and now the loop has to work out what it
    missed without re-reporting what it already reported.
  - **what the loop already did is state, not memory.** Escalations, reports and
    cancellations are restored from disk, so "report only once" has to hold
    across the exit rather than only within a turn. That is the only form of the
    invariant a product can rely on.

Writes reuse the ledger's durability discipline (temp file, fsync, rename, fsync
the directory) for the same reason: a rename survives a killed process but not a
host losing power.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from raven.ops.action_log import ActionLog
from raven.ops.handoff import MockOrchestrator
from raven.ops.interruption import ContractGuard, InterruptionContract
from raven.ops.scripted_backend import JobScript, ScriptedJobBackend, _ScriptedJob
from raven.ops.scripted_human import Escalation, HumanReply, ScriptedHuman
from raven.ops.simclock import SimClock

_VERSION = 1


class WorldStateError(RuntimeError):
    """The saved world exists but cannot be read back."""


def _script_to_dict(script: JobScript | None) -> dict[str, Any] | None:
    return asdict(script) if script is not None else None


def _script_from_dict(payload: dict[str, Any] | None) -> JobScript | None:
    if payload is None:
        return None
    data = dict(payload)
    # asdict turns the (offset, sample) tuples into lists; JobScript is only ever
    # read positionally, but restoring tuples keeps a round trip equal to the
    # original, which is what the tests compare.
    data["progress"] = [tuple(entry) for entry in data.get("progress", [])]
    return JobScript(**data)


def snapshot(
    *,
    clock: SimClock,
    backend: ScriptedJobBackend,
    human: ScriptedHuman,
    guard: ContractGuard | None = None,
    orchestrator: MockOrchestrator | None = None,
    action_log: ActionLog | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything needed to carry on, and nothing the loop should remember.

    The agent's own reasoning is deliberately absent: a resumed turn starts cold
    and has to reconstruct its situation from this, which is the behaviour under
    test.
    """
    jobs = {}
    for idem_key, job_id in backend._by_idem.items():
        job = backend._jobs[job_id]
        jobs[idem_key] = {
            "job_id": job_id,
            "submitted_at_ms": job.submitted_at_ms,
            "polls": job.polls,
            "first_observed_terminal_ms": job.first_observed_terminal_ms,
            "cancelled_at_ms": job.cancelled_at_ms,
            "script": _script_to_dict(job.script),
            "counterfactual": _script_to_dict(job.counterfactual),
            "payload": job.spec.payload,
            "labels": dict(getattr(job.spec, "labels", {}) or {}),
        }
    escalations = {
        topic: {
            "message": esc.message,
            "asked_at_ms": esc.asked_at_ms,
            "reply": asdict(esc.reply),
            "follow_ups": list(esc.follow_ups),
            "read_at_ms": esc.read_at_ms,
        }
        for topic, esc in human._asked.items()
    }
    state: dict[str, Any] = {
        "version": _VERSION,
        "now_ms": clock.now_ms(),
        "seq": backend._seq,
        "jobs": jobs,
        "human": {
            "replies": {k: asdict(v) for k, v in human._replies.items()},
            "default": asdict(human._default),
            "escalations": escalations,
        },
    }
    if guard is not None:
        state["guard"] = {
            "contract": asdict(guard._contract),
            "start_hour": guard._start_hour,
            "allowed": guard._allowed,
            "unestimated": guard._unestimated,
            "breach_attempts": [list(entry) for entry in guard._breach_attempts],
        }
    if orchestrator is not None:
        # Only the dedupe keys are carried. Restoring the reports themselves would
        # let a resumed run re-derive its own score; what has to survive is the
        # fact that a signal was already reported.
        state["orchestrator"] = {"seen_keys": sorted(orchestrator._seen_keys)}
    if action_log is not None:
        state["action_log"] = action_log.to_dict()
    if extra:
        state["extra"] = extra
    return state


def save(path: str | Path, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise WorldStateError(f"saved world at {target} is unreadable ({exc})") from exc
    if state.get("version") != _VERSION:
        raise WorldStateError(f"saved world at {target} has version {state.get('version')!r}, expected {_VERSION}")
    return state


class RestoredWorld:
    """A world carried across a process exit, plus how much it moved unwatched."""

    def __init__(
        self,
        *,
        clock: SimClock,
        backend: ScriptedJobBackend,
        human: ScriptedHuman,
        guard: ContractGuard | None,
        orchestrator: MockOrchestrator,
        action_log: ActionLog,
        downtime_ms: int,
        extra: dict[str, Any],
    ) -> None:
        self.clock = clock
        self.backend = backend
        self.human = human
        self.guard = guard
        self.orchestrator = orchestrator
        self.action_log = action_log
        self.downtime_ms = downtime_ms
        self.extra = extra

    def already_reported(self) -> set[str]:
        """Signals reported before the exit. Reporting one again is the failure
        the invariant exists to prevent, and it can only be checked from here."""
        return set(self.orchestrator._seen_keys)


def restore(state: dict[str, Any], *, downtime_ms: int = 0) -> RestoredWorld:
    """Rebuild the world, then advance it by ``downtime_ms`` unobserved.

    The advance happens before the loop gets a turn, so anything that became
    terminal while the process was gone is already terminal on the first poll --
    and its detection latency correctly counts the downtime, because the loop was
    responsible for watching it and was not there.
    """
    clock = SimClock(speed_factor=0)
    clock.advance_ms(int(state["now_ms"]) + max(0, int(downtime_ms)))

    backend = ScriptedJobBackend(clock)
    backend._seq = int(state.get("seq", 0))
    from raven.ops.backend import JobSpec

    for idem_key, saved in state.get("jobs", {}).items():
        job = _ScriptedJob(
            spec=JobSpec(payload=saved.get("payload") or {}, idem_key=idem_key,
                         labels=saved.get("labels") or {}),
            script=_script_from_dict(saved.get("script")) or JobScript(),
            submitted_at_ms=int(saved["submitted_at_ms"]),
            polls=int(saved.get("polls", 0)),
            first_observed_terminal_ms=saved.get("first_observed_terminal_ms"),
            cancelled_at_ms=saved.get("cancelled_at_ms"),
            counterfactual=_script_from_dict(saved.get("counterfactual")),
        )
        backend._by_idem[idem_key] = saved["job_id"]
        backend._jobs[saved["job_id"]] = job

    saved_human = state.get("human", {})
    human = ScriptedHuman(
        clock,
        replies={k: HumanReply(**v) for k, v in saved_human.get("replies", {}).items()},
        default=HumanReply(**saved_human.get("default", {})),
    )
    for topic, saved in saved_human.get("escalations", {}).items():
        human._asked[topic] = Escalation(
            topic=topic,
            message=saved["message"],
            asked_at_ms=int(saved["asked_at_ms"]),
            reply=HumanReply(**saved["reply"]),
            follow_ups=list(saved.get("follow_ups", [])),
            read_at_ms=saved.get("read_at_ms"),
        )

    guard = None
    if "guard" in state:
        saved_guard = state["guard"]
        contract_fields = dict(saved_guard["contract"])
        quiet = contract_fields.get("quiet_hours")
        if quiet is not None:
            contract_fields["quiet_hours"] = tuple(quiet)
        guard = ContractGuard(
            InterruptionContract(**contract_fields), start_hour=int(saved_guard.get("start_hour", 9))
        )
        guard._allowed = int(saved_guard.get("allowed", 0))
        guard._unestimated = int(saved_guard.get("unestimated", 0))
        guard._breach_attempts = [(int(at), why) for at, why in saved_guard.get("breach_attempts", [])]

    action_log = ActionLog.from_dict(state.get("action_log") or {})
    orchestrator = MockOrchestrator(action_log)
    orchestrator._seen_keys = set(state.get("orchestrator", {}).get("seen_keys", []))

    return RestoredWorld(
        clock=clock,
        backend=backend,
        human=human,
        guard=guard,
        orchestrator=orchestrator,
        action_log=action_log,
        downtime_ms=max(0, int(downtime_ms)),
        extra=state.get("extra") or {},
    )
