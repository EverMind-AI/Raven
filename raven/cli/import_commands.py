"""Cold-start import CLI commands: scan, run, status."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from raven.cli._plugin_stack import build_plugin_registry, maybe_build_memory_backend
from raven.config.loader import load_config
from raven.importer.orchestrator import ImportSummary, ProgressEvent, run_import
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier

console = Console()

import_app = typer.Typer(
    help="Cold-start import from other AI tools",
    invoke_without_command=True,
    no_args_is_help=True,
)


PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    Platform.CLAUDE_CODE: "Claude Code",
    Platform.CODEX: "Codex",
    Platform.KIMICODE: "Kimi Code",
    Platform.HERMES: "Hermes",
    Platform.OPENCLAW: "OpenClaw",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_scanners() -> list[Scanner]:
    from raven.importer.scanners import ClaudeCodeScanner

    return [ClaudeCodeScanner()]


def _default_state() -> ImportState:
    return ImportState()


async def _scan_all_platforms(
    scanners: list[Scanner] | None = None,
    *,
    platform_filter: Platform | None = None,
) -> list[ScanResult]:
    from loguru import logger

    if scanners is None:
        scanners = _build_scanners()
    if platform_filter:
        scanners = [s for s in scanners if s.platform == platform_filter]
    logger.info("scan started: {} scanner(s)", len(scanners))
    results: list[ScanResult] = []
    for scanner in scanners:
        found = await scanner.scan()
        logger.info(
            "scan {}: {} results",
            scanner.platform.value,
            len(found),
        )
        results.extend(found)
    mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    logger.info("scan completed: {} results ({} memory_file, {} conversation)", len(results), mem, conv)
    return results


def _filter_by_tier(results: list[ScanResult], tier: Tier) -> list[ScanResult]:
    if tier == Tier.FULL:
        return results
    return [r for r in results if r.kind == SourceKind.MEMORY_FILE]


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _platform_option(value: Optional[str]) -> Platform | None:
    if value is None:
        return None
    try:
        return Platform(value)
    except ValueError:
        raise typer.BadParameter(f"Unknown platform {value!r}. Available: {', '.join(p.value for p in Platform)}")


async def _build_and_run(
    items: list[tuple[Scanner, ScanResult]],
    state: ImportState,
    *,
    on_progress: Callable[[ProgressEvent], None] | None = None,
) -> ImportSummary:
    from raven.config.raven import load_raven_config

    workspace = load_config().workspace_path
    ec_config = load_raven_config()
    registry = build_plugin_registry(ec_config)
    backend = maybe_build_memory_backend(workspace, ec_config, registry=registry)
    if backend is None:
        console.print(
            "[red]No memory backend configured. Run `raven onboard` first.[/red]",
        )
        raise typer.Exit(1)

    await backend.start()
    try:
        return await run_import(items, backend, state, on_progress=on_progress)
    finally:
        await backend.stop()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@import_app.command("scan")
def scan_cmd(
    platform: Optional[str] = typer.Option(None, "--platform", help="Filter to a specific platform"),
) -> None:
    """Preview importable data from other AI tools."""
    from loguru import logger as _logger

    _logger.disable("raven")
    platform_filter = _platform_option(platform)

    async def _do() -> list[ScanResult]:
        return await _scan_all_platforms(platform_filter=platform_filter)

    try:
        results = asyncio.run(_do())
    finally:
        _logger.enable("raven")

    if not results:
        console.print("No importable data found.")
        console.print(f"Supported platforms: {', '.join(PLATFORM_DISPLAY_NAMES.values())}")
        return

    table = Table(title="Cold-Start Import -- Available Sources")
    table.add_column("Platform")
    table.add_column("Kind")
    table.add_column("Source Key")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")

    for r in sorted(results, key=lambda x: (x.platform, x.kind, x.source_key)):
        table.add_row(
            PLATFORM_DISPLAY_NAMES.get(r.platform.value, r.platform.value),
            r.kind.value,
            r.source_key,
            str(len(r.file_paths)),
            _format_size(r.estimated_size),
        )

    console.print(table)
    mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    console.print(f"\nTotal: {len(results)} items ({mem} memory files, {conv} conversations)")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@import_app.command("status")
def status_cmd(
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Show cold-start import progress."""
    import time
    from collections import Counter

    from rich.progress_bar import ProgressBar
    from rich.table import Table

    from raven.config.paths import get_logs_dir

    state = _default_state()
    progress = state.get_progress()
    entries = progress.get("entries", {})
    meta = progress.get("meta", {})
    total = meta.get("total", len(entries))

    if not total and not entries:
        if output_json:
            console.print(json.dumps({"total": 0, "submitted": 0, "failed": 0, "skipped": 0, "status": "none"}))
        else:
            console.print("No import in progress. Run `raven import run` to start.")
        return

    # Compute counts
    status_counts = Counter(v.get("status") for v in entries.values())
    submitted = status_counts.get("submitted", 0)
    failed = status_counts.get("failed", 0)
    done = submitted + failed
    remaining = max(0, total - done)

    # Per-platform breakdown
    platform_stats: dict[str, dict[str, int]] = {}
    failed_items: list[tuple[str, str]] = []
    timestamps: list[float] = []
    for key, entry in entries.items():
        platform = key.split(":", 1)[0] if ":" in key else "unknown"
        if platform not in platform_stats:
            platform_stats[platform] = {"submitted": 0, "failed": 0, "total": 0}
        platform_stats[platform]["total"] += 1
        platform_stats[platform][entry.get("status", "unknown")] = (
            platform_stats[platform].get(entry.get("status", "unknown"), 0) + 1
        )
        if entry.get("timestamp"):
            timestamps.append(entry["timestamp"])
        if entry.get("status") == "failed":
            failed_items.append((key, entry.get("error", "unknown error")))

    # Timing
    now = time.time()
    last_update = max(timestamps) if timestamps else 0
    first_update = min(timestamps) if timestamps else 0

    if output_json:
        console.print(
            json.dumps(
                {
                    "total": total,
                    "submitted": submitted,
                    "failed": failed,
                    "remaining": remaining,
                    "entries": entries,
                }
            )
        )
        return

    # Visual output
    console.print("\n [bold]Cold-Start Import Status[/bold]\n")

    # Progress bar
    pct = int(done / total * 100) if total else 0
    bar = ProgressBar(total=total, completed=done, width=30)
    console.print(" ", bar, f" {pct}%  {done}/{total}\n")

    # Platform table
    table = Table(show_header=True, box=None, padding=(0, 2, 0, 0))
    table.add_column("Platform", style="bold")
    table.add_column("Submitted", justify="right", style="green")
    if failed:
        table.add_column("Failed", justify="right", style="yellow")
    table.add_column("Remaining", justify="right")
    table.add_column("Total", justify="right")
    for plat, stats in sorted(platform_stats.items()):
        display_name = PLATFORM_DISPLAY_NAMES.get(plat, plat)
        plat_done = stats.get("submitted", 0) + stats.get("failed", 0)
        plat_remaining = stats["total"] - plat_done
        row = [display_name, str(stats.get("submitted", 0))]
        if failed:
            row.append(str(stats.get("failed", 0)))
        row.append(str(plat_remaining))
        row.append(str(stats["total"]))
        table.add_row(*row)
    console.print(table)

    # Timing
    console.print()
    if first_update and last_update:
        duration = int(last_update - first_update)
        mins, secs = divmod(duration, 60)
        console.print(f" Duration:  {mins}m {secs}s")
    if last_update:
        ago = int(now - last_update)
        console.print(f" Updated:   {ago}s ago")

    log_path = get_logs_dir() / "import.log"
    console.print(f" Log:       {log_path}")

    # Failed items
    if failed_items:
        console.print()
        console.print(" [yellow]Failed items:[/yellow]")
        for key, error in failed_items:
            console.print(f"   {key}: {error}")
        console.print()
        console.print(" Run `raven import run` to retry failed items.")

    console.print()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@import_app.command("run")
