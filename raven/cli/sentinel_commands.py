"""Sentinel subcommands — owns the ``sentinel_app`` Typer instance.

Bundles all ``raven sentinel ...`` subcommands for inspecting and driving
the proactivity subsystem:

- ``sentinel status``           — show config + current NudgePolicy state
- ``sentinel enable``           — flip sentinel.enabled=true (next start)
- ``sentinel disable``          — flip sentinel.enabled=false (next start)
- ``sentinel config set``       — patch nudge-policy quotas (next start)
- ``sentinel tick``             — fire a single Sentinel tick
- ``sentinel ticks``            — batch-run Sentinel ticks over a range
- ``sentinel nudges``           — inspect pending / recent nudges
- ``sentinel decisions``        — list pending decisions
- ``sentinel routines``         — list learned routines
- ``sentinel discover-now``     — [internal] force-trigger TaskDiscoverer
                                   (used by proactivity-eval longrun)

``commands.py`` imports :data:`sentinel_app` and registers it on the top-level
``app`` via ``app.add_typer(sentinel_app, name="sentinel")``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

console = Console()

from raven.cli._helpers import make_provider, parse_fake_now
from raven.config.paths import get_sentinel_dir, get_workspace_path



sentinel_app = typer.Typer(help="Inspect and drive the proactivity subsystem")


async def _headless_nudge_sink(_out: object) -> None:
    """Accept a Sentinel nudge when no channel outlet is wired.

    The spine DeliveryHub + outlets are built only inside the gateway/REPL run
    loops (where ``dispatcher.set_post(hub.post)`` is called). The headless
    ``sentinel tick``/``ticks`` CLI has no channel to deliver to, so this sink
    lets the executor finish dispatch and report ``delivered=True``. Without it
    every policy-approved nudge dies as ``no_post`` — which silently zeroed
    proactivity-eval's anticipatory (Type A) score.
    """
    return None


@sentinel_app.callback()
def _sentinel_suppress_info_logs() -> None:
    """Mute raven subsystem INFO logs so CLI table output stays clean.
    Set ``RAVEN_CLI_DEBUG=1`` to keep all logs."""
    from raven.cli._log_silence import mute_subsystem_logs_unless_debug

    mute_subsystem_logs_unless_debug()


def _load_sentinel_config():
    from raven.config.raven import load_raven_config

    return load_raven_config()


def _build_sentinel_config_view(sentinel_cfg) -> dict:
    s = sentinel_cfg
    if s is None:
        return {"enabled": False, "note": "no sentinel block"}
    np = s.nudge_policy
    return {
        "enabled": s.enabled,
        "inject_enabled": getattr(s, "inject_enabled", True),
        "defer_enabled": getattr(s, "defer_enabled", True),
        "max_nudges_per_hour": np.max_nudges_per_hour,
        "max_nudges_per_day": getattr(np, "max_nudges_per_day", 10),
        "min_interval_seconds": np.min_interval_seconds,
        "quiet_hours": tuple(np.quiet_hours),
        "dedup_window_seconds": getattr(np, "dedup_window_seconds", 86400),
        "inject_ttl_seconds": getattr(np, "inject_ttl_seconds", 1800),
        "defer_idle_threshold_seconds": getattr(np, "defer_idle_threshold_seconds", 300),
        "defer_max_wait_seconds": getattr(np, "defer_max_wait_seconds", 86400),
    }


@sentinel_app.command("status")
def sentinel_status():
    """Show Sentinel configuration + current NudgePolicy state."""
    ec_config = _load_sentinel_config()
    view = _build_sentinel_config_view(ec_config.sentinel)

    table = Table(title="Sentinel Status", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    for k, v in view.items():
        table.add_row(k, str(v))
    console.print(table)

    if not view.get("enabled"):
        console.print(
            "[dim]Sentinel is currently disabled. "
            "Set sentinel.enabled=true in config to activate.[/dim]"
        )


@sentinel_app.command("enable")
def sentinel_enable():
    """Enable the Sentinel proactivity engine (persists sentinel.enabled=true).

    Master switch is read once at process start, so this takes effect on the
    next ``raven agent`` / ``raven gateway`` start, not on a running one.
    """
    from raven.config.update import set_sentinel_enabled

    prev = set_sentinel_enabled(True)
    if prev:
        console.print("[dim]Sentinel is already enabled.[/dim]")
        return
    console.print(
        "[green]✓[/green] Sentinel enabled (sentinel.enabled=true). "
        "Restart the agent/gateway to start the engine."
    )


@sentinel_app.command("disable")
def sentinel_disable():
    """Disable the Sentinel proactivity engine (persists sentinel.enabled=false).

    Takes effect on the next ``raven agent`` / ``raven gateway`` start;
    a process already running keeps ticking until it restarts.
    """
    from raven.config.update import set_sentinel_enabled

    prev = set_sentinel_enabled(False)
    if not prev:
        console.print("[dim]Sentinel is already disabled.[/dim]")
        return
    console.print(
        "[green]✓[/green] Sentinel disabled (sentinel.enabled=false). "
        "Restart the agent/gateway to stop the engine."
    )


@sentinel_app.command("tick")
def sentinel_tick(
    workspace: str = typer.Option(None, "--workspace", "-w", help="Override workspace path"),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--live",
        help="dry-run (default) skips executor dispatch; --live actually fires",
    ),
    fake_now: str | None = typer.Option(
        None,
        "--fake-now",
        help=(
            "ISO-8601 timestamp to freeze 'now' for Sentinel components in "
            "this tick. Used by the proactivity-eval subprocess harness."
        ),
    ),
):
    """Run a single Sentinel tick (assemble + Planner.decide + dispatch).

    Useful for debugging: prints the decision and which route ran. In
    --dry-run mode, the decision is produced but executors are NOT wired,
    so nothing actually sends. --live does full dispatch through the
    executors.
    """
    import asyncio as _asyncio

    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.proactive_engine.sentinel import (
        DeferManager,
        NudgeDispatcher,
        NudgeInjector,
        NudgePolicy,
        ProactivePlanner,
    )
    from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
    from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner
    from raven.proactive_engine.sentinel.executor.runner import SentinelRunner

    ec_config = _load_sentinel_config()
    base_config = ec_config.base
    ws = Path(workspace) if workspace else get_workspace_path()
    provider = make_provider(base_config)
    # make_provider returns a tuple in some versions; normalise.
    if isinstance(provider, tuple):
        provider = provider[0]
    model = provider.get_default_model()

    # Frozen clock for eval. ``_kwargs`` is unpacked into every constructor
    # that accepts ``now_fn``; empty dict is a no-op for normal operation.
    now_fn = parse_fake_now(fake_now)
    _kwargs = {"now_fn": now_fn} if now_fn is not None else {}

    memory_store = MemoryStore(ws, **_kwargs)
    policy = NudgePolicy(ec_config.sentinel.nudge_policy, **_kwargs)
    learner = RoutineLearner(**_kwargs)

    assembler = ContextAssembler(
        memory_store=memory_store,
        routine_learner=learner,
        nudge_policy=policy,
        **_kwargs,
    )
    planner = ProactivePlanner(provider, model)

    # Wire the attention.md + behaviors.md path so tick_once() refreshes
    # user_memory/attention.md and (optionally) writes
    # user_memory/behaviors.md — without it both files stay at 0 bytes
    # even after a successful Planner cycle.
    from raven.cli._proactive_stack import build_attention_path
    from raven.proactive_engine.sentinel.executor.pending_decision import (
        PendingDecisionStore,
    )
    from raven.proactive_engine.sentinel.feedback.tracker import (
        NudgeFeedbackTracker,
    )
    from raven.proactive_engine.sentinel.predictor.routine_store import (
        RoutineStore,
    )
    from raven.session.manager import SessionManager
    sentinel_dir = get_sentinel_dir()
    session_manager = SessionManager(ws)
    feedback = NudgeFeedbackTracker(sentinel_dir / "feedback.jsonl")
    feedback.load()
    pending_store = PendingDecisionStore(sentinel_dir / "pending_decisions.json")
    routine_store = RoutineStore(sentinel_dir / "routines.json")
    attention_updater, behaviors_extractor = build_attention_path(
        memory_store=memory_store,
        session_manager=session_manager,
        sentinel_cfg=ec_config.sentinel,
        provider=provider,
        model=model,
        feedback=feedback,
        pending_store=pending_store,
        routine_store=routine_store,
        policy=policy,
        now_fn=now_fn,
    )

    if dry_run:
        runner = SentinelRunner(
            planner=planner,
            assembler=assembler,
            policy=policy,
            dispatcher=None,
            injector=None,
            defer_manager=None,
            spawn=None,
            feedback=feedback,
            attention_updater=attention_updater,
            behaviors_extractor=behaviors_extractor,
            **_kwargs,
        )
    else:
        dispatcher = NudgeDispatcher(**_kwargs)
        dispatcher.set_post(_headless_nudge_sink)
        injector = NudgeInjector(**_kwargs)
        # For a one-shot tick we don't need DeferManager's background loop;
        # but provide it so defer decisions register correctly.
        # Sessions not tracked here — defer fires on "no_session" path.
        defer_mgr = DeferManager(dispatcher, session_lookup=lambda _k: None, **_kwargs)
        runner = SentinelRunner(
            planner=planner,
            assembler=assembler,
            policy=policy,
            dispatcher=dispatcher,
            injector=injector,
            defer_manager=defer_mgr,
            spawn=None,
            feedback=feedback,
            attention_updater=attention_updater,
            behaviors_extractor=behaviors_extractor,
            **_kwargs,
        )

    async def _run():
        outcome = await runner.tick_once()
        return outcome

    outcome = _asyncio.run(_run())

    table = Table(title="Sentinel Tick", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    d = outcome.decision
    table.add_row("action", d.action)
    table.add_row("priority", d.priority)
    table.add_row("proactivity_score", f"{d.proactivity_score:.2f}")
    table.add_row("target_session", d.target_session or "-")
    table.add_row("reason", (d.reason or "")[:200])
    if d.nudge_message:
        table.add_row("nudge_message", d.nudge_message[:200])
    if d.spawn_task:
        table.add_row("spawn_task", d.spawn_task[:200])
    table.add_row("route", outcome.route)
    if outcome.result:
        table.add_row("delivered", str(outcome.result.delivered))
        table.add_row("result_reason", outcome.result.reason)
    console.print(table)


@sentinel_app.command("ticks")
def sentinel_ticks(
    from_iso: str = typer.Option(..., "--from", help="ISO-8601 start time (first tick fires AT this time)"),
    to_iso: str = typer.Option(..., "--to", help="ISO-8601 end time (last tick fires AT or BEFORE this time)"),
    interval_seconds: int = typer.Option(1800, "--interval-seconds", help="Step between ticks (default 30 min)"),
    workspace: str = typer.Option(None, "--workspace", "-w", help="Override workspace path"),
    config: str | None = typer.Option(
        None, "--config",
        help="Override config file path. Redirects ALL runtime dirs (sentinel "
             "state.json, cron jobs.json, etc.) to the config's parent dir — "
             "the proactivity-eval parallel longrun uses this for per-persona "
             "state isolation.",
    ),
    live: bool = typer.Option(False, "--live/--dry-run",
                              help="--live wires the executor stack (dispatcher+injector+defer); "
                                   "--dry-run (default) builds Planner only, no dispatch"),
):
    """Run a sequence of Sentinel ticks between ``--from`` and ``--to``.

    Builds the full Sentinel stack ONCE and reuses it across every tick
    in this range. A mutable clock is threaded into all components via
    ``now_fn``; each iteration rebinds the clock to the next tick time.
    Each tick result is emitted as a single JSON object on stdout, one
    per line, so external eval drivers can stream results without waiting
    for the whole range to finish.

    The proactivity-eval longrun harness drives this via
    ``proactivity_eval.RavenDriver.sentinel_ticks(...)`` — collapses
    cold-start overhead by reusing a single subprocess across all ticks.
    """
    import asyncio as _asyncio
    import json as _json
    from datetime import datetime as _dt, timedelta as _td

    from raven.cli._proactive_stack import build_sentinel_stack
    from raven.session.manager import SessionManager

    # ── Honor --config redirect (per-persona isolation for parallel eval) ──
    if config:
        from raven.config.loader import set_config_path
        cfg_path = Path(config).expanduser().resolve()
        if not cfg_path.exists():
            raise typer.BadParameter(f"--config file not found: {cfg_path}")
        set_config_path(cfg_path)

    # ── Parse boundaries ──
    try:
        start = _dt.fromisoformat(from_iso)
        end = _dt.fromisoformat(to_iso)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--from and --to must be ISO-8601 (got from={from_iso!r}, "
            f"to={to_iso!r}): {exc}"
        ) from exc
    if end < start:
        raise typer.BadParameter(f"--to ({to_iso}) must be >= --from ({from_iso})")
    if interval_seconds <= 0:
        raise typer.BadParameter(f"--interval-seconds must be positive (got {interval_seconds})")
    interval = _td(seconds=interval_seconds)

    # ── Mutable clock: rebound per iteration so the stack reads the right
    # fake-now without rebuilding every tick. build_sentinel_stack accepts
    # the bound ``clock.get`` and threads it into every component's now_fn. ──
    class _MutableClock:
        __slots__ = ("now",)
        def __init__(self, t: _dt): self.now = t
        def get(self) -> _dt: return self.now
    clock = _MutableClock(start)

    # ── Build the full Sentinel stack via the shared factory ──
    # Delegating to ``build_sentinel_stack`` keeps this CLI in lock-step
    # with gateway: TaskDiscoverer + task_discovery_targets +
    # routine_aggregator + pending_decisions + late-bind ChannelManager
    # all come along automatically, so the daily 08:00 menu fires inside
    # the batch the same way it would on a live gateway.
    ec_config = _load_sentinel_config()
    base_config = ec_config.base
    if workspace:
        # ``workspace_path`` is a derived property on Config; override the
        # backing field so build_sentinel_stack (which feeds it into
        # MemoryStore) and SessionManager both pick up the -w value.
        base_config.agents.defaults.workspace = str(
            Path(workspace).expanduser()
        )
    provider = make_provider(base_config)
    if isinstance(provider, tuple):
        provider = provider[0]

    session_manager = SessionManager(base_config.workspace_path)
    runner, _resp_modifier, _on_inbound = build_sentinel_stack(
        base_config, ec_config.sentinel,
        session_manager, provider,
        now_fn=clock.get,
    )
    if runner is None:
        raise typer.BadParameter(
            "sentinel.enabled=False in config — nothing to tick. "
            "Set sentinel.enabled=true to use ``sentinel ticks``."
        )

    # --dry-run: keep the Planner LLM path active (so the eval can score
    # decisions) but neutralize executors so no spine dispatch / inject
    # queue / defer-state side effects leak from a benchmark run.
    # task_discoverer also goes here: it owns its own dispatcher reference
    # and would otherwise still write PendingDecision + fire the LLM.
    if not live:
        runner.dispatcher = None
        runner.injector = None
        runner.defer_manager = None
        runner.task_discoverer = None
    elif runner.dispatcher is not None:
        # build_sentinel_stack leaves the dispatcher's hub post unbound (the hub
        # lives in the gateway/REPL loop, not here); wire a headless sink so
        # --live nudges actually report delivered.
        runner.dispatcher.set_post(_headless_nudge_sink)

    # ── Loop ──
    async def _run_all():
        current = start
        while current <= end:
            clock.now = current
            outcome = await runner.tick_with_context(runner.assembler.assemble())
            d = outcome.decision
            line = {
                "fake_now": current.isoformat(),
                "action": d.action,
                "route": outcome.route,
                "delivered": (outcome.result.delivered if outcome.result else None),
                "reason": (d.reason or "")[:500],
                "priority": d.priority,
                "target_session": d.target_session,
                "nudge_message": d.nudge_message,
                "spawn_task": d.spawn_task,
                "proactivity_score": round(d.proactivity_score, 3),
                "topic_tag": d.topic_tag,
            }
            print(_json.dumps(line, ensure_ascii=False), flush=True)
            current += interval

    _asyncio.run(_run_all())


@sentinel_app.command("nudges")
def sentinel_nudges(
    n: int = typer.Option(20, "--n", "-n", help="Show last N feedback events"),
    show_state: bool = typer.Option(
        True,
        "--state/--no-state",
        help="Also dump NudgePolicy persisted state (dedup / quotas / dismissals)",
    ),
):
    """Show recent NudgeFeedbackTracker events + NudgePolicy persisted state.

    Read-only inspector. Helps answer:
    - Which topics has Sentinel been firing on?
    - What's the recent dispatched/accepted/dismissed signal mix?
    - Why is the adaptive hour-quota multiplier where it is?
    - What's currently in the dedup / quota / dismissal-cooldown state?

    Data sources:
    - ~/.raven/sentinel/feedback.jsonl — every dispatched / engagement
      signal (falls back to legacy ``<default-workspace>/sentinel_feedback.jsonl``
      if the sentinel stack hasn't yet migrated it)
    - ~/.raven/sentinel/state.json — NudgePolicy fired_at / topic_fired_at /
      dismissed_at / hour_quota_multiplier (or per-process state.json for eval).
    """
    import json as _json
    from collections import Counter
    from datetime import timedelta

    from raven.config.paths import get_sentinel_dir

    feedback_path = get_sentinel_dir() / "feedback.jsonl"
    if not feedback_path.exists():
        legacy_path = get_workspace_path() / "sentinel_feedback.jsonl"
        if legacy_path.exists():
            feedback_path = legacy_path

    # --- Recent feedback events table ----------------------------------
    if not feedback_path.exists():
        console.print(f"[yellow]No feedback file at {feedback_path}[/yellow]")
        events = []
    else:
        events = []
        with feedback_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(_json.loads(line))
                except _json.JSONDecodeError:
                    continue

    if events:
        table = Table(title=f"Recent NudgeFeedback (last {min(n, len(events))} of {len(events)})")
        table.add_column("Time", style="dim")
        table.add_column("Signal", style="cyan")
        table.add_column("Action")
        table.add_column("Session")
        table.add_column("Priority")
        table.add_column("Score", justify="right")
        for ev in events[-n:]:
            ts = ev.get("ts", "")
            ts_short = ts[:19].replace("T", " ") if isinstance(ts, str) else ""
            sig = ev.get("signal", "?")
            sig_styled = (
                f"[green]{sig}[/green]"
                if sig == "accepted"
                else f"[red]{sig}[/red]"
                if sig == "dismissed"
                else f"[dim]{sig}[/dim]"
                if sig == "ignored"
                else sig
            )
            table.add_row(
                ts_short,
                sig_styled,
                ev.get("action", "-"),
                (ev.get("session_key") or "-")[:24],
                ev.get("priority", "-"),
                f"{ev.get('proactivity_score', 0):.2f}",
            )
        console.print(table)

        # --- 7-day signal counts -----------------------------------------
        cutoff = datetime.now() - timedelta(days=7)
        counts = Counter()
        for ev in events:
            ts = ev.get("ts", "")
            try:
                t = datetime.fromisoformat(ts)
                if t.tzinfo is not None:
                    t = t.replace(tzinfo=None)
                if t < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            counts[ev.get("signal", "?")] += 1
        if counts:
            console.print(
                f"\n[bold]7-day signal counts[/bold]: "
                + ", ".join(f"{k}={v}" for k, v in counts.most_common())
            )
            dispatched = counts.get("dispatched", 0)
            accepted = counts.get("accepted", 0)
            if dispatched:
                console.print(
                    f"  acceptance rate (last 7d): "
                    f"{accepted}/{dispatched} = {100 * accepted / dispatched:.0f}%"
                )

    # --- NudgePolicy persisted state ----------------------------------
    if show_state:
        state_path = get_sentinel_dir() / "state.json"
        if not state_path.exists():
            console.print(f"\n[dim]No NudgePolicy state at {state_path}[/dim]")
            return
        try:
            state = _json.loads(state_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError) as exc:
            console.print(f"\n[red]Failed to read NudgePolicy state: {exc}[/red]")
            return

        policy_state = state.get("nudge_policy", {})
        table2 = Table(title="NudgePolicy state (cross-process)", show_header=False)
        table2.add_column("Field", style="cyan")
        table2.add_column("Value")
        table2.add_row(
            "hour_quota_multiplier",
            f"{policy_state.get('hour_quota_multiplier', 1.0):.2f}",
        )
        fired_at = policy_state.get("fired_at") or []
        table2.add_row("recent fires (count)", str(len(fired_at)))
        topic_fired_at = policy_state.get("topic_fired_at") or {}
        table2.add_row("topics tracked", str(len(topic_fired_at)))
        dismissed_at = policy_state.get("dismissed_at") or {}
        table2.add_row("dismissed topics tracked", str(len(dismissed_at)))
        console.print(table2)

        # Top topics by recent fire count
        if topic_fired_at:
            now_ts = datetime.now().timestamp()
            seven_days = 7 * 86400
            topic_recent = Counter()
            for tag, timestamps in topic_fired_at.items():
                if isinstance(timestamps, list):
                    topic_recent[tag] = sum(
                        1
                        for t in timestamps
                        if isinstance(t, (int, float)) and (now_ts - t) <= seven_days
                    )
            top = [(t, n) for t, n in topic_recent.most_common(8) if n > 0]
            if top:
                console.print("\n[bold]Top topics fired (last 7d)[/bold]:")
                for tag, count in top:
                    console.print(f"  - [yellow]{tag}[/yellow] × {count}")


# ── Inspectors: decisions / discover-now / routines ────────
@sentinel_app.command("decisions")
def sentinel_decisions(
    all_: bool = typer.Option(
        False,
        "--all/--live-only",
        help="Include consumed decisions (default shows only live: pending or awaiting_confirm)",
    ),
    show_options: bool = typer.Option(
        False,
        "--show-options/--no-show-options",
        help="Expand each decision's options inline",
    ),
):
    """List discovery decisions (the menus TaskDiscoverer produced).

    Helps answer:
    - "Did discovery actually fire today / is the menu sitting in the store?"
    - "Why is the user stuck in awaiting_confirm — can I see what they
      picked?"
    - "Have any decisions been auto-expired by TTL without consume?"

    Default hides consumed history; pass ``--all`` to see them.
    """
    from datetime import datetime as _dt

    from raven.config.paths import get_sentinel_dir
    from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore

    store = PendingDecisionStore(get_sentinel_dir() / "pending_decisions.json")
    now_ms = int(_dt.now().timestamp() * 1000)

    if all_:
        decisions = store.all_decisions(include_consumed=True)
    else:
        decisions = store.all_active(now_ms=now_ms)

    if not decisions:
        msg = "No decisions" if all_ else "No live decisions (try --all)"
        console.print(f"[dim]{msg}.[/dim]")
        return

    table = Table(title=(f"Discovery decisions ({len(decisions)} {'total' if all_ else 'live'})"))
    table.add_column("ID", style="cyan")
    table.add_column("Channel")
    table.add_column("To", style="dim")
    table.add_column("State")
    table.add_column("Created")
    table.add_column("Options", justify="right")
    table.add_column("Picked")

    for d in sorted(decisions, key=lambda d: -d.created_at_ms):
        if d.consumed:
            state = f"[green]consumed[/green]" if d.picked_option_id else f"[red]cancelled[/red]"
        elif d.awaiting_confirm:
            state = "[yellow]awaiting_confirm[/yellow]"
        else:
            expired = d.is_expired(now_ms)
            state = "[red]expired[/red]" if expired else "pending"
        created_str = _dt.fromtimestamp(d.created_at_ms / 1000).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            d.decision_id,
            d.channel,
            d.to[:24],
            state,
            created_str,
            str(len(d.options)),
            (d.picked_option_id or "-")[:20],
        )
    console.print(table)

    if show_options:
        for d in sorted(decisions, key=lambda d: -d.created_at_ms):
            console.print(f"\n[cyan]{d.decision_id}[/cyan] ({d.channel}:{d.to}) options:")
            for i, opt in enumerate(d.options, start=1):
                marker = "✓" if d.picked_option_id == opt.id else " "
                console.print(
                    f"  {marker} [{i}] {opt.title}  [dim]({opt.type}/{opt.exec_kind})[/dim]"
                )
                if opt.why:
                    console.print(f"      [dim]— {opt.why}[/dim]")


@sentinel_app.command("discover-now")
def sentinel_discover_now(
    channel: str = typer.Argument(
        ...,
        help=(
            "Target channel name (``feishu`` / ``slack`` / …), or ``*`` "
            "to broadcast. Mirrors ``task_discovery_targets`` syntax."
        ),
    ),
    to: str = typer.Argument(
        "",
        help=(
            "Target chat_id. Optional — leave empty to auto-resolve via "
            "SessionManager (most-recent chat on this channel). Ignored "
            "when channel is ``*``."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the LLM-cost confirmation"),
    inproc: bool = typer.Option(
        False,
        "--inproc",
        help=(
            "Run TaskDiscoverer in this CLI process instead of queuing "
            "a trigger for the gateway. Used by the proactivity-eval "
            "longrun harness which acts as its own gateway. Manual use "
            "won't actually deliver to non-cli channels (the CLI has "
            "no channel adapters)."
        ),
    ),
    workspace: str = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Override workspace path. Only effective with --inproc "
            "(the file-trigger path delegates to the gateway's workspace)."
        ),
    ),
    fake_now: str | None = typer.Option(
        None,
        "--fake-now",
        help=(
            "ISO-8601 timestamp to freeze 'now'. Only effective with "
            "--inproc — used by the proactivity-eval subprocess harness."
        ),
    ),
):
    """[Internal] Force-trigger TaskDiscoverer for a target.

    Target syntax matches ``sentinel.task_discovery_targets``:

    - ``discover-now feishu ou_xxx``  — explicit channel + chat_id
    - ``discover-now feishu``         — chat_id auto-resolved via
                                          SessionManager at fire time
    - ``discover-now '*'``            — broadcast to every enabled channel

    Two modes:

    **Default (file-trigger)**: writes a trigger to
    ``<sentinel_dir>/discover_triggers.json``; the gateway's
    SentinelRunner consumes it on its next tick (≤
    ``tick_interval_seconds``) and runs the LLM + dispatch IN THE
    GATEWAY PROCESS — that's the only place with live ChannelManager
    + feishu/slack adapters wired to the spine (submitting via Intake,
    receiving via the spine outlet). Mirrors cron's jobs.json
    IPC pattern.

    **--inproc**: builds a SentinelRunner here and runs TaskDiscoverer
    locally. Required by the proactivity-eval longrun harness (no
    separate gateway). For manual use this only "works" if your target
    channel is cli — other channels' adapters live in the gateway
    process and won't receive a message delivered within this CLI
    subprocess.

    Helps answer (for eval / debugging):
    - "I want to demo the discovery menu right now — don't wait until 8am."
    - "Gateway was down at 8am, manually fire today's missed menu."

    Inspect after queuing (default mode):
    - ``raven sentinel decisions`` — see the resulting PendingDecision
    - gateway --logs — look for ``Sentinel: consuming N discover trigger(s)``
    - your target channel (feishu / slack / …) for the actual menu
    """
    # Validate config BEFORE the LLM-cost confirm — no point asking the
    # user to spend $0.05 if we'll abort 3 lines later because the
    # feature is disabled.
    ec_config = _load_sentinel_config()
    if not ec_config.sentinel.enabled:
        console.print("[red]sentinel.enabled is False in config[/red]")
        raise typer.Exit(code=1)
    if not ec_config.sentinel.task_discovery_enabled:
        console.print(
            "[red]sentinel.task_discovery_enabled is False — "
            "discovery is opt-in. Enable it in config.json[/red]"
        )
        raise typer.Exit(code=1)

    if not (
        yes
        or typer.confirm(
            "⚠ This will queue a discovery trigger that the gateway will "
            "run on its next tick — Planner LLM cost ~$0.01-0.05. Continue?",
            default=False,
        )
    ):
        console.print("[dim]aborted[/dim]")
        raise typer.Exit(code=1)

    if inproc:
        _sentinel_discover_now_inproc(
            channel=channel, to=to,
            workspace=workspace, fake_now=fake_now,
            ec_config=ec_config,
        )
        return

    # Default mode: write a trigger file under sentinel_dir; gateway's
    # SentinelRunner tick (≤ tick_interval_seconds) consumes it and
    # runs ``runner.discover_now(channel, to)`` IN THE GATEWAY PROCESS
    # — which is where the ChannelManager + feishu/slack adapters live.
    # Running the LLM call here (CLI subprocess) and then dispatching
    # to the gateway's outlets doesn't work — they're separate processes
    # (separate in-memory state). The trigger-file pattern mirrors cron's jobs.json IPC.
    from raven.config.paths import get_sentinel_dir
    from raven.proactive_engine.sentinel.discover_triggers import (
        DiscoverTriggerStore,
    )

    store = DiscoverTriggerStore(get_sentinel_dir() / "discover_triggers.json")
    trigger = store.add(channel=channel, to=to)
    if channel == "*":
        target_desc = "channel='*' (broadcast to all enabled at fire time)"
        watch_channel = "every enabled channel"
    elif not to:
        target_desc = f"channel={channel} to=(auto-resolve at fire time)"
        watch_channel = channel
    else:
        target_desc = f"channel={channel} to={to}"
        watch_channel = channel
    console.print(
        f"[green]✓[/green] trigger queued: id={trigger.id} {target_desc}\n"
        f"  Gateway will fire within ~"
        f"{ec_config.sentinel.tick_interval_seconds}s. Watch:\n"
        f"  • gateway --logs for [cyan]Sentinel: consuming N discover "
        f"trigger(s)[/cyan]\n"
        f"  • [cyan]raven sentinel decisions[/cyan] for the resulting menu\n"
        f"  • {watch_channel} for the actual menu push"
    )


def _sentinel_discover_now_inproc(
    *,
    channel: str,
    to: str,
    workspace: str | None,
    fake_now: str | None,
    ec_config,
) -> None:
    """In-process discover-now (--inproc flag) — used by the
    proactivity-eval longrun harness which acts as its own gateway.

    Builds a minimal SentinelRunner here, runs TaskDiscoverer + LLM +
    dispatch in this process. Output (PendingDecision in
    pending_decisions.json) is what the eval harness inspects to score
    the run.
    """
    import asyncio as _asyncio

    from loguru import logger

    from raven.cli._proactive_stack import build_sentinel_stack
    from raven.session.manager import SessionManager

    config = ec_config.base
    if workspace:
        config.agents.defaults.workspace = workspace
    session_manager = SessionManager(config.workspace_path)
    provider = make_provider(config)

    runner, _resp_modifier, _on_inbound = build_sentinel_stack(
        config,
        ec_config.sentinel,
        session_manager,
        provider,
        now_fn=parse_fake_now(fake_now),
    )
    if runner is None or runner.task_discoverer is None:
        console.print(
            "[red]TaskDiscoverer not wired (sentinel disabled or "
            "task_discovery_enabled=False)[/red]"
        )
        raise typer.Exit(code=1)

    target_label = channel if channel == "*" or not to else f"{channel}:{to}"
    console.print(f"[dim]firing TaskDiscoverer for {target_label}…[/dim]")

    async def _run() -> None:
        try:
            await runner.discover_now(channel, to)
        finally:
            try:
                await runner.stop()
            except Exception as exc:
                logger.warning(
                    "discover-now: runner.stop() raised: {}: {}",
                    type(exc).__name__,
                    exc,
                )

    _asyncio.run(_run())

    console.print(
        f"[green]✓[/green] discover_now invoked (inproc). Inspect with:\n"
        f"  [cyan]raven sentinel decisions[/cyan]"
    )


@sentinel_app.command("routines")
def sentinel_routines(
    status: str = typer.Option(
        None,
        "--status",
        help="Filter by status: candidate / active / retired",
    ),
):
    """List routines from the persistent RoutineStore.

    Helps answer:
    - "Why is this routine pattern not surfacing in discovery menus —
      is it candidate / retired / below threshold?"
    - "Which routines has the user actually confirmed?"
    - "Is a retired routine still in cooldown?"

    Reads ``~/.raven/sentinel/routines.json`` (the persistent store
    written by RoutineStore at Planner runtime — distinct from the
    in-memory RoutineLearner pass that re-derives candidates from
    HISTORY.md without persisting).
    """
    from datetime import datetime as _dt

    from raven.config.paths import get_sentinel_dir
    from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore

    if status not in (None, "candidate", "active", "retired"):
        console.print(f"[red]Unknown --status {status!r} (use candidate / active / retired)[/red]")
        raise typer.Exit(code=2)

    store = RoutineStore(get_sentinel_dir() / "routines.json")
    routines = store.all_routines()
    if status:
        routines = [r for r in routines if r.status == status]

    if not routines:
        msg = "No routines in store" if status is None else f"No routines with status={status}"
        console.print(f"[dim]{msg}.[/dim]")
        return

    table = Table(title=f"Routines ({len(routines)})")
    table.add_column("Status", style="cyan")
    table.add_column("ID")
    table.add_column("Weight", justify="right")
    table.add_column("Count", justify="right")
    table.add_column("Last Triggered", style="dim")
    table.add_column("Description")

    for r in sorted(routines, key=lambda r: (r.status != "active", -r.weight, -r.occurrence_count)):
        status_styled = (
            f"[green]active ✓[/green]"
            if r.status == "active"
            else f"[yellow]candidate[/yellow]"
            if r.status == "candidate"
            else f"[red]retired[/red]"
            if r.status == "retired"
            else r.status
        )
        last = "-"
        if r.last_triggered:
            try:
                t = _dt.fromisoformat(r.last_triggered)
                last = t.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                last = r.last_triggered[:16]
        table.add_row(
            status_styled,
            r.id,
            f"{r.weight:.1f}",
            str(r.occurrence_count),
            last,
            (r.description or "-")[:40],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# attention.md / behaviors.md inspectors


@sentinel_app.command("attention")
def sentinel_attention(
    section: str = typer.Option(
        None, "--section", "-s",
        help="Filter to a single H2 (e.g. '## Pending proposals'). Omit for full file.",
    ),
    workspace: str = typer.Option(
        None, "--workspace", "-w",
        help="Override workspace path (defaults to active).",
    ),
):
    """Show ``user_memory/attention.md`` — all sections or a single one.

    Read-only. Useful for verifying which producers have written and
    eyeballing the diagnostic Sentinel Observations block.
    """
    from raven.memory_engine.consolidate.attention import parse_attention

    ws = Path(workspace) if workspace else get_workspace_path()
    path = ws / "user_memory" / "attention.md"
    if not path.exists():
        console.print(f"[yellow]No attention.md at {path}[/yellow]")
        raise typer.Exit(code=1)
    text = path.read_text(encoding="utf-8")
    if section:
        sections = parse_attention(text)
        body = sections.get(section, "").strip()
        if not body:
            console.print(
                f"[yellow]Section {section!r} absent or empty in "
                f"{path}[/yellow]",
            )
            raise typer.Exit(code=1)
        console.print(f"[bold cyan]{section}[/bold cyan]\n")
        console.print(body)
        return
    console.print(f"[dim]{path}[/dim]\n")
    console.print(text)


@sentinel_app.command("behaviors")
def sentinel_behaviors(
    since: str = typer.Option(
        None, "--since",
        help="ISO date (YYYY-MM-DD) — only events ending on/after this date.",
    ),
    session_key: str = typer.Option(
        None, "--session",
        help="Filter to a single ``channel:chat_id`` session.",
    ),
    folded: bool = typer.Option(
        False, "--folded",
        help="Render folded single-line view (matches PlannerContext format).",
    ),
    workspace: str = typer.Option(
        None, "--workspace", "-w",
        help="Override workspace path.",
    ),
):
    """Show ``user_memory/behaviors.md`` events with optional filtering.

    Without filters, prints the full markdown file. With ``--folded``,
    renders one line per event in the same compact format Planner sees.
    """
    import json as _json
    from raven.memory_engine.consolidate.behaviors import (
        parse_behaviors, render_folded_block,
    )

    ws = Path(workspace) if workspace else get_workspace_path()
    path = ws / "user_memory" / "behaviors.md"
    if not path.exists():
        console.print(f"[yellow]No behaviors.md at {path}[/yellow]")
        raise typer.Exit(code=1)
    text = path.read_text(encoding="utf-8")
    if not since and not session_key and not folded:
        console.print(f"[dim]{path}[/dim]\n")
        console.print(text)
        return

    events = parse_behaviors(text)
    if since:
        try:
            from datetime import date as _date
            since_date = _date.fromisoformat(since)
            events = [
                e for e in events
                if e.day >= since_date.isoformat()
            ]
        except ValueError:
            console.print(
                f"[red]Bad --since {since!r}, expected YYYY-MM-DD[/red]",
            )
            raise typer.Exit(code=2)
    if session_key:
        events = [e for e in events if e.session == session_key]

    if not events:
        console.print("[yellow]No events match the filter.[/yellow]")
        return
    if folded:
        console.print(render_folded_block(events, max_events=10_000))
        return
    console.print(
        f"[dim]{path}[/dim] — {len(events)} event(s) after filter\n",
    )
    for ev in events:
        console.print(
            f"[bold cyan]### {ev.id}[/bold cyan] — "
            f"{ev.day} {ev.start}-{ev.end} · session "
            f"`{ev.session}` · {ev.intent}→{ev.outcome} · "
            f"{ev.topic}{' #' + ev.project if ev.project else ''}"
        )
        if ev.summary:
            console.print(f"  {ev.summary}\n")
        else:
            console.print("")


@sentinel_app.command("behaviors-rebuild")
def sentinel_behaviors_rebuild(
    session_key: str = typer.Option(
        None, "--session",
        help="Restrict to a single ``channel:chat_id`` session.",
    ),
    workspace: str = typer.Option(
        None, "--workspace", "-w",
        help="Override workspace path.",
    ),
):
    """Force a behaviors.md extraction pass, bypassing idle + cooldown gates.

    Uses the workspace-level sentinel config to build a BehaviorsExtractor
    and call ``run_all()``. Honors the ``--session`` filter by only
    advancing offsets for sessions matching the key (other sessions
    keep their current offsets untouched).
    """
    import asyncio

    from raven.config.raven import load_raven_config
    from raven.memory_engine.consolidate.behaviors_extractor import (
        BehaviorsExtractor,
    )
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.session.manager import SessionManager

    ws = Path(workspace) if workspace else get_workspace_path()
    cfg = load_raven_config()
    if not cfg.sentinel.behaviors_extract.enabled:
        console.print(
            "[yellow]behaviors_extract.enabled is False in config — "
            "rebuild will run but no future ticks will refresh.[/yellow]",
        )
    provider_pair = make_provider(cfg.base)
    provider = (
        provider_pair[0] if isinstance(provider_pair, tuple)
        else provider_pair
    )
    model = (
        cfg.sentinel.behaviors_extract.model
        or cfg.sentinel.evaluator_model
        or provider.get_default_model()
    )
    store = MemoryStore(ws)
    session_manager = SessionManager(ws)
    extractor = BehaviorsExtractor(
        memory_store=store,
        session_manager=session_manager,
        provider=provider,
        config=cfg.sentinel.behaviors_extract,
        model=model,
    )
    if session_key:
        # Narrow to a single session by patching SessionManager's
        # sessions_dir lookup — cheaper than threading a filter through
        # run_all. We just iterate the matching file directly.
        from raven.memory_engine.consolidate.behaviors_extractor import (
            BehaviorsOffsets,
        )
        offsets = BehaviorsOffsets.load(store.behaviors_offsets_path)

        async def _run_one() -> int:
            safe_key = session_key.replace(":", "_")
            path = session_manager.sessions_dir / f"{safe_key}.jsonl"
            if not path.exists():
                console.print(
                    f"[red]No session file at {path}[/red]",
                )
                return 0
            return await extractor._extract_one_session(path, offsets)  # noqa: SLF001

        added = asyncio.run(_run_one())
    else:
        added = asyncio.run(extractor.run_all())
    console.print(
        f"[green]Rebuild appended {added} event(s) to "
        f"{store.behaviors_file}[/green]",
    )


# ── config (sentinel section read/write) ──────────────────────────────


sentinel_config_app = typer.Typer(
    help="Read / write the sentinel section of config.json",
    no_args_is_help=True,
)
sentinel_app.add_typer(sentinel_config_app, name="config")


@sentinel_config_app.command("set")
def sentinel_config_set(
    max_nudges_per_hour: int | None = typer.Option(
        None, "--max-nudges-per-hour",
        help="Per-hour proactive-nudge quota (>= 1)",
    ),
    max_nudges_per_day: int | None = typer.Option(
        None, "--max-nudges-per-day",
        help="Per-day proactive-nudge quota — hard ceiling, even high "
             "priority can't bypass (>= 1)",
    ),
):
    """Patch the sentinel nudge-policy quotas on-disk. At least one flag
    required. Effective on the next agent/gateway start.

    Note: these cap Sentinel's own proactive nudges. Cron fires are NOT
    gated by them but DO consume the same hourly/daily counter, so a low
    cap mainly throttles proactive nudges while cron reminders still fire.
    """
    from raven.config.update import set_sentinel_nudge_quota

    if max_nudges_per_hour is None and max_nudges_per_day is None:
        console.print(
            "[red]Specify at least one of --max-nudges-per-hour / "
            "--max-nudges-per-day.[/red]"
        )
        raise typer.Exit(1)
    try:
        changed = set_sentinel_nudge_quota(
            per_hour=max_nudges_per_hour, per_day=max_nudges_per_day,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not changed:
        console.print("[dim]No change — quotas already at those values.[/dim]")
        return
    for field, (prev, new) in changed.items():
        console.print(f"[green]✓[/green] sentinel.{field} → {new} (was {prev!r})")
    if (
        max_nudges_per_hour is not None
        and max_nudges_per_day is not None
        and max_nudges_per_hour > max_nudges_per_day
    ):
        console.print(
            "[yellow]⚠ per-hour > per-day; the per-day ceiling will dominate.[/yellow]"
        )
    console.print("[dim]Effective on next agent/gateway start.[/dim]")


__all__ = ["sentinel_app"]
