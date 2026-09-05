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
- ``report-bug`` — the scriptable face of the browser's "Report a bug": file
  a Bug Report (record + shippable package with the problem metadata) for an
  explicit attempt and description (:mod:`raven.trajectory.bugreport`). Unlike
  ``report``, its deliverable embeds the trajectory inside a problem-metadata
  envelope and is gated by the redaction classification; ``blocked`` cannot be
  bypassed by any flag.
- ``replay``  — feed a bundle's recorded model replies and tool results back
  through the live harness (:func:`raven.trajectory.replay.run_replay`; no
  real tool code runs, no spans are emitted).
- ``minimize`` — shrink a bundle to a redacted Trajectory Cassette fit for
  the regression suite (:func:`raven.trajectory.cassette.minimize_bundle`).
- ``verdict`` — record a task-outcome label (source fixed to ``user``).
- ``pin`` / ``unpin`` — grant / revoke the never-purge retention promise
  (``unpin`` also clears absorbed aliases and member-level pins).
- ``merge``   — combine attempts into one definition in ``attempts.json``
  (:func:`raven.trajectory.store.merge_attempts`; pins migrate up).
- ``split``   — delete a merged attempt's definition; pins migrate down to
  the member traces (:func:`raven.trajectory.store.split_attempt`).
- ``list``    — aggregate addressable attempts from the trace logs so users
  can find the id they want to bundle; definition members fold into one row.

Ids echoed in output may come from user input, spans, or bundle manifests —
none are trusted: every dynamic value rendered through Rich is escaped first
(an id like ``x[/red]y`` is legal and would otherwise crash markup parsing).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from raven.cli._tty_guard import die_if_not_tty
from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore
from raven.trajectory.bundle import collect_bundle
from raven.trajectory.redact import redact_bundle
from raven.trajectory.report import get_uploader, pack_report
from raven.trajectory.verdict import VERDICT_STATUSES, read_verdicts, record_verdict

console = Console()

trajectory_app = typer.Typer(help="Package, label, and protect agent trajectories.")


@trajectory_app.callback(invoke_without_command=True)
def trajectory_main(
    ctx: typer.Context,
    workspace: Path | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help="Workspace holding the session records (default: the configured workspace)",
    ),
) -> None:
    """Package, label, and protect agent trajectories (bare invocation opens
    the interactive browser)."""
    # Subcommands must pass through before the TTY gate, or non-interactive
    # scripts running `trajectory save/list/...` would die on the check.
    if ctx.invoked_subcommand is not None:
        return
    die_if_not_tty("raven trajectory list")
    # Local import: the browser (and its questionary dependency) loads only
    # when actually entered, and the module cycle commands <-> browse breaks.
    from raven.cli.trajectory_browse import browse_trajectories

    browse_trajectories(workspace=workspace)


def _default_cassette_dir(attempt_id: str) -> Path:
    """Default cassette directory for ``attempt_id`` under the cassettes root.

    The id is recorded data; one that would name a path outside the cassettes
    directory raises ``ValueError`` (shared by the CLI and the browser so the
    escape check cannot drift)."""
    out_root = (tracing_config.state_dir() / "cassettes").resolve()
    out_dir = (out_root / attempt_id).resolve()
    if out_dir.parent != out_root:
        raise ValueError(f"id {attempt_id!r} cannot be used as a cassette directory name")
    return out_dir


def _bundle_dir_or_exit(target: str) -> Path:
    """The bundle directory ``target`` names — a path, or an id under the
    default bundles directory; exits when neither holds a bundle."""
    bundle_dir = Path(target)
    if (bundle_dir / "manifest.json").is_file():
        return bundle_dir
    bundle_dir = tracing_config.state_dir() / "bundles" / target
    if (bundle_dir / "manifest.json").is_file():
        return bundle_dir
    console.print(
        f"[red]no bundle found for {escape(repr(target))}[/red]"
        f" — run [cyan]raven trajectory save {escape(target)}[/cyan] first"
    )
    raise typer.Exit(code=1)


