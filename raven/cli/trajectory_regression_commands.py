"""``raven trajectory regression`` subapp — scaffold and gate regression cases.

Regression cases live under ``tests/trajectories/<case>/``: an ``expect.yaml``
(the assertion DSL), a ``case.yaml`` (the human contract), and a ``cassette/``
(a minimized, redacted bundle). ``init`` scaffolds that directory from a
recorded trajectory — bundle directory, attempt id, trajectory report tarball,
or bug report package — minimizing into a staging area, gating residual
findings on an explicit interactive review (the reviewed entries land in the
case.yaml draft), and publishing atomically only after every gate passed.
``validate`` is the static commit gate the CI trajectory job runs
(:func:`raven.trajectory.regression.validate_case`): schemas, cassette
completeness down to the replay contract, residual-scan coverage and review,
and the size budget. It never replays — the pytest suite does that.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from raven.trajectory.regression import (
    CASE_FILE,
    CASSETTE_DIR,
    EXPECT_TEMPLATE,
    EXPECTATION_FILE,
    ReviewedResidual,
    case_name_problem,
    discover_case_dirs,
    extract_bundle_source,
    render_case_template,
    validate_case,
)

console = Console()

regression_app = typer.Typer(help="Scaffold and gate trajectory regression cases.")

DEFAULT_CASES_ROOT = Path("tests/trajectories")


@regression_app.command("validate")
def regression_validate(
    case_dir: Path | None = typer.Argument(None, metavar="[CASE_DIR]", help="One case directory to validate"),
    all_cases: bool = typer.Option(False, "--all", help="Validate every case directory under --root"),
    root: Path = typer.Option(DEFAULT_CASES_ROOT, "--root", help="Cases root scanned by --all"),
) -> None:
    """Statically validate regression case directories: schemas, cassette
    completeness, residual scan, size budget. Exit codes: 0 — every case is
    fit to commit; 1 — any problem, a missing/empty cases root, or bad usage.
    """
    # Validated by hand so usage errors exit 1 (click's own usage errors exit
    # with code 2, which other trajectory commands reserve for real failures).
    if all_cases == (case_dir is not None):
        console.print("[red]pass exactly one of CASE_DIR or --all[/red]")
        raise typer.Exit(code=1)
    if all_cases:
        try:
            case_dirs = discover_case_dirs(root)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1)
        if not case_dirs:
            console.print(f"[red]no case directories under {escape(str(root))}[/red]")
            raise typer.Exit(code=1)
    else:
        case_dirs = [case_dir]

    total_problems = 0
    for directory in case_dirs:
        problems = validate_case(directory)
        if problems:
            total_problems += len(problems)
            console.print(f"[red]✗ {escape(directory.name)}[/red]")
            for problem in problems:
                console.print(f"  [red]{escape(problem)}[/red]", highlight=False)
        else:
            console.print(f"[green]✓[/green] {escape(directory.name)}")
    console.print(f"{len(case_dirs)} case(s), {total_problems} problem(s)")
    if total_problems:
        raise typer.Exit(code=1)


def _resolve_source(source: str, work_dir: Path) -> Path:
    """The bundle directory ``source`` names: a bundle path, a report tarball
    (extracted under ``work_dir``), or an attempt id under the state dir."""
    path = Path(source)
    if path.is_dir() and (path / "manifest.json").is_file():
        return path
    if path.is_file() and source.endswith(".tar.gz"):
        return extract_bundle_source(path, work_dir)
    # Deferred import: trajectory_commands imports this module at top level.
    from raven.cli.trajectory_commands import _bundle_dir_or_exit

    return _bundle_dir_or_exit(source)


def _attempt_id(bundle_dir: Path) -> str:
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        manifest = None
    if isinstance(manifest, dict) and isinstance(manifest.get("attempt_id"), str) and manifest["attempt_id"]:
        return manifest["attempt_id"]
    return bundle_dir.name


def _print_findings(findings) -> None:
    for finding in findings:
        console.print(f"  [yellow]{escape(finding.category)}[/yellow] in {escape(finding.file)}", highlight=False)
        console.print(f"    {escape(finding.sample)}", highlight=False)


def _review_findings(findings) -> list[ReviewedResidual] | None:
    """Interactive review of every residual finding; None = cancelled.

    Shows the full token and its source line (locally only — files receive
    the digest and the reviewer's reason, never the token), and takes a
    non-blank reason per finding. Declining a finding, an empty reason, or an
    aborted prompt cancels the whole init."""
    console.print(
        f"[yellow]{len(findings)} residual finding(s) must be reviewed before this case can be committed.[/yellow]"
    )
    reviewed: list[ReviewedResidual] = []
    for pos, finding in enumerate(findings, 1):
        occurrence = finding.occurrences[0]
        console.print(
            f"[{pos}/{len(findings)}] [yellow]{escape(finding.category)}[/yellow]"
            f" in {escape(finding.file)} line {occurrence['line_no']}",
            highlight=False,
        )
        console.print(f"  line:  {escape(occurrence['line'].strip()[:200])}", highlight=False)
        console.print(f"  token: {escape(finding.token)}", highlight=False)
        try:
            if not typer.confirm("Have you inspected this token and found it benign?", default=False):
                console.print("[red]finding declined — init cancelled, nothing was published[/red]")
                return None
            note = typer.prompt("Why is it benign?", default="", show_default=False).strip()
        except typer.Abort:
            console.print("[red]review aborted — init cancelled, nothing was published[/red]")
            return None
        if not note:
            console.print("[red]an empty reason cancels the init — nothing was published[/red]")
            return None
        digest = hashlib.sha256(finding.token.encode("utf-8")).hexdigest()
        reviewed.append(ReviewedResidual(sha256=digest, note=note))
    return reviewed


@regression_app.command("init")
def regression_init(
    source: str = typer.Argument(
        ..., metavar="SOURCE", help="Bundle directory, attempt id, or report tarball (.tar.gz)"
    ),
    name: str = typer.Option(..., "--name", help="Case name (lower snake_case, no version/ticket segments)"),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive; passes only a residual-free cassette"),
    root: Path = typer.Option(DEFAULT_CASES_ROOT, "--root", help="Cases root to publish into"),
    config: Path | None = typer.Option(
        None,
        "--config",
        exists=True,
        help="Config file the traced agent ran with; seeds redaction on top of the default config's secrets",
    ),
) -> None:
    """Scaffold a regression case from a recorded trajectory: minimize into a
    staging area, review residual findings, publish cassette plus expect.yaml
    and case.yaml drafts to the cases root.

    Exit codes: 0 — scaffold published (a draft: validate fails until its
    TODOs are filled); 1 — anything else, with nothing published.
    """
    from raven.trajectory.cassette import minimize_bundle
    from raven.trajectory.redact import scan_residuals

    problem = case_name_problem(name)
    if problem is not None:
        console.print(f"[red]{escape(problem)}[/red]")
        raise typer.Exit(code=1)
    dest = root / name
    if dest.exists():
        console.print(f"[red]{escape(str(dest))} already exists; choose another name[/red]")
        raise typer.Exit(code=1)

    work = Path(tempfile.mkdtemp(prefix="raven-regression-init-"))
    publish_tmp: Path | None = None
    try:
        bundle_dir = _resolve_source(source, work)
        staging = work / "case"
        staging.mkdir()
        try:
            minimize_bundle(bundle_dir, staging / CASSETTE_DIR, config_path=config)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1)

        findings = scan_residuals(staging / CASSETTE_DIR)
        reviewed: list[ReviewedResidual] = []
        if findings:
            if yes:
                console.print(
                    f"[red]{len(findings)} residual finding(s); --yes publishes only a residual-free"
                    " cassette — rerun interactively to review them[/red]"
                )
                _print_findings(findings)
                raise typer.Exit(code=1)
            maybe_reviewed = _review_findings(findings)
            if maybe_reviewed is None:
                raise typer.Exit(code=1)
            reviewed = maybe_reviewed

        (staging / EXPECTATION_FILE).write_text(EXPECT_TEMPLATE, encoding="utf-8")
        (staging / CASE_FILE).write_text(render_case_template(_attempt_id(bundle_dir), reviewed), encoding="utf-8")

        # Publish protocol: copy onto the destination filesystem first (a
        # cross-device move would degrade to copy+delete and could leave a
        # half-written case), re-check the name, then rename on one device.
        root.mkdir(parents=True, exist_ok=True)
        publish_tmp = Path(tempfile.mkdtemp(prefix=f".init-{name}-", dir=root))
        shutil.copytree(staging, publish_tmp, dirs_exist_ok=True)
        if dest.exists():
            console.print(f"[red]{escape(str(dest))} was created while init ran; nothing was published[/red]")
            raise typer.Exit(code=1)
        publish_tmp.rename(dest)
        publish_tmp = None
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if publish_tmp is not None:
            shutil.rmtree(publish_tmp, ignore_errors=True)

    console.print(f"[green]✓[/green] Case scaffolded at [cyan]{escape(str(dest))}[/cyan]")
    todos = validate_case(dest)
    if todos:
        console.print("The draft will not pass validate (or CI) until these are resolved:")
        for todo in todos:
            console.print(f"  [yellow]{escape(todo)}[/yellow]", highlight=False)
