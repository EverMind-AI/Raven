"""``raven cron`` subapp — operator-facing CLI for the CronService
state at ``~/.raven/cron/jobs.json``.

7 commands (per plan ``sorted-brewing-crayon.md``):

- ``cron list [--all]``         — overview + service banner
- ``cron get <id>``             — full detail of one job
- ``cron delete <id> [--yes]``  — destructive escape hatch
- ``cron enable <id>``          — toggle enabled=True
- ``cron disable <id>``         — toggle enabled=False
- ``cron run <id> [--force]``   — synchronously trigger
- ``cron add ...``              — flag-driven add (scripting / recovery
                                   path; agent's CronTool covers the
                                   interactive natural-language path)

Cross-cutting:

- All write ops default to a ``[y/N]`` confirm prompt; ``--yes`` skips.
- ID arguments accept any unique prefix of the 8-char hex job id.
- ``cron add`` stores ``channel="cli"`` / ``to="direct"`` by default
  (ephemeral); delivery target is resolved at trigger time per
  ``cron.forward_channels`` (see ``raven cron config``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from raven.cli._log_silence import mute_subsystem_logs_unless_debug
from raven.config.paths import get_cron_dir
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronJob, CronSchedule

if TYPE_CHECKING:
    pass


cron_app = typer.Typer(
    help="Inspect and manage scheduled cron jobs (~/.raven/cron/jobs.json)",
    no_args_is_help=True,
)
console = Console()


@cron_app.callback()
def _suppress_info_logs() -> None:
    """Mute raven subsystem INFO logs so the CLI table output stays
    clean. Set ``RAVEN_CLI_DEBUG=1`` to keep all logs."""
    mute_subsystem_logs_unless_debug()


# ── helpers ───────────────────────────────────────────────────────────


def _open_service() -> CronService:
    """Build a read/write CronService for one CLI invocation. Explicit
    ``allowed_channels=None`` so the CLI's intent — see and manipulate
    jobs across all channels — survives a future default change in
    CronService that might tighten to a restrictive default. Gateway
    keeps the actual delivery routing."""
    return CronService(get_cron_dir() / "jobs.json", allowed_channels=None)


def _format_schedule(schedule: CronSchedule) -> str:
    if schedule.kind == "at":
        if schedule.at_ms is None:
            return "at <unset>"
        ts = datetime.fromtimestamp(schedule.at_ms / 1000)
        return f"at {ts.strftime('%Y-%m-%d %H:%M')}"
    if schedule.kind == "every":
        return f"every {schedule.every_ms // 1000 if schedule.every_ms else '?'}s"
    if schedule.kind == "cron":
        tz = f" tz={schedule.tz}" if schedule.tz else ""
        return f"cron '{schedule.expr}'{tz}"
    return schedule.kind


def _format_next_run(j: CronJob) -> str:
    if j.state.next_run_at_ms is None:
        return "-"
    ts = datetime.fromtimestamp(j.state.next_run_at_ms / 1000)
    return ts.strftime("%Y-%m-%d %H:%M")


def _format_silent(j: CronJob) -> str:
    n = j.state.silent_fire_count
    if n == 0:
        return "0"
    if n >= 5:
        return f"[yellow]{n} ⚠[/yellow]"
    return str(n)


def _resolve_id(service: CronService, prefix: str, *, include_disabled: bool = True) -> CronJob:
    """Find a job by full id or unique prefix. Exits with friendly
    error on no-match or ambiguous-prefix."""
    jobs = service.list_jobs(include_disabled=include_disabled)
    matches = [j for j in jobs if j.id.startswith(prefix)]
    if not matches:
        console.print(f"[red]No job matching id/prefix {prefix!r}[/red]")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        cands = ", ".join(j.id for j in matches[:8])
        console.print(f"[red]Ambiguous prefix {prefix!r} — candidates: {cands}[/red]")
        raise typer.Exit(code=1)
    return matches[0]


def _confirm_destructive(prompt: str, *, yes: bool) -> bool:
    """Common destructive-op confirm. Returns True if user approves."""
    if yes:
        return True
    return typer.confirm(prompt, default=False)


import re

_DURATION_RE = re.compile(r"(\d+)(s|m|h|d)")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> int:
    """Parse a Go/systemd-style duration string into seconds.

    Accepts: '30s', '90s', '5m', '1h', '1h30m', '2h15m30s', '7d', '1d12h'.
    Units: s (seconds), m (minutes), h (hours), d (days).

    Rejects:
      - 'ms' / 'us' / 'ns' — sub-second precision is unreliable on this
        scheduler (tick frequency + delivery chain add hundreds of ms of
        jitter; sub-second is a footgun).
      - 'w' / 'mo' / 'y' — lengths not fixed (28-31 day months, leap
        years). For weekly / monthly / yearly anchors use --cron.
      - decimals, bare numbers, zero, empty.

    Note: --every and --cron are NOT substitutes. --every triggers at
    fixed intervals from the last run (drifts across calendar). --cron
    matches absolute time-of-day / day-of-week (calendar-anchored).
    """
    s = (value or "").strip()
    if not s:
        raise typer.BadParameter("duration cannot be empty")
    if s.endswith(("ms", "us", "ns")):
        raise typer.BadParameter(f"smallest unit is seconds (s); did you mean '1s' instead of {value!r}?")
    if "." in s:
        raise typer.BadParameter(f"only integer values supported; e.g. '90m' instead of {value!r}")
    if s.isdigit():
        raise typer.BadParameter(f"missing unit suffix; did you mean '{s}s'?")

    total = 0
    i = 0
    while i < len(s):
        m = _DURATION_RE.match(s, i)
        if m is None or m.start() != i:
            raise typer.BadParameter(f"invalid duration syntax: {value!r}")
        num, unit = int(m.group(1)), m.group(2)
        total += num * _DURATION_UNITS[unit]
        i = m.end()

    if total <= 0:
        raise typer.BadParameter("interval must be positive")
    return total


# ── list ──────────────────────────────────────────────────────────────


@cron_app.command("list")
def cron_list(
    all_: bool = typer.Option(
        False,
        "--all/--enabled-only",
        help="Include disabled jobs (default hides them)",
    ),
):
    """List scheduled cron jobs.

    Helps answer:
    - "What reminders does the user currently have?"
    - "Which jobs are firing too often (silent-fire ≥ 5)?"
    - "When's the next wake-up?"

    Default hides disabled jobs; pass ``--all`` to include them.
    """
    service = _open_service()
    status = service.status()
    jobs = service.list_jobs(include_disabled=all_)

    # Service-level banner (folds the standalone `cron status` cmd)
    enabled_count = sum(1 for j in service.list_jobs(include_disabled=True) if j.enabled)
    total_count = status["jobs"]
    next_wake_ms = status.get("next_wake_at_ms")
    if next_wake_ms is None:
        next_wake_str = "no scheduled fires"
    else:
        next_dt = datetime.fromtimestamp(next_wake_ms / 1000)
        delta = next_dt - datetime.now()
        secs = int(delta.total_seconds())
        if secs <= 0:
            next_wake_str = f"due ({next_dt.strftime('%H:%M')})"
        elif secs < 3600:
            next_wake_str = f"in {secs // 60}m ({next_dt.strftime('%H:%M')})"
        else:
            hours = secs // 3600
            mins = (secs % 3600) // 60
            next_wake_str = f"in {hours}h {mins}m ({next_dt.strftime('%Y-%m-%d %H:%M')})"

    console.print(
        f"[dim]Cron service: {total_count} jobs "
        f"({enabled_count} enabled, {total_count - enabled_count} disabled), "
        f"next wake {next_wake_str}[/dim]"
    )

    if not jobs:
        console.print("[dim]  (no jobs to list — pass --all to include disabled)[/dim]")
        return

    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Schedule")
    table.add_column("Next Run")
    table.add_column("Last", style="dim")
    table.add_column("Silent", justify="right")
    table.add_column("Channel")
    table.add_column("Name")
    for j in jobs:
        last_status = j.state.last_status or "-"
        last_styled = (
            f"[red]{last_status}[/red]"
            if last_status == "error"
            else f"[green]{last_status}[/green]"
            if last_status == "ok"
            else last_status
        )
        id_display = j.id if j.enabled else f"[dim]{j.id} (off)[/dim]"
        table.add_row(
            id_display,
            _format_schedule(j.schedule),
            _format_next_run(j),
            last_styled,
            _format_silent(j),
            j.payload.channel or "-",
            (j.name or "-")[:36],
        )
    console.print(table)


# ── show ──────────────────────────────────────────────────────────────


@cron_app.command("get")
def cron_get(
    id_prefix: str = typer.Argument(..., metavar="ID", help="Job id or unique prefix"),
):
    """Show full detail of one cron job (schedule, payload, state,
    silent-fire counter, claim status).

    Helps answer:
    - "Why is this job firing weird?"
    - "Has anyone claimed this job?"
    - "What's the last_error?"
    """
    service = _open_service()
    job = _resolve_id(service, id_prefix)

    table = Table(title=f"Cron job '{job.name or '(unnamed)'}'", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("ID", job.id)
    table.add_row("Name", job.name or "-")
    table.add_row("Enabled", "[green]True[/green]" if job.enabled else "[red]False[/red]")
    table.add_row("Schedule", _format_schedule(job.schedule))
    table.add_row("delete_after_run", str(job.delete_after_run))
    table.add_row("silent_fire_limit", str(job.silent_fire_limit) if job.silent_fire_limit else "-")

    # Payload
    table.add_row("─ Payload ─", "")
    table.add_row("kind", job.payload.kind)
    table.add_row("channel", job.payload.channel or "-")
    table.add_row("to", job.payload.to or "-")
    table.add_row("deliver", str(job.payload.deliver))
    table.add_row("topic_tag", job.payload.topic_tag or "-")
    table.add_row(
        "message",
        (job.payload.message[:200] + ("…" if len(job.payload.message) > 200 else "")) if job.payload.message else "-",
    )

    # State
    table.add_row("─ State ─", "")
    table.add_row("next_run_at", _format_next_run(job))
    table.add_row(
        "last_run_at",
        datetime.fromtimestamp(job.state.last_run_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if job.state.last_run_at_ms
        else "-",
    )
    table.add_row("last_status", job.state.last_status or "-")
    if job.state.last_error:
        table.add_row("last_error", f"[red]{job.state.last_error[:200]}[/red]")
    table.add_row("silent_fire_count", _format_silent(job))
    if job.state.claimed_by_pid:
        table.add_row("claimed_by_pid", str(job.state.claimed_by_pid))
        if job.state.claimed_at_ms:
            ts = datetime.fromtimestamp(job.state.claimed_at_ms / 1000)
            table.add_row("claimed_at", ts.strftime("%Y-%m-%d %H:%M:%S"))

    table.add_row("─ Timestamps ─", "")
    if job.created_at_ms:
        table.add_row(
            "created_at",
            datetime.fromtimestamp(job.created_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        )
    if job.updated_at_ms:
        table.add_row(
            "updated_at",
            datetime.fromtimestamp(job.updated_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        )

    console.print(table)


# ── remove ────────────────────────────────────────────────────────────


@cron_app.command("delete")
def cron_delete(
    id_prefix: str = typer.Argument(..., metavar="ID", help="Job id or unique prefix"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Remove a cron job (destructive — primary escape hatch when the
    agent's CronTool is misbehaving).

    Helps answer:
    - "Agent went haywire and made spam reminders, how do I clean up?"
    - "Need to remove a stale 'at' job that never fires."
    """
    service = _open_service()
    job = _resolve_id(service, id_prefix)

    sched = _format_schedule(job.schedule)
    if not _confirm_destructive(
        f"Remove cron job '{job.name or job.id}' ({sched})?",
        yes=yes,
    ):
        console.print("[dim]aborted[/dim]")
        raise typer.Exit(code=1)

    if service.remove_job(job.id):
        console.print(f"[green]✓[/green] Removed job {job.id}")
    else:
        console.print(f"[red]Failed to remove {job.id} (may have been removed by another process)[/red]")
        raise typer.Exit(code=1)