def _resolve_or_exit(id_: str) -> str:
    """Map ``id_`` to its canonical attempt id, exiting when no span matches.

    Keeps verdict/pin/unpin on the same address ``save`` uses — a label or
    pin written under a turn's trace id would never be found again by the
    attempt-keyed readers.
    """
    resolved = tstore.resolve_attempt_id(id_)
    if resolved is None:
        console.print(f"[red]no spans found for id {escape(repr(id_))}[/red]")
        raise typer.Exit(code=1)
    if resolved != id_:
        console.print(f"[dim]{escape(id_)} is one turn of attempt {escape(resolved)}[/dim]")
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
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    console.print(f"[green]✓[/green] Bundled to [cyan]{escape(str(bundle_dir))}[/cyan]")
    if manifest.get("attempt_id") != id_:
        console.print(
            f"  [dim]{escape(id_)} is one turn of attempt {escape(str(manifest.get('attempt_id')))};"
            f" bundled the whole attempt[/dim]"
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
    try:
        _report_attempt(id_, out=out, yes=yes, workspace=workspace, config=config, confirm=typer.confirm)
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)


def _cli_bundled_note(attempt_id: str) -> None:
    console.print(f"Bundled [cyan]{escape(attempt_id)}[/cyan] (re-packed to pick up the latest data)")


def _report_attempt(
    id_: str,
    *,
    out: Path | None,
    yes: bool,
    workspace: Path | None,
    config: Path | None,
    confirm: Callable[[str], bool],
    on_bundled: Callable[[str], None] = _cli_bundled_note,
) -> None:
    """The report workflow with injectable presentation seams.

    ``confirm`` carries only yes/no (a cancelled prompt must be raised by the
    injector, not folded into False); ``on_bundled`` renders the re-pack
    progress note (the browser injects an id-free variant). Collection errors
    (``LookupError`` / ``ValueError``) propagate to the caller — each frontend
    presents them within its own output boundary.
    """
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
    bundle_dir = collect_bundle(id_, workspace=workspace)
    attempt_id = bundle_dir.name
    on_bundled(attempt_id)

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

        if not yes and not confirm("Produce the report tarball?"):
            console.print("Aborted — no tarball was produced.")
            raise typer.Exit(code=1)

        out_file = out or tracing_config.state_dir() / "reports" / f"{attempt_id}.tar.gz"
        tarball = pack_report(staging / attempt_id, out_file)
        destination = get_uploader("local").upload(tarball, metadata=report.metadata())
        console.print(f"[green]✓[/green] Report ready at [cyan]{escape(str(destination))}[/cyan]")
        console.print("  [dim]local backend: nothing was uploaded — hand the file over yourself[/dim]")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@trajectory_app.command("report-bug")
