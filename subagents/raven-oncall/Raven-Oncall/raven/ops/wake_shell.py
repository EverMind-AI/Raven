"""A resident shell that fires a due wake when no interactive process is up.

The problem it exists for: an on-call loop schedules its own next wake as a cron
job, and the job's ``payload.channel`` is ``tui`` (or ``cli``) because that is
where it was created. ``raven gateway`` deliberately does not claim those --
``_build_gateway_channels`` explains why: a TUI-set reminder must deliver to the
TUI rather than race the gateway onto an IM channel. And ``raven agent -m`` is
one-shot: it exits as soon as it replies. So nothing fires the wake unless a
human keeps an interactive process open for the whole run. Every on-call
experiment so far has cost a person sitting with a TUI open for 90 minutes, and
that is the real reason the line moves slowly.

This shell is the missing piece and nothing more:

  - **no LLM, no task semantics.** It never interprets ``payload.message``; it
    passes it to a child process verbatim. Everything about "what to do when
    woken" stays where it was, in the agent. A shell that knew about campaigns
    would be a second decision-maker in the loop.
  - **one fresh process per wake.** The turn is spawned, it works, it exits. The
    shell holds no agent, no session and no conversation, so a wake's context
    does not grow with the run's length. Continuity has to come from the
    campaign's ledger on disk -- which is exactly the behaviour under test
    ("does it get called back and pick up from its own trail"), so the shell
    must not hand the turn a conversation to lean on instead.
  - **claiming is not reimplemented.** ``CronService`` already does the hard
    part: an fcntl-locked store, a pid claim with a TTL so a dead peer's job is
    stolen rather than stuck, one-shot deletion, and next-run computation. This
    wraps it; it does not replace it.

Shape and price come from the SentinelBench cold-start round (``sb_shell.py`` +
``sb_turn.py``, 564 cold starts, ``COLD START VERIFIED`` 98/98, 0 duplicate
notifications): a dumb poller plus a short-lived turn works, and it costs about
+37% model calls and about +3.0s median detection latency, because a woken turn
has to look before it can act. That was measured on the SentinelBench carrier,
not this one -- whether the ops carrier pays the same has to be measured here.

**This changes how raven is hosted, so it is a new experiment variable.** A run
under this shell is not comparable to a run under a person's TUI, any more than
a run on a different model is. Report it before attributing anything to it.

What it deliberately does not do:

  - it does not deliver to anyone. The child process owns delivery through its
    own channel wiring, the same as when a TUI fires the job.
  - it does not retry a failed turn. ``CronService`` records ``last_status`` and
    ``last_error``; a retry policy would be a decision, and there is no evidence
    yet about what the right one is.
  - it does not claim IM-channel jobs, so it cannot race the gateway.
  - it does not bound the child's runtime. A turn that hangs holds its claim
    until the TTL expires, and the shell will not notice.

Run it as::

    python -m raven.ops.wake_shell --store <dir>/cron/jobs.json --config <dir>/config.json

``--store`` is required and is refused if it points at the default
``~/.raven/cron/jobs.json`` unless ``--allow-shared-store`` is passed: two
processes claiming the same store is exactly how a live experiment gets extra
turns injected into it, and the cost of that mistake is a whole run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.proactive_engine.schedulers.cron.types import CronJob

# The ephemeral channels no resident process claims today. IM channels are left
# to the gateway on purpose.
DEFAULT_CHANNELS: frozenset[str] = frozenset({"tui", "cli"})

LEDGER_FILE = "wake_shell.jsonl"

# How far ahead an already-due wake is moved when --fire-missed is on. Small, but
# it has to be far enough ahead that the service's startup recompute still sees a
# future time -- at zero the job is dropped again.
MISSED_WAKE_LEAD_MS = 500


class SharedStoreRefused(RuntimeError):
    """The shell was pointed at a store another process is expected to own."""


def default_store_path() -> Path:
    return Path.home() / ".raven" / "cron" / "jobs.json"


def assert_store_is_isolated(store_path: Path, *, allow_shared: bool = False) -> None:
    """Refuse the default store unless the caller insists.

    Not a style preference. A live on-call run keeps its wakes in the default
    store, and a second claimer there gives that run turns nobody asked for.
    """
    if allow_shared:
        return
    if store_path.expanduser().resolve(strict=False) == default_store_path().resolve(strict=False):
        raise SharedStoreRefused(
            f"{store_path} is the default cron store, which an interactive process is "
            "expected to own. Pass --store pointing at an isolated store, or "
            "--allow-shared-store to use this one anyway."
        )


@dataclass(frozen=True)
class Spawn:
    """One short-lived turn the shell launched. Raw record, no interpretation.

    ``late_ms`` is set when this turn came from a wake that was already overdue
    at startup. A late wake and a missed wake are different failures and must not
    collapse into one count: "we never looked" and "we looked 31 minutes late"
    have different causes and different fixes.
    """

    job_id: str
    job_name: str
    at_ms: int
    argv: list[str]
    returncode: int | None
    duration_ms: int
    error: str | None = None
    late_ms: int | None = None


@dataclass(frozen=True)
class OverdueWake:
    """A wake that was already due when the shell started.

    ``fired`` says which of the two failures this was: ``True`` means it ran
    late (``late_ms`` says how late), ``False`` means ``CronService`` dropped it
    and it never ran at all. Recorded either way -- a wake silently discarded
    because a flag defaulted off is exactly what must not be invisible.
    """

    job_id: str
    job_name: str
    channel: str | None
    due_at_ms: int
    observed_at_ms: int
    late_ms: int
    fired: bool
    not_fired_reason: str | None = None


# Injected so the shell is testable without launching a real agent.
TurnSpawner = Callable[["CronJob"], Awaitable[Spawn]]


@dataclass
class SubprocessTurn:
    """Spawns ``raven agent -m <message>`` and waits for it to exit.

    A fresh invocation mints a fresh ``cli:`` session by default, which is the
    cold start this shell is for -- ``--resume`` is deliberately not passed.

    ``LITELLM_LOCAL_MODEL_COST_MAP`` is set because LiteLLM otherwise fetches a
    remote price table on each process's first completion. A resident process
    pays that once; a shell that starts a process per wake pays it every time
    (measured at ~4.4s per process on a box with no route to the internet).
    """

    config_path: Path | None = None
    executable: str | None = None
    extra_args: tuple[str, ...] = ()
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout_s: float | None = None

    def _argv(self, message: str) -> list[str]:
        exe = self.executable or shutil.which("raven")
        base = [exe, "agent"] if exe else [sys.executable, "-m", "raven.cli", "agent"]
        argv = [*base, "-m", message, "--no-markdown"]
        if self.config_path is not None:
            argv += ["--config", str(self.config_path)]
        argv += list(self.extra_args)
        return argv

    async def __call__(self, job: "CronJob") -> Spawn:
        argv = self._argv(job.payload.message)
        env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True", **self.env_overrides}
        started = time.monotonic()
        at_ms = int(time.time() * 1000)
        rc: int | None = None
        error: str | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                error = f"turn exceeded {self.timeout_s}s and was killed"
            rc = proc.returncode
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            error = f"{type(exc).__name__}: {exc}"
        return Spawn(
            job_id=job.id,
            job_name=job.name,
            at_ms=at_ms,
            argv=argv,
            returncode=rc,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )


class WakeShell:
    """Polls a cron store and spawns one short-lived turn per due job."""

    def __init__(
        self,
        store_path: Path,
        *,
        spawner: TurnSpawner,
        allowed_channels: frozenset[str] | set[str] | None = DEFAULT_CHANNELS,
        ledger_dir: Path | None = None,
        allow_shared_store: bool = False,
        fire_missed: bool = False,
    ) -> None:
        from raven.proactive_engine.schedulers.cron.service import CronService

        assert_store_is_isolated(Path(store_path), allow_shared=allow_shared_store)
        self.store_path = Path(store_path)
        self._spawner = spawner
        # The store is the only place a job lives, so the ledger sits beside it.
        self.ledger_path = Path(ledger_dir or self.store_path.parent) / LEDGER_FILE
        self.spawns: list[Spawn] = []
        self.fire_missed = fire_missed
        self.overdue: list[OverdueWake] = []
        # job_id -> how late that wake was, so the turn's own record carries it.
        self._late_by_job: dict[str, int] = {}
        self._cron = CronService(
            self.store_path,
            on_job=self._fire,
            allowed_channels=set(allowed_channels) if allowed_channels is not None else None,
        )

    async def _fire(self, job: "CronJob") -> None:
        """Spawn the turn and record that it happened.

        Recorded by the shell rather than inferred later: nothing downstream can
        tell "the wake fired and the turn did nothing" from "the wake never
        fired", and the cron store only keeps the most recent run.
        """
        spawn = await self._spawner(job)
        late_ms = self._late_by_job.pop(job.id, None)
        if late_ms is not None:
            spawn = replace(spawn, late_ms=late_ms)
        self.spawns.append(spawn)
        self._append_ledger(spawn)
        if spawn.error or spawn.returncode not in (0, None):
            logger.warning(
                "wake_shell: job '{}' turn exited rc={} error={}", job.name, spawn.returncode, spawn.error
            )
        else:
            logger.info("wake_shell: job '{}' turn finished in {}ms", job.name, spawn.duration_ms)

    def _append_ledger_row(self, row: dict[str, Any]) -> None:
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            # A shell that dies because it could not write its own log would be
            # worse than one with a gap in the log, and the gap is visible.
            logger.warning("wake_shell: could not append to {}: {}", self.ledger_path, exc)

    def _append_ledger(self, spawn: Spawn) -> None:
        self._append_ledger_row({"record": "spawn", **asdict(spawn)})

    def _survey_overdue(self) -> list[OverdueWake]:
        """Record every already-due one-shot wake, and rescue it if asked to.

        ``CronService.start`` deliberately discards a past-due ``at`` job:
        re-delivering a reminder that was missed while the process was down is
        the right behaviour for a person (it matches iOS / Calendar). For an
        on-call wake it is the opposite -- the job being due and unfired is the
        whole condition this shell exists to fix. That conflict is a product-level
        semantic clash between two callers of one scheduler, not something a flag
        settles; the flag only decides which side this process takes.

        So the survey runs **regardless of the flag**. With ``fire_missed`` off
        the wake still gets a record saying it was dropped and by how much it was
        overdue. A wake that vanishes because a flag defaulted off would look
        identical to a wake that was never scheduled, and those have different
        causes.

        Only ``at`` jobs are surveyed: a recurring job has a next occurrence and
        needs no rescue.

        The rescue has to move ``schedule.atMs``, not just ``state.nextRunAtMs``.
        ``_recompute_next_runs`` decides what to drop from ``schedule.at_ms``
        alone, so the service's own ``advance_job_to_now`` -- which moves state
        only, and is called on an already-running service -- is not enough before
        startup: the job would still be discarded.

        Written directly, before the service starts and caches anything. With an
        isolated store (the default, enforced) nobody else is writing. Under
        ``--allow-shared-store`` a peer could be, and this read-modify-write is
        not under the service's lock -- one more reason that flag is a last
        resort.
        """
        found: list[OverdueWake] = []
        try:
            payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return found
        now_ms = int(time.time() * 1000)
        target_ms = now_ms + MISSED_WAKE_LEAD_MS
        allowed = self._cron.allowed_channels
        rescued_any = False
        for raw in payload.get("jobs") or []:
            if not raw.get("enabled") or (raw.get("schedule") or {}).get("kind") != "at":
                continue
            due = (raw.get("state") or {}).get("nextRunAtMs") or (raw.get("schedule") or {}).get("atMs")
            if not due or due > now_ms:
                continue
            channel = (raw.get("payload") or {}).get("channel")
            reason: str | None = None
            if allowed is not None and channel and channel not in allowed:
                reason = f"channel {channel!r} is not in this shell's allow-list"
            elif not self.fire_missed:
                reason = "fire_missed is off, so CronService.start will drop it"
            if reason is None:
                raw["schedule"]["atMs"] = target_ms
                raw.setdefault("state", {})["nextRunAtMs"] = target_ms
                raw["updatedAtMs"] = now_ms
                rescued_any = True
            found.append(
                OverdueWake(
                    job_id=raw["id"],
                    job_name=raw.get("name") or raw["id"],
                    channel=channel,
                    due_at_ms=int(due),
                    observed_at_ms=now_ms,
                    late_ms=now_ms - int(due),
                    fired=reason is None,
                    not_fired_reason=reason,
                )
            )
        if rescued_any:
            tmp = self.store_path.with_name(self.store_path.name + ".wake_shell.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
        for wake in found:
            if wake.fired:
                logger.warning(
                    "wake_shell: firing '{}' {}s late (was due at {})",
                    wake.job_name, wake.late_ms // 1000, wake.due_at_ms,
                )
            else:
                logger.warning(
                    "wake_shell: NOT firing '{}', overdue {}s -- {}",
                    wake.job_name, wake.late_ms // 1000, wake.not_fired_reason,
                )
            self._append_ledger_row({"record": "overdue_wake", **asdict(wake)})
        return found

    async def start(self) -> None:
        self.overdue = self._survey_overdue()
        self._late_by_job = {w.job_id: w.late_ms for w in self.overdue if w.fired}
        await self._cron.start()

    def stop(self) -> None:
        self._cron.stop()

    def status(self) -> dict[str, Any]:
        """Raw counts. No success/failure verdict: an exit code is not a verdict
        on whether the wake did anything useful."""
        return {
            "store_path": str(self.store_path),
            "ledger_path": str(self.ledger_path),
            "spawns": len(self.spawns),
            "spawns_nonzero_exit": sum(1 for s in self.spawns if s.returncode not in (0, None)),
            "spawns_with_error": sum(1 for s in self.spawns if s.error),
            # Two different failures, never one number: a wake that ran late and a
            # wake that never ran have different causes and different fixes.
            "wakes_fired_late": [asdict(w) for w in self.overdue if w.fired],
            "wakes_dropped_unfired": [asdict(w) for w in self.overdue if not w.fired],
            "fire_missed": self.fire_missed,
            "cron": self._cron.status(),
        }

    async def run_forever(self, stop_after_s: float | None = None) -> None:
        """Serve until cancelled, or for ``stop_after_s`` when bounded.

        Bounded is what a first real run should use: a 5-minute budget proves the
        wake fires without risking a 90-minute experiment on an untested host.
        """
        await self.start()
        try:
            if stop_after_s is None:
                while True:
                    await asyncio.sleep(3600)
            else:
                await asyncio.sleep(stop_after_s)
        except asyncio.CancelledError:
            raise
        finally:
            self.stop()


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m raven.ops.wake_shell",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--store", type=Path, required=True, help="cron store to poll (jobs.json)")
    ap.add_argument("--config", type=Path, default=None, help="config file the spawned turn should use")
    ap.add_argument(
        "--channels",
        default=",".join(sorted(DEFAULT_CHANNELS)),
        help="comma-separated channels to claim; 'any' claims every channel (races the gateway)",
    )
    ap.add_argument("--turn-timeout", type=float, default=None, help="kill a turn that runs longer, in seconds")
    ap.add_argument("--stop-after", type=float, default=None, help="exit after this many seconds")
    ap.add_argument("--allow-shared-store", action="store_true", help="permit the default ~/.raven cron store")
    ap.add_argument(
        "--fire-missed",
        action="store_true",
        help=(
            "fire one-shot wakes that were already due at startup; off by default because "
            "CronService drops them on purpose (a missed reminder is not re-delivered)"
        ),
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    channels = None if args.channels.strip().lower() == "any" else frozenset(
        c.strip() for c in args.channels.split(",") if c.strip()
    )
    try:
        shell = WakeShell(
            args.store,
            spawner=SubprocessTurn(config_path=args.config, timeout_s=args.turn_timeout),
            allowed_channels=channels,
            allow_shared_store=args.allow_shared_store,
            fire_missed=args.fire_missed,
        )
    except SharedStoreRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    logger.info("wake_shell: polling {} for channels {}", args.store, channels or "any")
    try:
        asyncio.run(shell.run_forever(stop_after_s=args.stop_after))
    except KeyboardInterrupt:
        pass
    print(json.dumps(shell.status(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
