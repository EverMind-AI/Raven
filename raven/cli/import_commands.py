"""Cold-start import CLI commands: scan, run, status."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import typer
from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from raven.cli._plugin_stack import build_plugin_registry, maybe_build_memory_backend
from raven.config.loader import load_config
from raven.config.schema import Config
from raven.importer.orchestrator import ImportSummary, ProgressEvent, run_import
from raven.importer.skills import SkillOrigin
from raven.importer.skills.hermes import HermesSkillSource
from raven.importer.skills.installer import SkillImportSummary, install_skills
from raven.importer.state import ImportState
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind, Tier, filter_by_tier

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

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


def _default_state() -> ImportState:
    return ImportState()


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
    cancel_path: Path | None = None,
) -> ImportSummary:
    from raven.config.raven import load_raven_config

    config = load_config()
    workspace = config.workspace_path
    ec_config = load_raven_config()
    registry = build_plugin_registry(ec_config)
    backend = maybe_build_memory_backend(workspace, ec_config, registry=registry)
    if backend is None:
        console.print(
            "[red]No memory backend configured. Run `raven onboard` first.[/red]",
        )
        raise typer.Exit(1)

    try:
        await backend.start()
    except Exception as e:
        console.print(f"[red]Failed to start EverOS memory server: {e}[/red]")
        console.print("[dim]Check the server log: ~/.raven/logs/everos-server.log[/dim]")
        console.print("[dim]Retry: raven import run[/dim]")
        raise typer.Exit(1)
    try:
        summary = await run_import(items, backend, state, on_progress=on_progress, cancel_path=cancel_path)
        # The native mirror is additive and runs after the EverOS pass, so any
        # failure in it must be surfaced without reversing an import that has
        # already succeeded. loguru is redirected to a file during `run`, so the
        # console line is what the user actually sees.
        try:
            await _land_hermes_user_md(items, workspace, config)
        except Exception as exc:
            logger.warning("hermes user.md mirror failed: {}", exc)
            console.print(
                f"[yellow]Imported to EverOS, but mirroring USER.md into the native profile failed: {exc}[/yellow]"
            )
        try:
            skill_summary = await _install_hermes_skills(items, workspace, state)
        except Exception as exc:
            logger.warning("hermes skill import failed: {}", exc)
            console.print(f"[yellow]Imported to EverOS, but installing Hermes skills failed: {exc}[/yellow]")
            skill_summary = None
        if skill_summary is not None:
            console.print(_format_skill_summary(skill_summary))
        return summary
    finally:
        await backend.stop()


def _report_scan_error(platform: Platform, error: BaseException) -> None:
    """Say which platform could not be scanned, and why, on the console.

    `scan` silences the raven logger and `run` sends it to a file, so a scanner
    failure would otherwise show up only as that platform's data quietly missing
    from the table. Hermes has to shell out to enumerate conversations, so this
    is reachable simply by having its binary off PATH.
    """
    name = PLATFORM_DISPLAY_NAMES.get(platform.value, platform.value)
    console.print(f"[yellow]Could not scan {name}: {error}[/yellow]")
    console.print("[dim]Other platforms were scanned normally.[/dim]")


def _format_skill_summary(summary: SkillImportSummary) -> str:
    """One standalone sentence, worded like the ``scan`` line.

    It lands before ``_print_summary``'s indented block, so it must read on its
    own rather than borrow that block's ``  Label:`` shape. Naming the untouched
    factory skills matters: without it, a run that installs 12 of 82 looks like
    it dropped 70.
    """
    parts = [f"{summary.installed} installed"]
    if summary.pristine:
        parts.append(f"{summary.pristine} left as factory content")
    if summary.skipped:
        parts.append(f"{summary.skipped} already present")
    if summary.failed:
        parts.append(f"{summary.failed} failed")
    return f"Hermes skills: {', '.join(parts)}"


async def _install_skills_without_a_scan(platform_filter: Platform | None) -> bool:
    """Install Hermes skills on the paths where no ScanResult survived.

    Skills are directories rather than message sources, so they never appear as
    ScanResults and the tier filter has nothing of theirs to keep. An install
    whose only importable data is skills therefore reached an early return and
    was told there was nothing to import. Reached only when the normal path did
    not run, so a skill is never installed twice.
    """
    if platform_filter not in (None, Platform.HERMES):
        return False
    summary = await install_skills(HermesSkillSource(), load_config().workspace_path, _default_state())
    if summary.total == 0:
        return False
    console.print(_format_skill_summary(summary))
    return True


async def _install_hermes_skills(
    items: list[tuple[Scanner, ScanResult]],
    workspace: Path,
    state: ImportState,
) -> SkillImportSummary | None:
    """Install Hermes skills once per run, only when Hermes is in scope.

    Skills are directories, not message sources, so they never travel as a
    ScanResult; Hermes' presence among the scanned items is what "in scope"
    means here, mirroring how ``_land_hermes_user_md`` detects it.
    """
    if not any(result.platform is Platform.HERMES for _scanner, result in items):
        return None
    return await install_skills(HermesSkillSource(), workspace, state)


async def _land_hermes_user_md(
    items: list[tuple[Scanner, ScanResult]],
    workspace: Path,
    config: Config,
) -> None:
    """Mirror the Hermes ``user-md`` source into the native ``user.md`` profile.

    EverOS storage alone does not reach every consumer: Curator, Personalizer,
    and the Sentinel producers read ``user_memory/profile/user.md`` directly
    and never see EverOS-only content.
    """
    from raven.importer.hermes_user_md import import_user_md_sections
    from raven.importer.scanners.hermes import split_memory_entries
    from raven.memory_engine.consolidate.consolidator import MemoryStore

    for _scanner, result in items:
        if result.platform is not Platform.HERMES or result.source_key != "user-md":
            continue
        (path,) = result.file_paths
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("hermes user.md mirror skipped: {}", exc)
            return
        entries = split_memory_entries(raw)
        if not entries:
            return
        written = await import_user_md_sections(
            entries,
            MemoryStore(workspace),
            provider=_make_hermes_provider(config),
            model=config.agents.defaults.model,
        )
        logger.info("hermes user.md mirror: {} entries landed", len(written))
        # The skill phase prints its own line, so staying silent here made a
        # successful mirror look like it had not run. loguru is file-only during
        # `run`, which is why this is a console print rather than a log call.
        parts = [f"{len(written)} entries into the native profile"]
        if written.skipped:
            parts.append(f"{written.skipped} already present")
        console.print(f"Hermes USER.md: {', '.join(parts)}")
        return


def _make_hermes_provider(config: Config) -> "LLMProvider | None":
    """Best-effort provider for the USER.md heading classifier.

    ``check_provider_credentials`` (called inside ``make_provider``) raises when
    no LLM credentials are configured; import cold-start must still succeed in
    that case, falling back to the ``## Notes`` heading.

    Built eagerly rather than lazily so litellm is imported here: it reattaches
    its own stderr StreamHandler on import, and ``redirect_loguru_to_file`` has
    already stripped TTY handlers by this point, so a deferred import would put
    litellm's DEBUG chatter straight onto the terminal that is supposed to show
    only the progress bar. Stripping again right after the import closes that
    window. There is nothing to gain from laziness in a one-shot command.
    """
    from raven.cli._helpers import make_provider
    from raven.cli._log_file import _strip_tty_stream_handlers

    try:
        provider = make_provider(config)
    except Exception as exc:
        logger.info("hermes user.md mirror: no LLM provider available ({}); using fallback heading", exc)
        return None
    _strip_tty_stream_handlers()
    return provider


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

    async def _do() -> tuple[list[ScanResult], int | None]:
        from raven.importer.scanners import scan_all

        results = await scan_all(platform_filter=platform_filter, on_error=_report_scan_error)
        skill_count = None
        if platform_filter is None or platform_filter is Platform.HERMES:
            skills = await HermesSkillSource().discover()
            skill_count = sum(1 for s in skills if s.origin is not SkillOrigin.BUNDLED_PRISTINE)
        return results, skill_count

    try:
        results, skill_count = asyncio.run(_do())
    finally:
        _logger.enable("raven")

    if not results:
        # Skills do not travel as ScanResults, so an install whose only
        # importable data is skills has an empty results list. Returning here
        # unconditionally told such a user there was nothing to import while
        # a dozen of their own skills were waiting.
        if skill_count:
            console.print(f"Hermes skills: {skill_count} importable")
            console.print("No memory files or conversations found.")
        else:
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
        # A scanner that cannot cost its unit up front leaves both at zero --
        # hermes' conversation listing reports only an id and a source, so
        # rendering "0" and "0 B" there would read as an empty conversation.
        has_files = bool(r.file_paths)
        table.add_row(
            PLATFORM_DISPLAY_NAMES.get(r.platform.value, r.platform.value),
            r.kind.value,
            r.source_key,
            str(len(r.file_paths)) if has_files else "-",
            _format_size(r.estimated_size) if has_files else "-",
        )

    console.print(table)
    mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    console.print(f"\nTotal: {len(results)} items ({mem} memory files, {conv} conversations)")
    if skill_count is not None:
        console.print(f"Hermes skills: {skill_count} importable")


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

    from raven.config.paths import get_logs_dir

    state = _default_state()
    progress = state.get_progress()
    entries = {k: v for k, v in progress.get("entries", {}).items() if ":" in k}
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
    console.print(" ", bar, f" {pct}%  {done}/{total}")

    cancel_file = state.cancel_path
    if cancel_file.exists() and remaining > 0:
        console.print("  [yellow]Cancelled[/yellow]\n")
    else:
        console.print()

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
        ago_mins, ago_secs = divmod(ago, 60)
        if ago_mins:
            console.print(f" Updated:   {ago_mins}m {ago_secs}s ago")
        else:
            console.print(f" Updated:   {ago_secs}s ago")

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


@import_app.command("stop")
def stop_cmd() -> None:
    """Cancel a running background import."""
    state = _default_state()
    if not state._path.exists():
        console.print("No import in progress.")
        return
    cancel = state.cancel_path
    if cancel.exists():
        console.print("Import is already being cancelled.")
        return
    cancel.touch()
    console.print("Import cancelled. Running item will complete, remaining items skipped.")
    console.print("Use `raven import status` to view progress.")


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

    cancel = _default_state().cancel_path
    if cancel.exists():
        cancel.unlink(missing_ok=True)

    platform_filter = _platform_option(platform)
    from raven.importer.scanners import scan_all

    all_results = await scan_all(platform_filter=platform_filter, on_error=_report_scan_error)

    if not all_results:
        if not await _install_skills_without_a_scan(platform_filter):
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

    filtered = filter_by_tier(all_results, selected_tier)
    if not filtered:
        if not await _install_skills_without_a_scan(platform_filter):
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

    from raven.importer.scanners import build_scanners

    scanners = build_scanners()
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

            summary = await _build_and_run(items, state, on_progress=on_progress, cancel_path=state.cancel_path)
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
    if summary.cancelled:
        console.print("[bold yellow]Import Cancelled[/bold yellow]\n")
    elif summary.failed:
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
    if summary.cancelled:
        remaining = summary.total - summary.submitted - summary.skipped - summary.failed
        console.print(f"  Remaining: {remaining}")
        console.print()
        console.print("Run `raven import run` to continue remaining items.")
    elif summary.failed:
        console.print()
        console.print("Run `raven import run` to retry failed items.")
    if log_path:
        console.print(f"  Log: {log_path}")
    console.print()