def trajectory_report_bug(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    description: str = typer.Option(..., "--description", "-d", help="One-line problem description (required)"),
    expected: str = typer.Option("", "--expected", help="Expected result"),
    actual: str = typer.Option("", "--actual", help="Actual result"),
    severity: str = typer.Option("", "--severity", help="low | medium | high | critical"),
    steps: str = typer.Option("", "--steps", help="Steps to reproduce"),
    reporter: str = typer.Option("", "--reporter", help="Your name or handle (included in the package)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the creation confirmation (for scripts)"),
    accept_risk: bool = typer.Option(
        False,
        "--accept-risk",
        help="Grant the separate needs_review authorization (--yes does not imply it)",
    ),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace holding the session records (default: the configured workspace)"
    ),
    config: Path | None = typer.Option(
        None, "--config", exists=True, help="Config file the traced agent ran with (seeds redaction + environment)"
    ),
) -> None:
    """File a Bug Report for an attempt: a local record plus a shippable package.

    The package embeds a redacted, path-sanitized Trajectory Report inside a
    problem-metadata envelope (completeness, environment, redaction summary).
    Without a TTY, --yes is required and needs_review additionally requires
    --accept-risk; blocked reports cannot be produced by any flag.
    """
    import sys

    from raven.trajectory import bugreport as breport

    if severity and severity not in breport.SEVERITIES:
        console.print(f"[red]--severity must be one of: {', '.join(breport.SEVERITIES)}[/red]")
        raise typer.Exit(code=1)

    interactive = sys.stdin.isatty()
    # Authorization preflight, before any side effect: collection pins the
    # attempt and creates staging state, and a script that cannot confirm has
    # no screen on which that could be disclosed.
    if not interactive and not yes:
        console.print("[red]--yes is required without a TTY; nothing was collected or pinned.[/red]")
        raise typer.Exit(code=1)

    try:
        prep = breport.prepare_trajectory(id_, workspace=workspace, config_path=config)
    except breport.StaleAttemptError:
        console.print("[red]The attempt changed while the report was being prepared — nothing was created.[/red]")
        raise typer.Exit(code=1)
    except (ValueError, LookupError, breport.PreparationError) as exc:
        console.print(f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    console.print("Snapshot collected; the attempt was pinned so cleanup won't remove it.")

    exit_code = 0
    try:
        if prep.classification == breport.CLASSIFICATION_BLOCKED:
            _print_blocked_cli()
            exit_code = 1
            return
        try:
            prep = breport.freeze_export(
                prep,
                description=description,
                expected=expected,
                actual=actual,
                severity=severity,
                steps=steps,
                reporter=reporter,
            )
        except breport.PreparationError as exc:
            console.print(
                f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False
            )
            exit_code = 1
            return
        if prep.classification == breport.CLASSIFICATION_BLOCKED:
            _print_blocked_cli()
            exit_code = 1
            return

        _print_bug_cli_summary(prep)
        if not yes and not typer.confirm("Create the bug report?", default=True):
            console.print("Cancelled — no bug report was created.")
            exit_code = 1
            return
        if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW and not accept_risk:
            if interactive:
                if not typer.confirm(
                    "The redaction needs review: flagged content may include real secrets. Ship the package anyway?",
                    default=False,
                ):
                    console.print("Cancelled — no bug report was created.")
                    exit_code = 1
                    return
            else:
                console.print(
                    "[red]The redaction needs review; pass --accept-risk to grant the separate"
                    " authorization (--yes does not imply it):[/red]"
                )
                for reason in prep.reasons:
                    console.print(f"  - {escape(reason)}", highlight=False)
                exit_code = 1
                return

        try:
            _record_dir, record = breport.confirm_and_package(prep)
        except breport.StaleAttemptError:
            console.print("[red]The attempt changed while the report was being prepared — nothing was created.[/red]")
            exit_code = 1
            return
        except breport.PreparationError as exc:
            console.print(
                f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False
            )
            exit_code = 1
            return
        except breport.PackagingError as exc:
            console.print(f"[red]✗ Bug report {escape(prep.report_id)} failed: {escape(str(exc))}[/red]")
            if exc.retryable:
                console.print('  The collected snapshot is kept. Retry from "Bug reports" in raven trajectory.')
            exit_code = 1
            return
        console.print(f"[green]✓[/green] Bug report {escape(record['report_id'])} ready (local_ready)", highlight=False)
        console.print(f"  Package: [cyan]{escape(record['package']['path'])}[/cyan]", highlight=False)
        console.print("  Not uploaded — hand the package file to a developer yourself.")
    finally:
        # A no-op once the record landed; on every earlier exit it removes the
        # pre-confirmation staging state (mirrors the browser flow).
        prep.cleanup()
        if exit_code:
            raise typer.Exit(code=exit_code)


def _print_blocked_cli() -> None:
    console.print("[red]✗ Cannot create a bug report from this material.[/red]")
    console.print(
        "  It contains a private key block; even though the copy was redacted, it is\n"
        "  not allowed to leave the machine as a bug report package.",
        highlight=False,
    )


def _print_bug_cli_summary(prep) -> None:
    from raven.trajectory import bugreport as breport

    meta = prep.package_metadata
    completeness = meta["completeness"]
    redaction = meta["redaction"]
    exact = sum(redaction["exact_replacements"].values())
    patterns = sum(redaction["pattern_replacements"].values())
    console.print(f"  Problem:      {escape(prep.problem['description'])}", highlight=False)
    line = completeness["status"]
    if completeness["reasons"]:
        line += f" ({len(completeness['reasons'])} reason(s))"
    console.print(f"  Completeness: {escape(line)}", highlight=False)
    counts = f"{exact} known-value + {patterns} pattern replacement(s)"
    if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW:
        console.print(f"  Redaction:    {counts} · NEEDS REVIEW", highlight=False)
        for reason in prep.reasons:
            console.print(f"    - {escape(reason)}", highlight=False)
    else:
        console.print(f"  Redaction:    {counts} · residual scan: clean", highlight=False)
    console.print(
        "  Note: this check looks for credentials only — names, business data, and\n"
        "  customer content are NOT anonymized.",
        highlight=False,
    )
    console.print(f"  Will produce: bug report package {escape(prep.report_id)}.tar.gz (kept locally)")
    console.print("  Nothing will be uploaded — the package stays on this machine.")


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
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)

    streamed = f" ({report.llm_calls_streamed} streamed)" if report.llm_calls_streamed else ""
    console.print(f"Replayed [cyan]{escape(bundle_dir.name)}[/cyan] ({mode} mode)")
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
    # The manifest is recorded data: broken JSON, a non-object top level, or a
    # non-string attempt_id must fail controlled (or fall back to the
    # directory name), never leak a parse/path-join traceback.
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = None
    if not isinstance(manifest, dict):
        console.print(f"[red]{escape(str(bundle_dir / 'manifest.json'))} is not a valid bundle manifest[/red]")
        raise typer.Exit(code=1)
    manifest_id = manifest.get("attempt_id")
    attempt_id = manifest_id if isinstance(manifest_id, str) and manifest_id else bundle_dir.name
    out_dir = out
    if out_dir is None:
        try:
            out_dir = _default_cassette_dir(attempt_id)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1)
    try:
        report = minimize_bundle(bundle_dir, out_dir, config_path=config)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Cassette written to [cyan]{escape(str(report.cassette_dir))}[/cyan]")
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
    console.print(f"[green]✓[/green] Recorded verdict [cyan]{status}[/cyan] for {escape(attempt_id)}")