# ── enable / disable ──────────────────────────────────────────────────


@cron_app.command("enable")
def cron_enable(
    id_prefix: str = typer.Argument(..., metavar="ID", help="Job id or unique prefix"),
):
    """Enable a previously-disabled cron job.

    Helps answer:
    - "I disabled a routine reminder during vacation, how do I resume?"
    """
    service = _open_service()
    job = _resolve_id(service, id_prefix)
    if job.enabled:
        console.print(f"[dim]Job {job.id} is already enabled.[/dim]")
        return
    if service.enable_job(job.id, enabled=True):
        console.print(f"[green]✓[/green] Enabled job {job.id} ({_format_schedule(job.schedule)})")
    else:
        console.print(f"[red]Failed to enable {job.id}[/red]")
        raise typer.Exit(code=1)


@cron_app.command("disable")
def cron_disable(
    id_prefix: str = typer.Argument(..., metavar="ID", help="Job id or unique prefix"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Disable a cron job without deleting it (paused — preserves intent
    for later re-enable).

    Helps answer:
    - "Pause my morning standup reminder during a 1-week vacation."
    """
    service = _open_service()
    job = _resolve_id(service, id_prefix)
    if not job.enabled:
        console.print(f"[dim]Job {job.id} is already disabled.[/dim]")
        return
    if not _confirm_destructive(
        f"Disable cron job '{job.name or job.id}' ({_format_schedule(job.schedule)})?",
        yes=yes,
    ):
        console.print("[dim]aborted[/dim]")
        raise typer.Exit(code=1)
    if service.enable_job(job.id, enabled=False):
        console.print(f"[green]✓[/green] Disabled job {job.id}")
    else:
        console.print(f"[red]Failed to disable {job.id}[/red]")
        raise typer.Exit(code=1)


# ── run ───────────────────────────────────────────────────────────────


@cron_app.command("run")
def cron_run(
    id_prefix: str = typer.Argument(..., metavar="ID", help="Job id or unique prefix"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even if disabled (default skips disabled jobs)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the state-mutation confirm prompt"),
):
    """Test-fire a cron job: claim + execute the schedule path with
    delivery stubbed out, **and advance persistent job state as if the
    fire really happened**.

    State changes that DO take effect (this is not a true dry-run):
      • One-shot ``at`` jobs with ``delete_after_run=True`` are
        REMOVED from the store.
      • One-shot ``at`` jobs with ``delete_after_run=False`` are
        DISABLED + ``next_run_at_ms`` cleared.
      • Recurring ``cron`` / ``every`` jobs have ``next_run_at_ms``
        advanced to the next computed fire time, and ``last_run_at_ms`` /
        ``last_status`` are updated as if the fire succeeded.

    Only the message *delivery* is stubbed (CLI has no AgentLoop /
    channel wiring), so the user never sees the reminder pop up — but
    the job's place in the schedule moves forward.

    Helps answer:
    - "Will my cron schedule resolve to the time I expect after one fire?"
    - "Does the on_job hook actually get invoked for this job?"

    NOTE: a one-shot reminder you test-fire with ``cron run`` is gone
    afterwards. To preview *without* mutation, use ``cron get <id>``.
    """
    service = _open_service()
    job = _resolve_id(service, id_prefix)
    if not job.enabled and not force:
        console.print(f"[red]Job {job.id} is disabled — pass --force to run anyway.[/red]")
        raise typer.Exit(code=1)

    # Gateway-running detection (best-effort heuristic). Surface BEFORE
    # the state-mutation confirm so it's part of the user's decision
    # context — they should know the test-fire might race a real
    # gateway claim before they say "y".
    #
    # Two signals, OR'd, because each on its own has gaps:
    #
    #   1. Active claim within the ``gateway_heartbeat_s`` window —
    #      precise: "gateway is currently mid-run on some job". Misses
    #      idle gateway between ticks. Distinct from CronService's own
    #      ``_CLAIM_TTL_MS = 30 * 60 * 1000`` (30min, the time before
    #      a stale claim becomes forcibly releasable). We use a much
    #      tighter 60s window because we want to know if a *live*
    #      gateway is here right now — not whether some crashed peer
    #      left an orphan claim 25 minutes ago.
    #
    #   2. jobs.json mtime within the same window — broader: catches
    #      recent claim acquire/release writes even after the claim
    #      column was cleared. False-positives after our own
    #      cron add / remove / disable, but those still warrant the
    #      warning ("you might be racing yourself" is honest).
    #
    # Neither signal catches a gateway sleeping for an hour between
    # widely-spaced jobs — there's no heartbeat protocol to fix that
    # without out-of-scope service changes.
    from time import time as _time

    gateway_heartbeat_s = 60
    gateway_heartbeat_ms = gateway_heartbeat_s * 1000
    now_s = _time()
    now_ms = int(now_s * 1000)
    has_live_claim = any(
        j.state.claimed_by_pid is not None
        and j.state.claimed_at_ms is not None
        and (now_ms - j.state.claimed_at_ms) < gateway_heartbeat_ms
        for j in service.list_jobs(include_disabled=True)
    )
    jobs_path = service.store_path
    mtime_recent = jobs_path.exists() and (now_s - jobs_path.stat().st_mtime) < gateway_heartbeat_s
    if has_live_claim or mtime_recent:
        why = "active claim in the last 60s" if has_live_claim else "jobs.json modified in the last 60s"
        console.print(
            f"[yellow]⚠ Possible gateway activity ({why}). CLI run "
            f"competes for the claim via fcntl; if gateway wins, the "
            f"test-fire won't execute.[/yellow]"
        )

    # State-mutation confirm: spell out what changes for THIS job kind so
    # the user can't claim they were misled by a "DRY-RUN" label that
    # didn't actually mean dry-run.
    is_one_shot = job.schedule.kind == "at"
    will_delete = is_one_shot and job.delete_after_run
    will_disable = is_one_shot and not job.delete_after_run
    if will_delete:
        mutation_warn = (
            "[red]⚠ This will REMOVE the job from the store (one-shot 'at' with delete_after_run=True).[/red]"
        )
    elif will_disable:
        mutation_warn = "[yellow]⚠ This will DISABLE the job after firing (one-shot 'at' reminder).[/yellow]"
    else:
        mutation_warn = (
            f"[yellow]⚠ This will advance next_run_at and write "
            f"last_run_at as if the fire succeeded "
            f"({_format_schedule(job.schedule)}).[/yellow]"
        )
    console.print(mutation_warn)
    if not _confirm_destructive(
        f"Test-fire job '{job.name or job.id}' (message delivery stubbed, but job state advances)?",
        yes=yes,
    ):
        console.print("[dim]aborted[/dim]")
        raise typer.Exit(code=1)

    console.print(f"[cyan][TEST-FIRE][/cyan] {job.id} ({_format_schedule(job.schedule)})")

    delivered: dict[str, Any] = {"called": False}

    async def _stub_on_job(j) -> None:
        delivered["called"] = True
        console.print("[cyan][TEST-FIRE][/cyan] would-deliver (stubbed):")
        console.print(f"    channel = {j.payload.channel}")
        console.print(f"    to      = {j.payload.to}")
        console.print(f"    message = {j.payload.message[:120]}{'…' if len(j.payload.message) > 120 else ''}")

    service.on_job = _stub_on_job
    ok = asyncio.run(service.run_job(job.id, force=force))
    if not ok:
        console.print(f"[red]Failed to run {job.id} (couldn't claim?)[/red]")
        raise typer.Exit(code=1)
    if not delivered["called"]:
        console.print(
            "[yellow]Job state advanced but on_job stub wasn't invoked "
            "(empty job? race lost to gateway?). Check `cron get "
            f"{job.id[:6]}` for current state.[/yellow]"
        )
    else:
        # Closing message reflects what *actually* happened: delivery
        # was stubbed, but state moved forward.
        if will_delete:
            tail = "Job has been REMOVED from the store."
        elif will_disable:
            tail = "Job has been DISABLED; re-enable with `cron enable`."
        else:
            updated = next(
                (j for j in service.list_jobs(include_disabled=True) if j.id == job.id),
                None,
            )
            if updated is not None:
                tail = f"next_run advanced to {_format_next_run(updated)}."
            else:
                tail = "Job state advanced (check `cron get`)."
        console.print(f"[dim](message delivery was stubbed — user did not see the reminder. {tail})[/dim]")


# ── add ───────────────────────────────────────────────────────────────


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", help="Short reminder name (≤ 30 chars)"),
    message: str = typer.Option(..., "--message", help="Reminder text the user will see"),
    cron: str = typer.Option(
        None,
        "--cron",
        help="Cron expression for clock-based recurring jobs (e.g. '0 9 * * *')",
    ),
    at_iso: str = typer.Option(
        None,
        "--at",
        help="ISO datetime for one-shot jobs (e.g. '2026-05-15T08:00:00')",
    ),
    every: str = typer.Option(
        None,
        "--every",
        help="Interval duration for fixed-interval recurring jobs (e.g. '7s', '5m', '1h30m', '7d'; units s/m/h/d)",
    ),
    tz: str = typer.Option(
        None,
        "--tz",
        help="IANA timezone for cron expressions (e.g. 'Asia/Shanghai')",
    ),
    channel: str = typer.Option(
        None,
        "--channel",
        help="Delivery channel; defaults to 'cli' (ephemeral, routed at trigger time via cron.forward_channels)",
    ),
    to: str = typer.Option(
        None,
        "--to",
        help="Recipient chat_id; defaults to 'direct' for ephemeral cli channel",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts"),
):
    """Create a cron job from explicit flags (advanced / scripting path).

    For interactive use, prefer ``raven agent`` — the LLM understands
    natural language ("remind me to take my meds at 9 every day") and routes through the same
    CronService backend. Use this CLI ``add`` when:

    - Scripting / dotfiles batch setup
    - LLM is unavailable but you need to create a reminder
    - You want a precise cron expression with no LLM-misinterpretation risk
    - Recovering jobs from a backup

    Schedule is one of ``--cron`` / ``--at`` / ``--every`` (mutually
    exclusive, exactly one required). ``--every`` accepts duration
    strings with units ``s`` / ``m`` / ``h`` / ``d`` (e.g. ``7s`` /
    ``5m`` / ``1h30m`` / ``7d``); sub-second units and weeks/months/
    years are rejected (the latter belong under ``--cron`` for
    calendar-anchored schedules).

    When ``--channel`` / ``--to`` are omitted, the job is stored as
    ``channel="cli"`` / ``to="direct"`` (ephemeral) and routed at
    trigger time according to ``cron.forward_channels`` — see
    ``raven cron config get`` for current routing.
    """
    # Validate schedule: exactly one of the three
    schedule_flags = [
        ("--cron", cron),
        ("--at", at_iso),
        ("--every", every),
    ]
    set_flags = [name for name, val in schedule_flags if val is not None and val != ""]
    if len(set_flags) != 1:
        if not set_flags:
            console.print("[red]Need exactly one schedule flag: --cron OR --at OR --every[/red]")
        else:
            console.print(f"[red]Pass exactly one of --cron / --at / --every (got {set_flags})[/red]")
        raise typer.Exit(code=2)

    # Build CronSchedule
    if cron:
        # Validate cron expression syntax up-front. Without this, a typo
        # like `--cron "0 9 * *"` (missing field) silently creates a
        # job whose _compute_next_run swallows the exception and returns
        # None — the job would then never fire. Fail-fast at the CLI
        # boundary instead.
        try:
            from croniter import croniter

            croniter(cron)
        except Exception as exc:
            console.print(f"[red]Invalid cron expression {cron!r}: {exc}[/red]")
            raise typer.Exit(code=2)
        # Validate timezone if provided
        if tz:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:
                console.print(f"[red]Unknown timezone: {tz!r}[/red]")
                raise typer.Exit(code=2)
        schedule = CronSchedule(kind="cron", expr=cron, tz=tz)
        delete_after = False
    elif at_iso:
        try:
            dt = datetime.fromisoformat(at_iso)
        except ValueError:
            console.print(f"[red]Invalid ISO datetime: {at_iso!r} (expected YYYY-MM-DDTHH:MM:SS)[/red]")
            raise typer.Exit(code=2)
        # A naive `at` + tz means "that wall-clock time in tz"; anchor it so
        # .timestamp() does not fall back to the host's local zone. An
        # offset-aware string carries its own zone, so tz is ignored.
        if dt.tzinfo is None and tz:
            try:
                from zoneinfo import ZoneInfo

                dt = dt.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                console.print(f"[red]Unknown timezone: {tz!r}[/red]")
                raise typer.Exit(code=2)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        delete_after = True
    else:
        every_seconds = _parse_duration(every)
        schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        delete_after = False

    if channel is None:
        channel = "cli"
    if to is None:
        to = "direct"

    service = _open_service()
    try:
        job = service.add_job(
            name=name[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=channel,
            to=to,
            delete_after_run=delete_after,
        )
    except ValueError as exc:
        # The service rejects a non-runnable schedule (at in the past,
        # every <= 0, invalid cron expr) rather than storing a job that
        # silently never fires.
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"[green]✓[/green] Created job '{job.name}' (id: {job.id})")


# ── config (cron section read/write) ──────────────────────────────────


cron_config_app = typer.Typer(
    help="Read / write the cron section of ~/.raven/config.json",
    no_args_is_help=True,
)
cron_app.add_typer(cron_config_app, name="config")


def _parse_forward_channels(value: str) -> list[str]:
    """Parse a CLI value into ``cron.forward_channels``.

    Accepts:
      - ``"*"`` / ``"all"`` → ``["*"]``                 (broadcast)
      - ``""``  / ``"none"`` → ``[]``                   (no delivery)
      - CSV (``"telegram,feishu"``) → ``["telegram", "feishu"]``
    """
    raw = value.strip()
    if raw in ("", "none"):
        return []
    if raw in ("*", "all"):
        return ["*"]
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("list value parsed to empty after stripping")
    return parts


def _parse_timezone(value: str) -> str:
    """Validate via zoneinfo; pass through on success."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz = value.strip()
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz!r}") from exc
    return tz


_KEY_HANDLERS: dict[str, dict[str, Any]] = {
    "forward_channels": {
        "parse": _parse_forward_channels,
        "display": lambda v: ",".join(v) if v else "(none)",
    },
    "default_timezone": {
        "parse": _parse_timezone,
        "display": lambda v: v,
    },
}


@cron_config_app.command("get")
def cron_config_get(
    forward_channels: bool = typer.Option(
        False,
        "--forward-channels",
        help="Show only forward_channels value",
    ),
    default_timezone: bool = typer.Option(
        False,
        "--default-timezone",
        help="Show only default_timezone value",
    ),
) -> None:
    """Show cron config — no flags lists every key as a table;
    each flag prints just that key's value on its own line (handy
    for shell substitution)."""
    from raven.config.loader import load_config

    config = load_config()
    selected = [
        k
        for k, picked in (
            ("forward_channels", forward_channels),
            ("default_timezone", default_timezone),
        )
        if picked
    ]
    if not selected:
        table = Table(title="cron config (effective values)", show_lines=False)
        table.add_column("key", style="cyan")
        table.add_column("value")
        for k, handler in _KEY_HANDLERS.items():
            value = getattr(config.cron, k)
            table.add_row(k, handler["display"](value))
        console.print(table)
        return
    for k in selected:
        value = getattr(config.cron, k)
        console.print(_KEY_HANDLERS[k]["display"](value))


@cron_config_app.command("set")
def cron_config_set(
    forward_channels: str | None = typer.Option(
        None,
        "--forward-channels",
        help="New forward_channels (CSV / '*' / 'all' / 'none' / '')",
    ),
    default_timezone: str | None = typer.Option(
        None,
        "--default-timezone",
        help="New default_timezone (any zoneinfo name, e.g. Asia/Shanghai)",
    ),
) -> None:
    """Patch one or more cron config keys on-disk. Effective on the
    next cron fire — closures in the gateway read config fresh each
    tick, no restart needed. At least one --flag is required."""
    from raven.config.update import update_cron_config

    raw_updates: list[tuple[str, str]] = []
    if forward_channels is not None:
        raw_updates.append(("forward_channels", forward_channels))
    if default_timezone is not None:
        raw_updates.append(("default_timezone", default_timezone))

    if not raw_updates:
        console.print("[red]Must specify at least one flag, e.g. --forward-channels / --default-timezone.[/red]")
        raise typer.Exit(1)

    # Parse all first so a bad value never half-writes.
    parsed_updates: list[tuple[str, Any, dict[str, Any]]] = []
    for key, raw in raw_updates:
        handler = _KEY_HANDLERS[key]
        try:
            parsed = handler["parse"](raw)
        except ValueError as exc:
            console.print(f"[red]Invalid value for {key}: {exc}[/red]")
            raise typer.Exit(1)
        parsed_updates.append((key, parsed, handler))

    for key, parsed, handler in parsed_updates:
        prev = update_cron_config(key, parsed)
        console.print(f"[green]✓[/green] cron.{key} → {handler['display'](parsed)} (was {prev!r})")


@cron_config_app.command("reset")
def cron_config_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirm prompt"),
) -> None:
    """Reset the whole cron section to schema defaults
    (``forward_channels=['*']``, ``default_timezone='Asia/Shanghai'``)."""
    from raven.config.update import reset_cron_config

    if not _confirm_destructive(
        "Reset all cron config to defaults?",
        yes=yes,
    ):
        console.print("[yellow]Aborted.[/yellow]")
        return
    reset_cron_config()
    console.print("[green]✓[/green] cron config reset; defaults take effect on next load.")


__all__ = ["cron_app"]