def run_cmd(
    platform: Optional[str] = typer.Option(None, "--platform", help="Platform to import from"),
    tier: Optional[str] = typer.Option(None, "--tier", help="Import tier: memory_files or full"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Interactive cold-start import: scan, select, execute."""
    asyncio.run(_run_async(platform=platform, tier=tier, yes=yes))


async def _run_async(
    *,
    platform: str | None,
    tier: str | None,
    yes: bool,
) -> None:
    from loguru import logger as _logger

    from raven.cli._log_file import redirect_loguru_to_file

    log_path = redirect_loguru_to_file("import.log", terminal_level=None)

    platform_filter = _platform_option(platform)
    all_results = await _scan_all_platforms(platform_filter=platform_filter)

    if not all_results:
        console.print("No importable data found.")
        return

    if platform_filter is None:
        platforms_found = sorted({r.platform for r in all_results})
        if len(platforms_found) == 1:
            platform_filter = platforms_found[0]
        else:
            picked = _pick_platform(platforms_found)
            if picked is None:
                return
            platform_filter = picked
            all_results = [r for r in all_results if r.platform == platform_filter]

    if tier is not None:
        try:
            selected_tier = Tier(tier)
        except ValueError:
            console.print(f"[red]Unknown tier {tier!r}. Use 'memory_files' or 'full'.[/red]")
            raise typer.Exit(1)
    else:
        selected_tier = _pick_tier(all_results)
        if selected_tier is None:
            return

    filtered = _filter_by_tier(all_results, selected_tier)
    if not filtered:
        console.print("No items match the selected tier.")
        return

    mem = sum(1 for r in filtered if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in filtered if r.kind == SourceKind.CONVERSATION)
    console.print(
        f"\nAbout to import {len(filtered)} items "
        f"({mem} memory files, {conv} conversations) "
        f"from {platform_filter.value if platform_filter else 'all platforms'}.",
    )
    if not yes:
        if not typer.confirm("Proceed?", default=True):
            return

    scanners = _build_scanners()
    scanner_map = {s.platform: s for s in scanners}
    items: list[tuple[Scanner, ScanResult]] = []
    for r in filtered:
        scanner = scanner_map.get(r.platform)
        if scanner:
            items.append((scanner, r))

    state = _default_state()
    state.set_total(len(items))

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Importing...", total=len(items))

            def on_progress(event: ProgressEvent) -> None:
                progress.update(
                    task_id,
                    advance=1,
                    description=f"[{event.current}/{event.total}] {event.platform}/{event.source_key}",
                )

            summary = await _build_and_run(items, state, on_progress=on_progress)
    finally:
        _logger.remove()
        _logger.add(sys.stderr, level="WARNING")

    _print_summary(summary, log_path=log_path)


def _pick_platform(platforms: list[Platform]) -> Platform | None:
    try:
        questionary = _require_questionary()
    except SystemExit:
        return None
    choices = [{"name": p.value, "value": p} for p in platforms]
    picked = questionary.select(
        "Select platform:",
        choices=choices,
    ).ask()
    return picked


def _pick_tier(results: list[ScanResult]) -> Tier | None:
    try:
        questionary = _require_questionary()
    except SystemExit:
        return None
    mem_count = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv_count = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    choices = []
    if mem_count:
        choices.append(
            {"name": f"Memory files only ({mem_count} items, fast)", "value": Tier.MEMORY_FILES},
        )
    choices.append(
        {
            "name": f"Full import ({mem_count + conv_count} items, includes conversations)",
            "value": Tier.FULL,
        },
    )
    picked = questionary.select(
        "Select import tier:",
        choices=choices,
    ).ask()
    return picked


def _require_questionary() -> Any:
    try:
        import questionary

        return questionary
    except ImportError:
        console.print(
            "[red]questionary is required for interactive mode. Install it or use --platform and --tier flags.[/red]",
        )
        raise typer.Exit(1)


def _print_summary(summary: ImportSummary, *, log_path: Path | None = None) -> None:
    console.print()
    if summary.failed:
        console.print("[bold yellow]Import Complete (with errors)[/bold yellow]\n")
    else:
        console.print("[bold green]Import Complete[/bold green]\n")
    console.print(f"  Submitted: {summary.submitted}  [green]✅[/green]")
    if summary.skipped:
        console.print(f"  Skipped:   {summary.skipped}  (already imported)")
    if summary.failed:
        console.print(f"  Failed:    {summary.failed}  [yellow]⚠️[/yellow]")
        console.print()
        for err in summary.errors:
            console.print(f"    {err.platform}/{err.source_key}: {err.error}")
        console.print()
        console.print("Run `raven import run` to retry failed items.")
    if log_path:
        console.print(f"  Log: {log_path}")
    console.print()
