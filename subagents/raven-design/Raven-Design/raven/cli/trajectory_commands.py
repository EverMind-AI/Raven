"""``raven trajectory`` subapp — package, label, and protect trajectories.

Trajectories are addressed by attempt id (equal to the trace id for a
single-turn attempt; see :mod:`raven.trajectory`). Every id-taking command
resolves a turn's trace id to its canonical attempt id first, so verdicts,
pins, and bundles always land under the same name. The subcommands are thin
wrappers over the trajectory layer:

- ``save``    — pack one attempt into a self-contained bundle directory
  (:func:`raven.trajectory.bundle.collect_bundle`; auto-pins the id).
- ``report``  — re-pack, redact a copy (three layers, original untouched),
  preview residual suspects, and produce a shareable ``.tar.gz``.
- ``replay``  — feed a bundle's recorded model replies and tool results back
  through the live harness (:func:`raven.trajectory.replay.run_replay`; no
  real tool code runs, no spans are emitted).
- ``minimize`` — shrink a bundle to a redacted Trajectory Cassette fit for
  the regression suite (:func:`raven.trajectory.cassette.minimize_bundle`).
- ``verdict`` — record a task-outcome label (source fixed to ``user``).
- ``pin`` / ``unpin`` — grant / revoke the never-purge retention promise.
- ``list``    — aggregate addressable attempts from the trace logs so users
  can find the id they want to bundle.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore
from raven.trajectory.bundle import collect_bundle
from raven.trajectory.redact import redact_bundle
from raven.trajectory.report import get_uploader, pack_report
from raven.trajectory.verdict import VERDICT_STATUSES, read_verdicts, record_verdict

console = Console()

trajectory_app = typer.Typer(
    help="Package, label, and protect agent trajectories.",
    no_args_is_help=True,
)


def _bundle_dir_or_exit(target: str) -> Path:
    """The bundle directory ``target`` names — a path, or an id under the
    default bundles directory; exits when neither holds a bundle."""
    bundle_dir = Path(target)
    if (bundle_dir / "manifest.json").is_file():
        return bundle_dir
    bundle_dir = tracing_config.state_dir() / "bundles" / target
    if (bundle_dir / "manifest.json").is_file():
        return bundle_dir
    console.print(f"[red]no bundle found for {target!r}[/red] — run [cyan]raven trajectory save {target}[/cyan] first")
    raise typer.Exit(code=1)


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


@trajectory_app.command("report")
def trajectory_report(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Output tarball path (default: <trace-state>/reports/<attempt-id>.tar.gz)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the interactive confirmation (for scripts)"),
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace holding the session records (default: the configured workspace)",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        exists=True,
        help="Config file the traced agent ran with; seeds redaction on top of the default config's"
        " secrets and, unless --workspace is given, names the workspace for session lookup",
    ),
) -> None:
    """Redact a trajectory and pack it into a shareable .tar.gz (the original bundle is untouched)."""
    if workspace is None and config is not None:
        # The traced agent's session records live in that config's workspace;
        # without this the bundle silently omits session.jsonl. An unloadable
        # config falls back to the default workspace — redaction reports the
        # degradation separately (config_loaded).
        try:
            from raven.config.loader import load_config

            workspace = load_config(config).workspace_path
        except Exception:
            pass
    try:
        bundle_dir = collect_bundle(id_, workspace=workspace)
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    attempt_id = bundle_dir.name
    console.print(f"Bundled [cyan]{attempt_id}[/cyan] (re-packed to pick up the latest data)")

    staging = Path(tempfile.mkdtemp(prefix=f"raven-report-{attempt_id}-"))
    try:
        report = redact_bundle(bundle_dir, staging / attempt_id, config_path=config)

        if not report.config_loaded:
            console.print(
                "[yellow]config could not be fully read — known-value redaction may be incomplete;"
                " review the residual samples with extra care[/yellow]"
            )
        console.print(
            f"Redacted a copy: {sum(report.exact.values())} known-value"
            f" + {sum(report.patterns.values())} pattern replacement(s)"
        )
        for name, hits in sorted(report.patterns.items()):
            console.print(f"  [dim]pattern {name}: {hits}[/dim]")
        if report.skipped_binaries:
            console.print(
                f"  [yellow]{len(report.skipped_binaries)} non-UTF-8 file(s) excluded"
                f" from the copy — listed in redaction.json[/yellow]"
            )

        if report.findings:
            by_category = Counter(f.category for f in report.findings)
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(by_category.items()))
            console.print(
                f"[yellow]Residual scan flagged {len(report.findings)} suspicious token(s)[/yellow] ({summary}):"
            )
            for finding in report.findings[:5]:
                console.print(f"  [dim]{escape(f'{finding.file}: {finding.sample}')}[/dim]")
            if len(report.findings) > 5:
                console.print(f"  [dim]... and {len(report.findings) - 5} more (see redaction.json)[/dim]")
            console.print("Review the samples above — a flagged token may be a real secret the redactor missed.")
        else:
            console.print("[green]Residual scan: clean[/green]")

        if not yes and not typer.confirm("Produce the report tarball?"):
            console.print("Aborted — no tarball was produced.")
            raise typer.Exit(code=1)

        out_file = out or tracing_config.state_dir() / "reports" / f"{attempt_id}.tar.gz"
        tarball = pack_report(staging / attempt_id, out_file)
        destination = get_uploader("local").upload(tarball, metadata=report.metadata())
        console.print(f"[green]✓[/green] Report ready at [cyan]{destination}[/cyan]")
        console.print("  [dim]local backend: nothing was uploaded — hand the file over yourself[/dim]")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@trajectory_app.command("replay")
def trajectory_replay(
    target: str = typer.Argument(..., metavar="BUNDLE_OR_ID", help="Bundle directory, or an attempt/trace id"),
    strict: bool = typer.Option(
        False,
        "--strict/--warn",
        help="strict: halt at the first divergence; warn (default): report divergences and keep feeding",
    ),
) -> None:
    """Replay a saved trajectory through the live harness (mock replay: recorded
    model replies and tool results are fed back; no real tool code runs).

    Exit codes: 0 — replayed to the end; 1 — bad target; 2 — replay halted
    (strict divergence, or the recording ran out mid-replay).
    """
    from raven.trajectory.replay import run_replay

    bundle_dir = _bundle_dir_or_exit(target)
    mode = "strict" if strict else "warn"
    try:
        report = asyncio.run(run_replay(bundle_dir, mode=mode))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    streamed = f" ({report.llm_calls_streamed} streamed)" if report.llm_calls_streamed else ""
    console.print(f"Replayed [cyan]{bundle_dir.name}[/cyan] ({mode} mode)")
    console.print(
        f"  turns: {report.turns_replayed}/{report.turns_recorded}"
        f"  model calls: {report.llm_calls_replayed}/{report.llm_calls_recorded}{streamed}"
        f"  tool calls: {report.tool_calls_replayed}/{report.tool_calls_recorded}"
    )
    if report.divergences:
        console.print(f"[yellow]{len(report.divergences)} divergence(s):[/yellow]")
        for div in report.divergences:
            console.print(f"  [yellow]{escape(div.render())}[/yellow]")
    else:
        console.print("[green]No divergence — the harness reproduced the recording.[/green]")
    if report.halted:
        console.print("[red]Replay halted before the end of the recording.[/red]")
        raise typer.Exit(code=2)
    console.print("[green]✓[/green] Replay ran to the end of the recorded turns.")


def _fmt_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(value)} B"


@trajectory_app.command("minimize")
def trajectory_minimize(
    target: str = typer.Argument(..., metavar="BUNDLE_OR_ID", help="Bundle directory, or an attempt/trace id"),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Cassette directory (default: <trace-state>/cassettes/<attempt-id>)"
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        exists=True,
        help="Config file the traced agent ran with; seeds redaction on top of the default config's secrets",
    ),
) -> None:
    """Shrink a bundle into a redacted Trajectory Cassette — the committable
    form a regression case replays (only what replay consumes is kept)."""
    from raven.trajectory.cassette import minimize_bundle

    bundle_dir = _bundle_dir_or_exit(target)
    attempt_id = (
        json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8")).get("attempt_id") or bundle_dir.name
    )
    out_dir = out
    if out_dir is None:
        # The manifest's attempt id is recorded data; refuse one that would
        # name a default path outside the cassettes directory.
        out_root = (tracing_config.state_dir() / "cassettes").resolve()
        out_dir = (out_root / attempt_id).resolve()
        if out_dir.parent != out_root:
            console.print(f"[red]id {attempt_id!r} cannot be used as a cassette directory name[/red]")
            raise typer.Exit(code=1)
    try:
        report = minimize_bundle(bundle_dir, out_dir, config_path=config)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Cassette written to [cyan]{report.cassette_dir}[/cyan]")
    console.print(f"  size: {_fmt_bytes(report.original_bytes)} -> {_fmt_bytes(report.cassette_bytes)}")
    console.print(
        f"  spans: {report.span_count}/{report.source_span_count} kept"
        f"  artifacts: {report.artifact_count}"
        f"  llm calls: {report.llm_calls}  tool calls: {report.tool_calls}  turns: {report.turns}"
        f"  session: {report.session}"
    )
    redaction = report.redaction
    if not redaction.config_loaded:
        console.print(
            "  [yellow]config could not be fully read — known-value redaction may be incomplete;"
            " review the cassette with extra care before committing it[/yellow]"
        )
    console.print(
        f"  redacted: {sum(redaction.exact.values())} known-value"
        f" + {sum(redaction.patterns.values())} pattern replacement(s)"
    )
    if redaction.findings:
        console.print(
            f"  [yellow]residual scan flagged {len(redaction.findings)} suspicious token(s)"
            f" — review redaction.json before committing the cassette[/yellow]"
        )
        for finding in redaction.findings[:5]:
            console.print(f"    [dim]{escape(f'{finding.file}: {finding.sample}')}[/dim]")
    else:
        console.print("  [green]residual scan: clean[/green]")


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