@trajectory_app.command("pin")
def trajectory_pin(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
    reason: str = typer.Option("", "--reason", "-r", help="Why this trajectory is kept"),
) -> None:
    """Protect a trajectory from any future purge."""
    # pin_attempt resolves and pins under the attempts lock, so a concurrent
    # merge/split cannot leave this pin on an id that no longer owns anything.
    try:
        attempt_id = tstore.pin_attempt(id_, reason=reason)
    except LookupError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)
    if attempt_id != id_:
        console.print(f"[dim]{escape(id_)} is one turn of attempt {escape(attempt_id)}[/dim]")
    console.print(f"[green]✓[/green] Pinned {escape(attempt_id)}")


@trajectory_app.command("unpin")
def trajectory_unpin(
    id_: str = typer.Argument(..., metavar="ID", help="Attempt id or trace id"),
) -> None:
    """Remove a trajectory's purge protection (clears the definition id,
    absorbed aliases, and all member-level pins)."""
    if tstore.unpin_attempt(id_):
        console.print(f"[green]✓[/green] Unpinned {escape(id_)}")
    else:
        console.print(f"{escape(id_)} was not pinned.")


@trajectory_app.command("merge")
def trajectory_merge(
    ids: list[str] = typer.Argument(
        ...,
        metavar="IDS...",
        help="Two or more attempt/trace ids to combine (any mix of definition, alias, legacy, or trace ids)",
    ),
) -> None:
    """Merge attempts into one definition; pins migrate up, verdicts and pins
    recorded under the absorbed ids stay visible through it.

    Attempts made of adjacent turns replay best; gaps between merged turns
    surface as replay divergences.
    """
    try:
        new_id = tstore.merge_attempts(ids)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] Merged into {escape(new_id)}")


