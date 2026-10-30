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
  envelope and is gated by the redaction classification: review findings are
  adjudicated per item, and shipping a review report always takes an explicit
  risk authorization (never implied by ``--yes``); decisions remembered from
  earlier reviews are replayed (:mod:`raven.trajectory.redaction_history`).
  When ``RAVEN_BUGREPORT_URL``
  names a receiver, the package is uploaded as soon as it lands
  (:mod:`raven.trajectory.upload`).
- ``redaction-history`` — ``list`` / ``forget`` / ``clear`` the keep/replace
  decisions the bug report review remembers
  (:mod:`raven.trajectory.redaction_history`).
- ``upload-bug-report`` — send (or send again) a filed report's package to the
  receiver; the same package twice yields the same receipt.
- ``receiver`` — run the Bug Report Receiver itself: the HTTP service that takes
  packages in, checks them, and stores them by day
  (:mod:`raven.trajectory.receiver`).
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
from typing import Any, Callable

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from raven.cli._tty_guard import die_if_not_tty
from raven.cli.trajectory_regression_commands import regression_app
from raven.tracing import config as tracing_config
from raven.trajectory import store as tstore
from raven.trajectory.bundle import collect_bundle
from raven.trajectory.redact import redact_bundle
from raven.trajectory.report import get_uploader, pack_report
from raven.trajectory.verdict import VERDICT_STATUSES, read_verdicts, record_verdict

console = Console()

trajectory_app = typer.Typer(help="Package, label, and protect agent trajectories.")
trajectory_app.add_typer(regression_app, name="regression")
redaction_history_app = typer.Typer(help="Inspect and edit the remembered redaction review decisions.")
trajectory_app.add_typer(redaction_history_app, name="redaction-history")


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


def _print_review_failure(
    items: list, undecided: list, reasons: list[str], missing: list[str], resolution: Any
) -> None:
    from raven.cli import trajectory_browse as tbrowse

    if undecided:
        console.print(f"[red]The redaction needs review; {len(undecided)} item(s) require your decision:[/red]")
    else:
        console.print("[red]The redaction needs review:[/red]")
    console.print(f"  {tbrowse._REVIEW_PATHS_NOTE}", highlight=False)
    for warning in tbrowse._review_warnings(reasons):
        console.print(f"  [yellow]! {escape(warning)}[/yellow]", highlight=False)
    tbrowse._print_history_decisions(resolution, items)
    index_by_id = {item.id: index for index, item in enumerate(items, 1)}
    for item in undecided:
        tbrowse._print_review_item(item, index_by_id[item.id], len(items), index_by_id)
    console.print(f"[red]Pass: {'; '.join(missing)} (--yes does not imply them).[/red]")


