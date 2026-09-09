"""``raven trajectory regression`` subapp — gate (and later scaffold) regression cases.

Regression cases live under ``tests/trajectories/<case>/``: an ``expect.yaml``
(the assertion DSL), a ``case.yaml`` (the human contract), and a ``cassette/``
(a minimized, redacted bundle). ``validate`` is the static commit gate the CI
trajectory job runs (:func:`raven.trajectory.regression.validate_case`):
schemas, cassette completeness down to the replay contract, residual-scan
coverage and cleanliness, and the size budget. It never replays — the pytest
suite does that.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from raven.trajectory.regression import discover_case_dirs, validate_case

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
