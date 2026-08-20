"""``raven trajectory`` subapp — package, label, and protect trajectories.

Trajectories are addressed by attempt id (equal to the trace id for a
single-turn attempt; see :mod:`raven.trajectory`). Every id-taking command
resolves a turn's trace id to its canonical attempt id first, so verdicts,
pins, and bundles always land under the same name. The subcommands are thin
wrappers over the trajectory layer:

- ``save``    — pack one attempt into a self-contained bundle directory
  (:func:`raven.trajectory.bundle.collect_bundle`; auto-pins the id).
- ``verdict`` — record a task-outcome label (source fixed to ``user``).
- ``pin`` / ``unpin`` — grant / revoke the never-purge retention promise.
- ``list``    — aggregate addressable attempts from the trace logs so users
  can find the id they want to bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore
from raven.trajectory.bundle import collect_bundle
from raven.trajectory.verdict import VERDICT_STATUSES, read_verdicts, record_verdict

console = Console()

trajectory_app = typer.Typer(
    help="Package, label, and protect agent trajectories.",
    no_args_is_help=True,
)


def _resolve_or_exit(id_: str) -> str:
    """Map ``id_`` to its canonical attempt id, exiting when no span matches.

    Keeps verdict/pin/unpin on the same address ``save`` uses — a label or
    pin written under a turn's trace id would never be found again by the
    attempt-keyed readers.
    """
    resolved = tstore.resolve_attempt_id(id_)
    if resolved is None:
        console.print(f"[red]no spans found for id {id_!r}[/red]")
        raise typer.Exit(code=1)
    if resolved != id_:
        console.print(f"[dim]{id_} is one turn of attempt {resolved}[/dim]")
    return resolved


@trajectory_app.command("save")
def trajectory_save(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Output directory (default: <trace-state>/bundles)"),
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace holding the session records (default: the configured workspace)",
    ),
) -> None:
    """Pack a trajectory into a self-contained bundle directory (and pin it)."""
    try:
        bundle_dir = collect_bundle(id_, out_dir=out, workspace=workspace)
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    console.print(f"[green]✓[/green] Bundled to [cyan]{bundle_dir}[/cyan]")
    if manifest.get("attempt_id") != id_:
        console.print(
            f"  [dim]{id_} is one turn of attempt {manifest.get('attempt_id')}; bundled the whole attempt[/dim]"
        )
    session_note = "included" if manifest.get("session_included") else "not found (omitted)"
    console.print(
        f"  spans: {manifest.get('span_count')}"
        f"  artifacts: {manifest.get('artifact_count')}"
        f"  verdicts: {manifest.get('verdict_count')}"
        f"  session: {session_note}"
    )
    missing = manifest.get("missing_artifacts") or []
    if missing:
        console.print(f"  [yellow]{len(missing)} referenced artifact(s) missing — listed in manifest.json[/yellow]")


@trajectory_app.command("verdict")
def trajectory_verdict(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    status: str = typer.Option(..., "--status", "-s", help="pass | fail | infra"),
    why: str | None = typer.Option(None, "--why", help="Failure-cause class (free-form)"),
    notes: str | None = typer.Option(None, "--notes", help="Free-form notes"),
) -> None:
    """Record a task-outcome label for an attempt (source: user)."""
    if status not in VERDICT_STATUSES:
        raise typer.BadParameter(f"status must be one of {', '.join(VERDICT_STATUSES)}; got {status!r}")
    attempt_id = _resolve_or_exit(id_)
    record_verdict(attempt_id, status, source="user", why=why, notes=notes)
    console.print(f"[green]✓[/green] Recorded verdict [cyan]{status}[/cyan] for {attempt_id}")


@trajectory_app.command("pin")
def trajectory_pin(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    reason: str = typer.Option("", "--reason", "-r", help="Why this trajectory is kept"),
) -> None:
    """Protect a trajectory from any future purge."""
    attempt_id = _resolve_or_exit(id_)
    tstore.pin(attempt_id, reason=reason)
    console.print(f"[green]✓[/green] Pinned {attempt_id}")


@trajectory_app.command("unpin")
def trajectory_unpin(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
) -> None:
    """Remove a trajectory's purge protection."""
    # Best-effort resolution (a stale pin may outlive its spans), and drop the
    # literal id too so pins written before canonical resolution still clear.
    resolved = tstore.resolve_attempt_id(id_) or id_
    removed = [x for x in dict.fromkeys((resolved, id_)) if tstore.unpin(x)]
    if removed:
        console.print(f"[green]✓[/green] Unpinned {' and '.join(removed)}")
    else:
        console.print(f"{id_} was not pinned.")


@trajectory_app.command("list")
def trajectory_list(
    session: str | None = typer.Option(None, "--session", help="Only attempts of this session key"),
) -> None:
    """List addressable attempts aggregated from the trace logs."""
    state = tracing_config.state_dir()
    attempts: dict[str, dict] = {}
    for span in tstore.iter_spans(state, session_key=session):
        attrs = span.get("attributes") or {}
        aid = attrs.get("attempt.id") or span.get("traceId")
        if not aid:
            continue
        entry = attempts.setdefault(aid, {"session": None, "spans": 0, "start": None, "end": None})
        entry["spans"] += 1
        if entry["session"] is None and attrs.get("session.key"):
            entry["session"] = attrs["session.key"]
        span_start, span_end = span.get("startTime"), span.get("endTime")
        if span_start and (entry["start"] is None or span_start < entry["start"]):
            entry["start"] = span_start
        if span_end and (entry["end"] is None or span_end > entry["end"]):
            entry["end"] = span_end

    if not attempts:
        scope = f"session {session!r}" if session else "the trace logs"
        console.print(f"[dim]No attempts found in {scope}.[/dim]")
        return

    latest: dict[str, str] = {}
    for v in read_verdicts(state):
        latest[v.attempt_id] = v.status

    table = Table()
    # Fold, never truncate: the id must stay copy-pastable into `save`.
    table.add_column("Attempt ID", style="cyan", overflow="fold")
    table.add_column("Session")
    table.add_column("Spans", justify="right")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Verdict")

    def _fmt(ts: str | None) -> str:
        return (ts or "")[:19].replace("T", " ") or "-"

    for aid, entry in sorted(attempts.items(), key=lambda kv: kv[1]["end"] or "", reverse=True):
        table.add_row(
            aid,
            entry["session"] or "-",
            str(entry["spans"]),
            _fmt(entry["start"]),
            _fmt(entry["end"]),
            latest.get(aid, "-"),
        )
    console.print(table)


__all__ = ["trajectory_app"]