def _decide_without_tty(
    items: list,
    reasons: list[str],
    *,
    keep_findings: bool,
    redact_findings: bool,
    accept_risk: bool,
    history: Any,
) -> list:
    """``report-bug``'s decisions with no TTY: flags, then remembered decisions.

    ``history`` is None when replay is off. Missing flags are judged before
    any decision application and before any history write: otherwise a run
    whose items are all covered but that lacks --accept-risk would keep
    exporting and fail late without the item listing (a redact run would even
    rewrite the context before failing). Exits 1 listing every undecided item
    and every missing flag at once.
    """
    from raven.cli import trajectory_browse as tbrowse
    from raven.trajectory import review as treview

    flagged = keep_findings or redact_findings
    resolution = None
    if history is not None and not flagged:
        resolution, history_warnings = tbrowse._resolve_history(history, items)
        tbrowse._print_history_warnings(history_warnings)
    replayed = {decision.item_id: decision.action for decision in resolution.auto} if resolution else {}
    undecided = [item for item in items if item.id not in replayed]
    missing = []
    if any(item.kind == treview.KIND_SUSPECTED for item in undecided) and not flagged:
        missing.append("--keep-findings or --redact-findings")
    if not accept_risk:
        missing.append("--accept-risk")
    if missing:
        _print_review_failure(items, undecided, reasons, missing, resolution)
        raise typer.Exit(code=1)
    tbrowse._print_history_decisions(resolution, items)
    flag_action = treview.ACTION_REDACTED if redact_findings else treview.ACTION_KEPT
    decisions = []
    for item in items:
        if item.id in replayed:
            decisions.append(treview.ReviewDecision(item.id, replayed[item.id]))
        elif item.kind == treview.KIND_CONFIRMED:
            decisions.append(treview.ReviewDecision(item.id, treview.ACTION_ACKNOWLEDGED))
        else:
            decisions.append(treview.ReviewDecision(item.id, flag_action))
    if resolution is not None and replayed:
        tbrowse._remember_decisions(history, items, decisions, source="cli", replayed=resolution.replayed)
    return decisions


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
        help="Authorize a review report: acknowledges confirmed-sensitive items"
        " and grants the risk consent (--yes does not imply it)",
    ),
    keep_findings: bool = typer.Option(
        False, "--keep-findings", help="Keep every suspected finding as-is (confirms they are harmless)"
    ),
    redact_findings: bool = typer.Option(
        False, "--redact-findings", help="Replace every suspected finding with [REDACTED:user-confirmed]"
    ),
    review_all: bool = typer.Option(
        False,
        "--review-all",
        help="Ignore remembered redaction decisions for this run and decide every item again"
        " (new answers replace the remembered ones)",
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
    Review findings are adjudicated per item on a TTY (a flag pre-decides the
    items of its kind); without one, --yes is required, suspected findings
    need --keep-findings or --redact-findings, and every review report needs
    --accept-risk — a run missing flags fails listing all items and every
    missing flag at once, producing nothing.

    Decisions remembered from earlier reviews are replayed first and listed
    (with or without a TTY), so only the other items need an answer or a
    flag; --review-all ignores them for one run. The blanket keep/redact
    flags neither read nor write the history, and
    RAVEN_BUGREPORT_REDACTION_HISTORY=off turns it off.
    """
    from raven.trajectory import bugreport as breport
    from raven.trajectory import review as treview
    from raven.trajectory import upload as tupload

    description = description.strip()
    if not description:
        # Typer only enforces presence; a blank value must fail here, before
        # any collection or pin side effect.
        console.print("[red]--description must not be blank; nothing was collected or pinned.[/red]")
        raise typer.Exit(code=1)
    if severity and severity not in breport.SEVERITIES:
        console.print(f"[red]--severity must be one of: {', '.join(breport.SEVERITIES)}[/red]")
        raise typer.Exit(code=1)
    if keep_findings and redact_findings:
        console.print(
            "[red]--keep-findings and --redact-findings are mutually exclusive; nothing was collected or pinned.[/red]"
        )
        raise typer.Exit(code=1)

    interactive = _stdin_is_tty()
    # Authorization preflight, before any side effect: collection pins the
    # attempt and creates staging state, and a script that cannot confirm has
    # no screen on which that could be disclosed.
    if not interactive and not yes:
        console.print("[red]--yes is required without a TTY; nothing was collected or pinned.[/red]")
        raise typer.Exit(code=1)
    base_url = _resolved_upload_url()
    # The probe only informs: a person learns the receiver is down before
    # spending a review on the report, and a local package can still be made.
    if base_url and _receiver_problem(base_url) is not None:
        if interactive and not yes:
            if not typer.confirm(_CONTINUE_WITHOUT_RECEIVER, default=False):
                console.print("Cancelled — nothing was collected or pinned.")
                raise typer.Exit(code=1)
        else:
            console.print("  Continuing: if the upload fails, the package stays on this machine.")

    try:
        prep = breport.prepare_trajectory(id_, workspace=workspace, config_path=config)
    except breport.StaleAttemptError:
        console.print("[red]The attempt changed while the report was being prepared — nothing was created.[/red]")
        raise typer.Exit(code=1)
    except (ValueError, LookupError, breport.PreparationError) as exc:
        console.print(f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    console.print("Snapshot collected; the attempt was pinned so cleanup won't remove it.")

    def _deny_noninteractive(reasons: list[str]) -> None:
        console.print(
            "[red]The redaction needs review; pass --accept-risk to grant the separate"
            " authorization (--yes does not imply it):[/red]"
        )
        for reason in reasons:
            console.print(f"  - {escape(reason)}", highlight=False)

    from raven.trajectory import redaction_history as thistory

    history = thistory.RedactionHistory() if thistory.enabled() else None
    replay = history is not None and not review_all

    def _decide(items: list, reasons: list[str]) -> list:
        from raven.cli import trajectory_browse as tbrowse

        flagged = keep_findings or redact_findings
        if not interactive:
            return _decide_without_tty(
                items,
                reasons,
                keep_findings=keep_findings,
                redact_findings=redact_findings,
                accept_risk=accept_risk,
                history=history if replay else None,
            )
        flag_decisions: list = []
        remaining: list = []
        for item in items:
            if item.kind == treview.KIND_CONFIRMED and accept_risk:
                flag_decisions.append(treview.ReviewDecision(item.id, treview.ACTION_ACKNOWLEDGED))
            elif item.kind == treview.KIND_SUSPECTED and flagged:
                action = treview.ACTION_REDACTED if redact_findings else treview.ACTION_KEPT
                flag_decisions.append(treview.ReviewDecision(item.id, action))
            else:
                remaining.append(item)
        if not remaining:
            return flag_decisions
        # Linked groups exist only among suspected items and the flags cover
        # a kind whole, so a group is never split between flags and
        # interaction; history replays whole linked groups, so it never
        # splits one either.
        from raven.cli._styles import RAVEN_STYLE

        try:
            decide = tbrowse.make_review_decider(
                tbrowse._require_questionary(), RAVEN_STYLE, history=history, replay=replay, source="cli"
            )
            decided = decide(remaining, reasons)
        except (tbrowse._ActionCancelledError, tbrowse._CancelledError) as exc:
            # Esc/Ctrl-C during adjudication: the CLI has no outer screen
            # to unwind to, so both mean "cancel this report".
            raise treview.ReviewCancelledError("cancelled from the review screen") from exc
        return flag_decisions + decided

    exit_code = 0
    try:
        try:
            prep = breport.freeze_export(
                prep,
                description=description,
                expected=expected,
                actual=actual,
                severity=severity,
                steps=steps,
                reporter=reporter,
                decide=_decide,
            )
        except treview.ReviewCancelledError:
            console.print("Cancelled — no bug report was created.")
            exit_code = 1
            return
        except breport.PreparationError as exc:
            console.print(
                f"[red]✗ Could not prepare the trajectory snapshot: {escape(str(exc))}[/red]", highlight=False
            )
            exit_code = 1
            return

        _print_bug_cli_summary(prep, upload_target=tupload.upload_target(base_url) if base_url else "")
        if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW and not accept_risk:
            # The review authorization can only come from --accept-risk or
            # this risk-worded consent; --yes never grants it.
            if interactive:
                if prep.user_decisions:
                    console.print("Review decisions are complete. Confirm the report contents shown above.")
                if not typer.confirm("Ship the package with the risks listed above?", default=False):
                    console.print("Cancelled — no bug report was created.")
                    exit_code = 1
                    return
            else:
                _deny_noninteractive(prep.reasons)
                exit_code = 1
                return
        elif not yes and not typer.confirm("Create the bug report?", default=True):
            console.print("Cancelled — no bug report was created.")
            exit_code = 1
            return

        try:
            record_dir, record = breport.confirm_and_package(prep)
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
        if base_url:
            exit_code = _upload_and_report(record_dir, base_url)
        else:
            console.print("  Not uploaded — hand the package file to a developer yourself.")
    finally:
        # A no-op once the record landed; on every earlier exit it removes the
        # pre-confirmation staging state (mirrors the browser flow).
        prep.cleanup()
        if exit_code:
            raise typer.Exit(code=exit_code)


def _stdin_is_tty() -> bool:
    import sys

    return sys.stdin.isatty()


def _resolved_upload_url() -> str:
    """The receiver to upload to ("" when off), or exit before any side effect."""
    from raven.trajectory import upload as tupload

    try:
        return tupload.resolve_upload_url()
    except tupload.UploadConfigError as exc:
        console.print(f"[red]✗ Bug report upload is misconfigured: {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)


_CONTINUE_WITHOUT_RECEIVER = "Continue anyway? If the upload fails, the package stays on this machine."


def _receiver_problem(base_url: str) -> str | None:
    """Probe the receiver behind a spinner; print and return why it cannot be used, None when it answers."""
    from raven.trajectory import upload as tupload

    try:
        with console.status("Checking the bug report receiver...", spinner="dots"):
            tupload.probe_receiver(base_url)
    except tupload.ProbeError as exc:
        console.print(
            f"[red]✗ The bug report receiver at {escape(tupload.upload_target(base_url))} is not reachable: "
            f"{escape(exc.reason)}[/red]",
            highlight=False,
            soft_wrap=True,
        )
        return exc.reason
    return None


def _upload_and_report(record_dir: Path, base_url: str) -> int:
    """Upload a landed report's package and print the outcome; the exit code to use."""
    from raven.trajectory import upload as tupload

    try:
        with console.status("Uploading the package...", spinner="dots"):
            _record, receipt = tupload.upload_record(record_dir, base_url=base_url)
    except tupload.UploadError as exc:
        console.print(f"  [red]✗ Upload failed: {escape(str(exc))}[/red]", highlight=False, soft_wrap=True)
        console.print(
            f"  The package is kept; retry with: raven trajectory upload-bug-report {escape(record_dir.name)}",
            highlight=False,
            soft_wrap=True,
        )
        return 1
    verb = "Already uploaded" if receipt.duplicate else "Uploaded"
    console.print(f"  {verb} — receipt {escape(receipt.receipt_id)}", highlight=False)
    return 0


@trajectory_app.command("upload-bug-report")
def trajectory_upload_bug_report(
    report_id: str = typer.Argument(..., metavar="REPORT_ID", help="Bug report id (br-...)"),
    url: str | None = typer.Option(None, "--url", help="Receiver base URL (default: $RAVEN_BUGREPORT_URL)"),
) -> None:
    """Upload, or upload again, a filed Bug Report Package to the receiver.

    The package is the one the report was created with; a package that has
    been changed since is refused. Uploading the same package twice is safe:
    the receiver answers with the receipt it already holds.
    """
    from raven.trajectory import bugreport as breport
    from raven.trajectory import upload as tupload

    try:
        base_url = tupload.validate_upload_url(url) if url is not None else tupload.resolve_upload_url()
    except tupload.UploadConfigError as exc:
        console.print(f"[red]✗ Bug report upload is misconfigured: {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    if not base_url:
        console.print(f"[red]✗ No upload URL is configured; set {tupload.UPLOAD_URL_ENV} or pass --url.[/red]")
        raise typer.Exit(code=2)
    record_dir = breport.bugreports_root() / report_id
    if not breport.is_report_id(report_id) or not (record_dir / breport.RECORD_FILE).is_file():
        console.print(f"[red]✗ Unknown bug report {escape(report_id)}[/red]", highlight=False)
        raise typer.Exit(code=1)
    try:
        breport.load_record(record_dir)
    except ValueError as exc:
        console.print(f"[red]✗ Bug report {escape(report_id)} is damaged: {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    raise typer.Exit(code=_upload_and_report(record_dir, base_url))


def _receiver_address(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


@trajectory_app.command("receiver")
def trajectory_receiver(
    save_dir: Path = typer.Option(..., "--save-dir", help="Directory that receives packages (created 0700)"),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Bind address; anything but loopback belongs behind a TLS proxy"
    ),
    port: int = typer.Option(18795, "--port", min=0, max=65535, help="TCP port; 0 picks a free one and prints it"),
    trusted_proxy: list[str] = typer.Option(
        [], "--trusted-proxy", help="CIDR of a proxy whose X-Forwarded-For is believed (repeatable)"
    ),
    max_package_mib: int = typer.Option(64, "--max-package-mib", min=1, help="Largest package accepted, in MiB"),
    max_unpacked_mib: int = typer.Option(
        256, "--max-unpacked-mib", min=1, help="Largest decompressed volume inspected per package, in MiB"
    ),
    max_daily_mib: int = typer.Option(2048, "--max-daily-mib", min=0, help="Bytes accepted per UTC day, in MiB"),
    max_daily_count: int = typer.Option(500, "--max-daily-count", min=0, help="Packages accepted per UTC day"),
    min_free_mib: int = typer.Option(1024, "--min-free-mib", min=0, help="Free disk space to keep, in MiB"),
    rate_burst: int = typer.Option(
        10, "--rate-burst", min=0, help="Uploads one address may make at once; 0 disables rate limiting"
    ),
    rate_per_10min: int = typer.Option(
        30, "--rate-per-10min", min=0, help="Uploads one address regains per ten minutes"
    ),
    concurrency: int = typer.Option(4, "--concurrency", min=1, help="Uploads handled at the same time"),
    idle_timeout: float = typer.Option(
        30.0, "--idle-timeout", min=0.0, help="Seconds without body data before an upload is dropped"
    ),
    total_timeout: float = typer.Option(600.0, "--total-timeout", min=0.0, help="Seconds one upload may take"),
) -> None:
    """Run the Bug Report Receiver: accept packages into --save-dir until stopped.

    The receiver authenticates nobody; who can reach it is decided by the
    network it listens on. It binds loopback by default -- put a TLS proxy in
    front of it for anything else, and name that proxy with --trusted-proxy
    so uploads are rate-limited per real client. Ctrl-C or SIGTERM stops it.
    """
    import ipaddress

    from raven.trajectory import receiver as trecv
    from raven.trajectory.package_check import PackageLimits

    for cidr in trusted_proxy:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise typer.BadParameter(f"not a CIDR network: {cidr}", param_hint="--trusted-proxy")
    save_dir = save_dir.resolve()
    if save_dir.exists() and not save_dir.is_dir():
        console.print(f"[red]✗ --save-dir is not a directory: {escape(str(save_dir))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    config = trecv.ReceiverConfig(
        save_dir=save_dir,
        host=host,
        port=port,
        trusted_proxies=tuple(trusted_proxy),
        limits=PackageLimits(max_package_bytes=max_package_mib * 2**20, max_unpacked_bytes=max_unpacked_mib * 2**20),
        max_daily_bytes=max_daily_mib * 2**20,
        max_daily_count=max_daily_count,
        min_free_bytes=min_free_mib * 2**20,
        rate_burst=rate_burst,
        rate_per_10min=rate_per_10min,
        concurrency=concurrency,
        idle_timeout=idle_timeout,
        total_timeout=total_timeout,
    )

    def _announce(bound_host: str, bound_port: int) -> None:
        console.print(
            f"Bug report receiver listening on {escape(_receiver_address(bound_host, bound_port))}",
            highlight=False,
            soft_wrap=True,
        )
        console.print(f"Saving packages under {escape(str(save_dir))}", highlight=False, soft_wrap=True)
        console.print("Press Ctrl-C to stop.")

    try:
        trecv.run_receiver(config, ready=_announce)
    except OSError as exc:
        console.print(
            f"[red]✗ Cannot prepare the receiver (save dir {escape(str(save_dir))}): {escape(str(exc))}[/red]",
            highlight=False,
            soft_wrap=True,
        )
        raise typer.Exit(code=1)
    console.print("Bug report receiver stopped.")


def _print_bug_cli_summary(prep, *, upload_target: str = "") -> None:
    """The final confirmation block, rendered from the frozen canonical metadata.

    Everything the package will carry must be on screen at the confirmation
    point — the optional problem fields, the externally shipped reporter
    identity, the full completeness reasons, and whether the package will
    leave this machine.
    """
    from raven.trajectory import bugreport as breport

    meta = prep.package_metadata
    attempt = meta["attempt"]
    completeness = meta["completeness"]
    redaction = meta["redaction"]
    manifest = prep.manifest if isinstance(prep.manifest, dict) else {}
    exact = sum(redaction["exact_replacements"].values())
    patterns = sum(redaction["pattern_replacements"].values())
    console.print("Bug report summary", highlight=False)
    time_range = manifest.get("time_range") or {}
    last_activity = str(time_range.get("end") or "?")[:16].replace("T", " ")
    console.print(
        f"  Session:      {escape(str(prep.session_key or '(no session)'))} · last activity {escape(last_activity)}",
        highlight=False,
    )
    traces = attempt.get("member_traces") or []
    console.print(
        f"  Attempt:      {escape(str(attempt.get('attempt_id')))} · {len(traces)} member trace(s)", highlight=False
    )
    console.print(f"  Problem:      {escape(meta['problem']['description'])}", highlight=False)
    for label, key in (("Expected", "expected"), ("Actual", "actual"), ("Severity", "severity"), ("Steps", "steps")):
        if meta["problem"].get(key):
            console.print(f"  {label + ':':<14}{escape(meta['problem'][key])}", highlight=False)
    if meta.get("reporter"):
        console.print(f"  Reporter:     {escape(meta['reporter'])} (included in the package)", highlight=False)
    session_note = "included" if manifest.get("session_included") else "missing"
    console.print(
        f"  Trajectory:   {manifest.get('span_count')} span(s) · session record {session_note}"
        f" · {manifest.get('artifact_count')} artifact(s), {len(manifest.get('missing_artifacts') or [])} missing",
        highlight=False,
    )
    line = completeness["status"]
    console.print(f"  Completeness: {escape(line)}", highlight=False)
    for reason in completeness["reasons"]:
        console.print(f"    - {escape(reason)}", highlight=False)
    counts = f"{exact} known-value + {patterns} pattern replacement(s)"
    if prep.classification == breport.CLASSIFICATION_NEEDS_REVIEW:
        console.print(f"  Redaction:    {counts} · NEEDS REVIEW", highlight=False)
        for notice in redaction.get("security_notices") or []:
            console.print(f"    [yellow]! {escape(notice)}[/yellow]", highlight=False)
        for reason in prep.reasons:
            console.print(f"    - {escape(reason)}", highlight=False)
        decisions = redaction.get("user_decisions") or []
        if decisions:
            from raven.cli.trajectory_browse import _decision_line, _decision_tally

            console.print(f"  Reviewed:     {_decision_tally(decisions)}", highlight=False)
            for entry in decisions:
                console.print(f"    - {escape(_decision_line(entry))}", highlight=False)
    else:
        console.print(f"  Redaction:    {counts} · residual scan: clean", highlight=False)
    console.print(
        "  Note: this check looks for credentials only — names, business data, and\n"
        "  customer content are NOT anonymized.",
        highlight=False,
    )
    console.print(f"  Will produce: bug report package {escape(prep.report_id)}.tar.gz (kept locally)")
    if upload_target:
        console.print(f"  Will upload to {escape(upload_target)} after the package is created.", highlight=False)
    else:
        console.print("  Nothing will be uploaded — the package stays on this machine.")


def _manifest_summary(bundle_dir: Path) -> dict[str, Any] | None:
    """Scalar attempt_id/format_version from the bundle manifest, best-effort."""
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    summary = {
        key: manifest[key] for key in ("attempt_id", "format_version") if isinstance(manifest.get(key), (str, int))
    }
    return summary or None


@trajectory_app.command("replay")
def trajectory_replay(
    target: str = typer.Argument(..., metavar="BUNDLE_OR_ID", help="Bundle directory, or an attempt/trace id"),
    strict: bool = typer.Option(
        False,
        "--strict/--warn",
        help="strict: halt at the first divergence; warn (default): report divergences and keep feeding",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the report as JSON (schema_version 1) instead of the human summary"
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the JSON report to this file (requires --json)"),
) -> None:
    """Replay a saved trajectory through the live harness (mock replay: recorded
    model replies and tool results are fed back; no real tool code runs).

    Exit codes: 0 — replayed to the end; 1 — bad target or bad usage; 2 —
    replay halted (strict divergence, or the recording ran out mid-replay).
    """
    from raven.trajectory.replay import run_replay

    # Validated by hand: click's own usage errors exit with code 2, which this
    # command reserves for "replay halted".
    if out is not None and not json_output:
        console.print("[red]--out requires --json[/red]")
        raise typer.Exit(code=1)
    bundle_dir = _bundle_dir_or_exit(target)
    mode = "strict" if strict else "warn"
    try:
        report = asyncio.run(run_replay(bundle_dir, mode=mode))
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        text = json.dumps(report.to_dict(manifest=_manifest_summary(bundle_dir)), indent=2)
        if out is not None:
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text + "\n", encoding="utf-8")
            except OSError as exc:
                console.print(f"[red]cannot write {escape(str(out))}: {escape(str(exc))}[/red]")
                raise typer.Exit(code=1)
        else:
            print(text)
        if report.halted:
            raise typer.Exit(code=2)
        return

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


@redaction_history_app.command("list")
def redaction_history_list() -> None:
    """List the remembered redaction decisions, newest first."""
    from raven.trajectory import redaction_history as thistory
    from raven.trajectory import review as treview

    history = thistory.RedactionHistory()
    try:
        snapshot = history.read()
    except thistory.HistoryStorageError as exc:
        console.print(f"[red]✗ {escape(str(exc))}[/red]", highlight=False)
        raise typer.Exit(code=1)
    if snapshot.corrupt:
        console.print(
            f"[red]✗ The redaction history at {escape(str(history.path))} could not be read"
            f" ({escape(snapshot.corrupt)}); it is backed up on the next save.[/red]",
            highlight=False,
        )
        raise typer.Exit(code=1)
    if not thistory.enabled():
        console.print(
            "[yellow]! RAVEN_BUGREPORT_REDACTION_HISTORY is off: bug reports neither replay nor record"
            " these decisions.[/yellow]",
            highlight=False,
        )
    if not snapshot.entries:
        console.print("No remembered redaction decisions.", highlight=False)
        return
    table = Table()
    table.add_column("ID", style="cyan", overflow="fold")
    table.add_column("Token")
    table.add_column("Category")
    table.add_column("Action")
    table.add_column("Decided")
    table.add_column("Reused", justify="right")
    for entry in snapshot.entries:
        table.add_row(
            escape(entry.id),
            escape(entry.masked),
            escape(entry.category),
            "kept" if entry.action == treview.ACTION_KEPT else "replaced",
            escape(entry.decided_at[:10]),
            str(entry.applied),
        )
    console.print(table)
    console.print(f"{len(snapshot.entries)} remembered decision(s) in {escape(str(history.path))}.", highlight=False)


@redaction_history_app.command("forget")
def redaction_history_forget(
    ids: list[str] = typer.Argument(..., metavar="ID...", help="Decision id from 'list' (12 to 64 hex digits)"),
) -> None:
    """Forget remembered decisions by id; nothing is removed unless every id matches exactly one."""
    from raven.trajectory import redaction_history as thistory

    try:
        result = thistory.RedactionHistory().forget(ids)
    except thistory.HistoryStorageError as exc:
        console.print(f"[red]✗ {escape(str(exc))}; nothing was forgotten.[/red]", highlight=False)
        raise typer.Exit(code=1)
    if result.unknown or result.ambiguous:
        for item_id in result.unknown:
            console.print(f"[red]✗ No remembered decision matches {escape(item_id)}.[/red]", highlight=False)
        for item_id in result.ambiguous:
            console.print(
                f"[red]✗ {escape(item_id)} matches more than one decision; give more digits.[/red]", highlight=False
            )
        console.print("[red]Nothing was forgotten.[/red]", highlight=False)
        raise typer.Exit(code=1)
    for item_id in result.removed:
        console.print(f"Forgot {escape(item_id)}.", highlight=False)
    if result.backup is not None:
        console.print(f"Backed up the unreadable redaction history to {escape(str(result.backup))}.", highlight=False)


@redaction_history_app.command("clear")
def redaction_history_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation (required without a TTY)"),
) -> None:
    """Forget every remembered redaction decision."""
    from raven.trajectory import redaction_history as thistory

    if not yes:
        if not _stdin_is_tty():
            console.print("[red]✗ --yes is required without a TTY; nothing was cleared.[/red]", highlight=False)
            raise typer.Exit(code=1)
        if not typer.confirm("Forget every remembered redaction decision?", default=False):
            console.print("Cancelled — nothing was cleared.", highlight=False)
            raise typer.Exit(code=1)
    try:
        result = thistory.RedactionHistory().clear()
    except thistory.HistoryStorageError as exc:
        console.print(f"[red]✗ {escape(str(exc))}; nothing was cleared.[/red]", highlight=False)
        raise typer.Exit(code=1)
    console.print(f"Removed {result.removed} remembered decision(s).", highlight=False)
    if result.backup is not None:
        console.print(f"Backed up the unreadable redaction history to {escape(str(result.backup))}.", highlight=False)


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


@trajectory_app.command("backfill-frames")
def trajectory_backfill_frames(
    session: str | None = typer.Option(None, "--session", help="Only this session key"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count what would be written, without writing"),
) -> None:
    """Write an external run's frame journal back as spans.

    Runs recorded before their steps were spans left only the journal pointer,
    so the timeline shows one summary row. This turns each run's stretch into
    the ``llm.call`` / ``tool.call`` rows it should have written -- the prompt,
    what it said and thought, and its tool calls. Idempotent: a second pass
    finds the spans and writes none, except to close a step an earlier pass
    left running past its run's end.
    """
    from raven.trajectory import frames_backfill

    result = frames_backfill.backfill(session_key=session, dry_run=dry_run)
    verb = "Would write" if dry_run else "Wrote"
    message = (
        f"{verb} {result['steps']} step span(s) for {result['written']} of {result['runs']} "
        f"external run(s); {result['already']} already on record"
    )
    if result.get("unread"):
        message += f"; {result['unread']} with nothing left to read in the journal"
    message += "."
    if result.get("repaired"):
        closing = "Would close" if dry_run else "Closed"
        message += f" {closing} {result['repaired']} step(s) that ran past their run's end."
    console.print(message, highlight=False)


__all__ = ["trajectory_app"]