@trajectory_app.command("split")
def trajectory_split(
    id_: str = typer.Argument(..., metavar="ID", help="Definition id, alias, or member trace id"),
) -> None:
    """Split a merged attempt back into its member traces.

    Deletes the definition: pins migrate down to the members, merged-attempt
    verdicts do not transfer, and members carrying a legacy span-level
    grouping revert to that original legacy attempt.
    """
    members = tstore.split_attempt(id_)
    if members is None:
        console.print(
            f"[red]no attempt definition owns {escape(repr(id_))}[/red] — only merged attempts can be split"
            " (a legacy attempt's grouping is recorded in its spans and cannot be removed)"
        )
        raise typer.Exit(code=1)
    console.print(
        f"[green]✓[/green] Split the merged attempt addressed by {escape(id_)}; restored {len(members)} member trace(s)"
    )
    for member in members:
        console.print(f"  [dim]{escape(member)}[/dim]")


@trajectory_app.command("list")
def trajectory_list(
    session: str | None = typer.Option(None, "--session", help="Only attempts of this session key"),
) -> None:
    """List addressable attempts aggregated from the trace logs (traces merged
    into one definition fold into a single row under the definition id)."""
    state = tracing_config.state_dir()
    # One definitions read up front (owning_attempt would re-read the file per
    # span). Definition ownership must win over a member's legacy attempt.id,
    # or merged legacy members would surface as a second row.
    defs = tstore.definitions(state)
    owner_by_trace = {t: def_id for def_id, entry in defs.items() for t in entry["traces"]}

    def _str(value) -> str | None:
        return value if isinstance(value, str) and value else None

    attempts: dict[str, dict] = {}
    for span in tstore.iter_spans(state, session_key=session):
        # Span attributes are unvalidated history: a JSON-legal record with a
        # non-object attributes value or a non-string id/key/timestamp must
        # degrade per-field, never kill list.
        raw_attrs = span.get("attributes")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        trace_id = _str(span.get("traceId"))
        aid = owner_by_trace.get(trace_id) or _str(attrs.get("attempt.id")) or trace_id
        if not aid:
            continue
        entry = attempts.setdefault(aid, {"session": None, "spans": 0, "start": None, "end": None})
        entry["spans"] += 1
        if entry["session"] is None and _str(attrs.get("session.key")):
            entry["session"] = attrs["session.key"]
        span_start, span_end = _str(span.get("startTime")), _str(span.get("endTime"))
        if span_start and (entry["start"] is None or span_start < entry["start"]):
            entry["start"] = span_start
        if span_end and (entry["end"] is None or span_end > entry["end"]):
            entry["end"] = span_end

    if not attempts:
        scope = f"session {escape(repr(session))}" if session else "the trace logs"
        console.print(f"[dim]No attempts found in {scope}.[/dim]")
        return

    # verdicts.jsonl is append-only, so file order is time order: the highest
    # index per id is that id's latest word.
    latest: dict[str, tuple[int, str]] = {}
    for idx, v in enumerate(read_verdicts(state)):
        latest[v.attempt_id] = (idx, v.status)

    def _latest_status(aid: str) -> str:
        entry = defs.get(aid)
        ids = (aid, *(entry.get("aliases") or []), *entry["traces"]) if entry else (aid,)
        hits = [latest[x] for x in ids if x in latest]
        return max(hits)[1] if hits else "-"

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
            escape(str(aid)),
            escape(str(entry["session"] or "-")),
            str(entry["spans"]),
            _fmt(entry["start"]),
            _fmt(entry["end"]),
            escape(str(_latest_status(aid))),
        )
    console.print(table)


__all__ = ["trajectory_app"]
