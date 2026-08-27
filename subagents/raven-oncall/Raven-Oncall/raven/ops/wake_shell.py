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

  - it does not deliver to anyone, and does not compose anything, in the hosting
    it was written for: the child process owns delivery through its own channel
    wiring, the same as when a TUI fires the job.

    That holds while nobody is watching, which is the case this shell exists for.
    It stops holding the moment this install is a sub-agent instance with an
    operator's pane open on it: the child's reply goes to a pipe this shell reads
    and drops, so the round happens and the person never sees it. Measured
    2026-08-25 through ``/new-instance``: the first round reached the operator
    (it was the direct turn that started the campaign) and rounds two onward did
    not, because from there on the only thing firing them was this shell.

    ``--dispatch-agent`` is the answer to that and changes only WHO ANSWERS the
    wake, never what the wake says: the job is handed to the host's store
    addressed to the instance that owns it, and the host runs it as one
    direct-chat turn against that instance. Still no delivery decided here -- the
    hand-off names the addressee and the host's ordinary machinery does the rest.
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

# How often the sweep looks for dispatched wakes whose turn has finished.
_SWEEP_INTERVAL_S = 30.0
# A backstop, not the signal. The signal is the handed-over job disappearing from
# the host's store, which is precise; this only bounds the case where that can no
# longer be read -- an unreadable store, a host that rewrote it by hand -- so the
# campaign is not left waiting on an answer that will never come. Deliberately
# long: whenever the store IS readable it decides, and a slow turn must never be
# judged for not having scheduled something it is still working on.
_DISPATCH_BACKSTOP_S = 1800.0

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
        notify_store: Path | None = None,
        dispatch_agent: str | None = None,
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
        # Where "this campaign concluded" is delivered, or None for the old
        # silence. The gap it closes, measured 2026-08-25: a campaign started
        # from a host raven's direct chat ran and concluded entirely in
        # wake-spawned turns, whose stdout only this shell ever reads -- the
        # person found out it had finished by asking. The store named here is
        # the HOST raven's cron store: its TUI already polls it for reminders,
        # so a one-shot ownerless job on the "tui" channel surfaces there with
        # no host-side change, waits if every window is closed, and is deleted
        # once shown. This shell stays a messenger -- the text is lifted from
        # the campaign's own report, never composed.
        self._notify_store = Path(notify_store) if notify_store else None
        # Campaigns whose conclusion was already announced -- or that were
        # concluded before this shell started, which a restart must not
        # re-announce. In-memory on purpose: the cost of the miss window (a
        # conclusion landing exactly between shell restarts goes unannounced)
        # is one silent ending, the same as today; a persisted set would be a
        # second ledger to keep true.
        self._notified: set[str] = set(self._concluded_campaigns())
        # The name this install is registered under on the HOST raven, or None for
        # the old behaviour of answering every wake in a child process of our own.
        #
        # Set, a wake owned by a window is handed back to that window instead of
        # being answered here -- see ``_dispatch_handle``. This is the difference
        # between the two hostings, and it is a difference in WHO IS WATCHING, not
        # in what the loop does: with nobody at a terminal a wake answered in a
        # child process is the best available outcome, and with an operator
        # holding that instance's pane open it is the worst, because the round
        # happens and they never see it.
        self._dispatch_agent = (dispatch_agent or "").strip() or None
        # Wakes handed to the host, each with the moment after which this shell
        # should ask whether anything came of it. Held in memory on purpose: a
        # restart means no turn of ours is outstanding, and re-arming for a turn
        # some previous process dispatched would be deciding about a turn nobody
        # here watched. The cost of the miss window is one unwatched round, and
        # the wake the campaign's own turn schedules is unaffected.
        self._rearm_due: list[tuple[float, str, "CronJob"]] = []
        self._sweep_task: asyncio.Task | None = None
        self._cron = CronService(
            self.store_path,
            on_job=self._fire,
            allowed_channels=set(allowed_channels) if allowed_channels is not None else None,
            # This runner holds no conversation, so the 2026-08-14 hazard the
            # owner rule guards against cannot occur here -- and every wake it
            # exists for is owned by a one-shot turn that is dead by the time
            # the wake is due. A live owner still keeps its wake.
            adopt_orphans=True,
        )

    def _dispatch_target(self, job: "CronJob") -> tuple[str, str] | None:
        """``(handle, session_key)`` for the host instance that owns this wake.

        A wake carries its owner as this install's own session key, and that
        session was minted by the host: ``subagent.json`` passes
        ``--session {agent_id}``, so the owner is ``cli:<agent id>``.

        The agent id is NOT the handle. Measured 2026-08-26 on a live
        ``/new-instance`` run: the wake's owner was
        ``cli:c9514237-6b26-450a-87dc-4229e3b92c3c`` while the host knew that
        very instance as ``raven-oncall-ca43bc``. A provisioned id and a handle
        are two names the host keeps for one instance, and only the host's
        registry relates them -- ``{agent_id}`` is the only one of the two the
        command template can even substitute, so the child never sees the other.

        The session key has to come from the same row for the same reason. The
        lane a direct chat runs on is ``<session key>#<agent>/<handle>``, and the
        session key is the host TUI's own (``tui:20260826_102911_ff4d5f``), not
        the ``tui:default`` a cron binding would suggest. Reconstructing it from
        the job's channel and recipient produces a lane no pane subscribes to,
        which loses the reply exactly as silently as not sending it.

        Read at fire time rather than cached: the operator may close the pane and
        open another between one wake and the next, and the row that matters is
        the one true when the wake goes off.

        None whenever the answer is not certain: no ``--dispatch-agent``, no
        notify store, an owner that is not a ``cli:`` session, or no row for this
        agent id. A wake this shell cannot address still has to be answered here
        rather than dropped -- an unwatched campaign is the one outcome worse
        than an unseen round.
        """
        if self._dispatch_agent is None or self._notify_store is None:
            return None
        owner = (getattr(job.payload, "owner", None) or "").strip()
        if not owner.startswith("cli:"):
            return None
        agent_id = owner[len("cli:"):].strip()
        if not agent_id:
            return None
        # The host home is the notify store's grandparent -- <home>/cron/jobs.json
        # -- so the one path this shell is given locates the registry too, and no
        # second flag can be set to a different install than the store.
        registry = Path(self._notify_store).parent.parent / "subagent_instances.json"
        try:
            rows = json.loads(registry.read_text(encoding="utf-8")).get("instances") or []
        except (OSError, ValueError, AttributeError):
            return None
        best: dict[str, Any] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("agent") != self._dispatch_agent or row.get("agentId") != agent_id:
                continue
            if best is None or (row.get("updatedAtMs") or 0) > (best.get("updatedAtMs") or 0):
                best = row
        if best is None:
            return None
        handle = str(best.get("handle") or "").strip()
        session_key = str(best.get("sessionKey") or "").strip()
        if not handle or ":" not in session_key:
            return None
        return handle, session_key

    def _dispatch_to_host(self, job: "CronJob", handle: str, session_key: str) -> str:
        """Hand this wake to the host raven, addressed to the instance that owns it.

        A plain write into the host's store, the same door ``_deliver_conclusion``
        uses. What is different is the addressing: naming the instance turns the
        wake into one direct-chat turn against it rather than a reminder the main
        agent reads out -- the main agent has no ops tools and could only
        paraphrase a message that says "read this ledger and decide".

        The message is passed through untouched. A wake turn starts from an empty
        history, so the message is the whole of what it gets, and a shell that
        edited it would be deciding something.
        """
        from datetime import datetime as _dt

        from raven.proactive_engine.schedulers.cron.service import CronService
        from raven.proactive_engine.schedulers.cron.types import CronSchedule

        # The host's session, split back into the (channel, recipient) a cron
        # binding is made of, so the host reassembles exactly the session key
        # this instance lives under. Carried in the fields that already exist
        # rather than a new one: the binding IS the delivery target, and the
        # instance's own session is the right target for the instance's round.
        channel, _, chat_id = session_key.partition(":")
        host = CronService(self._notify_store, allowed_channels=None)
        # The id is the receipt. delete_after_run means the host removes this job
        # in its post-run writeback -- AFTER the turn has run -- so the id going
        # missing from that store is this shell's only precise account of "the
        # turn is over". A wake still sitting there is queued for a window that
        # has not opened yet, which is not the same as a campaign nobody is
        # watching, and must not be re-armed on top of.
        handed = host.add_job(
            name=job.name,
            # A few seconds out: add_job refuses an ``at`` already in the past,
            # and "now" is in the past by the time the write lands.
            schedule=CronSchedule(kind="at", at_ms=int(_dt.now().timestamp() * 1000) + 5000),
            message=job.payload.message,
            channel=channel,
            to=chat_id,
            delete_after_run=True,
            dedup=False,
            campaign=getattr(job.payload, "campaign", None),
            direct_agent=self._dispatch_agent,
            direct_handle=handle,
        )
        return getattr(handed, "id", "")

    async def _fire(self, job: "CronJob") -> Any:
        """Answer the wake and record that it happened.

        Recorded by the shell rather than inferred later: nothing downstream can
        tell "the wake fired and the turn did nothing" from "the wake never
        fired", and the cron store only keeps the most recent run. A dispatched
        wake is recorded for the same reason and as a distinct record: it fired
        here and ran somewhere else, so this shell's ledger holds no turn for it
        and its absence must not read as a wake that never went off.
        """
        target = self._dispatch_target(job)
        if target is not None:
            handle, session_key = target
            handed_id = ""
            try:
                handed_id = self._dispatch_to_host(job, handle, session_key)
            except Exception as exc:  # noqa: BLE001 -- a failed hand-off must not stop the loop
                logger.warning("wake_shell: could not dispatch '{}' to instance {}: {}", job.name, handle, exc)
            else:
                self._append_ledger_row(
                    {
                        "record": "dispatch",
                        "job_id": job.id,
                        "job_name": job.name,
                        "at_ms": int(time.time() * 1000),
                        "agent": self._dispatch_agent,
                        "handle": handle,
                        "session_key": session_key,
                    }
                )
                logger.info(
                    "wake_shell: job '{}' handed to instance {}/{} in session {} on {}",
                    job.name, self._dispatch_agent, handle, session_key, self._notify_store,
                )
                # Who holds the schedule now is the question this answers. The
                # in-process re-arm cannot: it runs the instant the wake is handed
                # over, when the turn has not started and the honest answer to
                # "did it schedule its next look?" is "ask again later". This is
                # the later. Measured 2026-08-26: a dispatched turn reported its
                # results in prose and called no ops tool at all -- no next wake,
                # no trail entry, and the campaign stopped for good with 4 trials
                # done and the question it wanted answered sitting in a chat.
                self._rearm_due.append((time.monotonic() + _DISPATCH_BACKSTOP_S, handed_id, job))
                self._announce_conclusions()
                # Tells CronService this wake has a turn coming somewhere else,
                # so it does not read "no next look yet" as "the turn ignored the
                # campaign" and arm a second driver.
                from raven.proactive_engine.schedulers.cron.service import HANDED_OFF

                return HANDED_OFF
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
        self._announce_conclusions()

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

    def _concluded_campaigns(self) -> list[str]:
        """Names of campaigns whose directory holds a concluded.json."""
        try:
            from raven.ops.instrument import ops_home

            home = ops_home()
            if not home.exists():
                return []
            return sorted(d.name for d in home.iterdir()
                          if d.is_dir() and (d / "concluded.json").exists())
        except Exception:  # noqa: BLE001 -- an unreadable ops home is no conclusions, not a crash
            return []

    def _announce_conclusions(self) -> None:
        """Deliver each newly concluded campaign's report to the notify store, once.

        Runs after every fired turn rather than on a timer: a conclusion can only
        be produced by a turn, and this shell is what runs them. The message is
        the campaign's own words -- outcome from concluded.json and the head of
        its newest report -- because a shell that summarised would be a second
        author of the result.
        """
        if self._notify_store is None:
            return
        for name in self._concluded_campaigns():
            if name in self._notified:
                continue
            self._notified.add(name)
            try:
                self._deliver_conclusion(name)
            except Exception as exc:  # noqa: BLE001 -- a failed delivery must not stop the loop
                logger.warning("wake_shell: could not announce '{}' concluding: {}", name, exc)

    def _deliver_conclusion(self, campaign: str) -> None:
        from datetime import datetime as _dt

        from raven.ops.instrument import ops_home
        from raven.proactive_engine.schedulers.cron.service import CronService
        from raven.proactive_engine.schedulers.cron.types import CronSchedule

        cdir = ops_home() / campaign
        outcome = "?"
        try:
            outcome = json.loads((cdir / "concluded.json").read_text(encoding="utf-8")).get("outcome", "?")
        except Exception:  # noqa: BLE001 -- a malformed conclusion is still worth announcing
            pass
        head = ""
        reports = sorted(cdir.glob("report-*.md"))
        if reports:
            lines = reports[-1].read_text(encoding="utf-8").splitlines()
            head = "\n".join(lines[:16])
        message = (
            f"[Ops campaign '{campaign}' concluded: {outcome}]\n"
            f"{head}\n...\n"
            f"Full report: {reports[-1] if reports else cdir}"
        )
        # A plain write into the host's store: no owner, so whichever window is
        # open claims it; nobody open, it waits. delete_after_run so it is shown
        # once. The service is never start()ed here -- add_job is a locked file
        # write, and firing belongs to the host's own runner.
        host = CronService(self._notify_store, allowed_channels=None)
        host.add_job(
            name=f"ops:{campaign}:concluded",
            # A few seconds out: add_job refuses an ``at`` already in the past,
            # and "now" is in the past by the time the write lands.
            schedule=CronSchedule(kind="at", at_ms=int(_dt.now().timestamp() * 1000) + 5000),
            message=message,
            channel="tui",
            to="default",
            delete_after_run=True,
            dedup=True,
            campaign=campaign,
        )
        logger.info("wake_shell: announced '{}' concluded ({}) into {}", campaign, outcome, self._notify_store)

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

    def _host_job_ids(self) -> set[str] | None:
        """The ids still pending in the host's store, or None if it cannot be read.

        None is not an empty set: unreadable has to mean "no conclusion", or a
        transient read error would read as "every turn finished" and re-arm the
        lot of them.
        """
        if self._notify_store is None:
            return None
        try:
            data = json.loads(Path(self._notify_store).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return None
        return {str(j.get("id")) for j in jobs if isinstance(j, dict)}

    def _sweep_dispatched(self) -> None:
        """Re-arm a campaign whose dispatched turn has finished leaving no next look.

        Two ways to decide the turn is over, and the first is the real one:

        - **its job is gone from the host's store.** ``delete_after_run`` means the
          host removes it in the writeback that follows the turn, so its absence
          is an account of the turn ending rather than a guess about how long one
          takes. A turn is unbounded; every fixed grace is wrong for the slow one.
        - **the backstop elapsed**, for when that store stopped being readable.

        Only wakes this shell handed over. One it answered itself was judged by
        ``_execute_job`` when its child exited -- the right moment for that
        hosting, and the wrong one for this.
        """
        if not self._rearm_due:
            return
        now = time.monotonic()
        live = self._host_job_ids()
        keep: list[tuple[float, str, "CronJob"]] = []
        due: list["CronJob"] = []
        for deadline, handed_id, job in self._rearm_due:
            ran = live is not None and handed_id and handed_id not in live
            if ran or now >= deadline:
                due.append(job)
            else:
                keep.append((deadline, handed_id, job))
        self._rearm_due = keep
        for job in due:
            try:
                self._cron.ensure_campaign_watched(job)
            except Exception as exc:  # noqa: BLE001 -- one campaign must not stop the sweep
                logger.warning("wake_shell: re-arm check failed for '{}': {}", job.name, exc)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            try:
                self._sweep_dispatched()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the sweep is a safety net, not a critical path
                logger.exception("wake_shell: dispatch sweep failed; continuing")

    async def start(self) -> None:
        self.overdue = self._survey_overdue()
        self._late_by_job = {w.job_id: w.late_ms for w in self.overdue if w.fired}
        await self._cron.start()
        if self._dispatch_agent is not None:
            self._sweep_task = asyncio.ensure_future(self._sweep_forever())

    def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
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
        "--notify-store",
        type=Path,
        default=None,
        help=(
            "a HOST raven's cron store (jobs.json) to drop a one-shot 'campaign concluded' "
            "reminder into; omitted, a conclusion stays where it always was, on disk"
        ),
    )
    ap.add_argument(
        "--dispatch-agent",
        default=None,
        help=(
            "the name this install is registered under on the HOST raven; set, a wake "
            "owned by a host-minted session is handed back to that instance's pane "
            "instead of being answered in a child process here (needs --notify-store)"
        ),
    )
    ap.add_argument(
        "--fire-missed",
        action="store_true",
        help=(
            "fire one-shot wakes that were already due at startup; off by default because "
            "CronService drops them on purpose (a missed reminder is not re-delivered)"
        ),
    )
    return ap


def apply_instance_config(config: Path | None) -> None:
    """Point THIS process at the instance it serves, not just its children.

    --config was only ever handed to the spawned turns, so the shell's own
    process kept the default ~/.raven -- and everything in it that resolves
    ops_home() ran against the wrong instance. Measured 2026-08-25: the rearm
    hook appended wake_rearmed events into a same-named campaign of the DEFAULT
    instance, and the rearm message embedded that instance's ledger path, which
    the woken turn then obediently used -- concluding an experiment this shell
    had nothing to do with. The conclusion scan for --notify-store reads
    ops_home() too, and without this it would announce the wrong instance's
    endings.
    """
    if config is None:
        return
    from raven.config.loader import set_config_path

    set_config_path(Path(config))


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply_instance_config(args.config)
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
            notify_store=args.notify_store,
            dispatch_agent=args.dispatch_agent,
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
