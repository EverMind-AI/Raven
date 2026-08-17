"""``raven doctor`` — health check (static + optional --probe).

Default mode is zero-network, millisecond-fast. ``--probe`` sends one
chat exchange via :func:`raven.cli._helpers.send_probe`.

Exit codes:
  0  — all green (and probe ok if requested)
  1  — static check failed (config missing / schema invalid / unresolved routing)
  2  — static checks ok but ``--probe`` failed (lets CI distinguish from 1)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console

from raven import __logo__
from raven.cli._helpers import print_probe_troubleshooting, send_probe

console = Console()


@dataclass
class PathsInfo:
    config_path: str
    config_exists: bool
    workspace_path: str = ""
    workspace_exists: bool = False


@dataclass
class RoutingInfo:
    model: str
    provider: Optional[str]
    max_tokens: int
    context_window_tokens: int


@dataclass
class FeaturesInfo:
    channels_enabled: list[str] = field(default_factory=list)
    skill_forge_enabled: bool = False


@dataclass
class GatewayInfo:
    running: bool = False
    pid: Optional[int] = None
    started_at: Optional[float] = None


@dataclass
class ProbeResult:
    ok: bool
    text: Optional[str] = None
    tokens: Optional[int] = None
    elapsed_s: Optional[float] = None
    error: Optional[str] = None


@dataclass
class DoctorReport:
    version: int = 1
    config_loaded: bool = False
    paths: Optional[PathsInfo] = None
    routing: Optional[RoutingInfo] = None
    features: Optional[FeaturesInfo] = None
    gateway: Optional[GatewayInfo] = None
    probe: Optional[ProbeResult] = None

    def exit_code(self) -> int:
        if self.paths is None or not self.paths.config_exists:
            return 1
        if not self.config_loaded:
            return 1
        if self.routing is None or self.routing.provider is None:
            return 1
        if self.probe is not None and not self.probe.ok:
            return 2
        return 0


def _gather_static_checks() -> DoctorReport:
    """Inspect config / routing / features. Strictly zero-network."""
    from raven.config.loader import get_config_path, load_config

    config_path = get_config_path()
    paths = PathsInfo(
        config_path=str(config_path),
        config_exists=config_path.exists(),
    )
    report = DoctorReport(paths=paths)

    if not paths.config_exists:
        return report

    try:
        config = load_config()
    except Exception:
        return report
    report.config_loaded = True

    workspace = config.workspace_path
    paths.workspace_path = str(workspace)
    paths.workspace_exists = workspace.exists()

    defaults = config.agents.defaults
    report.routing = RoutingInfo(
        model=defaults.model,
        provider=config.get_provider_name(),
        max_tokens=defaults.max_tokens,
        context_window_tokens=defaults.context_window_tokens,
    )

    enabled: list[str] = []
    for name, value in config.channels.__dict__.items():
        if getattr(value, "enabled", False):
            enabled.append(name)

    try:
        skill_forge_on = bool(config.skill_forge.enabled)
    except Exception:
        skill_forge_on = False

    report.features = FeaturesInfo(
        channels_enabled=enabled,
        skill_forge_enabled=skill_forge_on,
    )

    from raven.cli._gateway_lock import read_status

    info = read_status(now=time.time())
    if info is None:
        report.gateway = GatewayInfo(running=False)
    else:
        report.gateway = GatewayInfo(running=True, pid=info.pid, started_at=info.started_at)

    return report


def _run_llm_probe(timeout_s: int) -> ProbeResult:
    """Wrap :func:`send_probe` so failures become a structured ProbeResult."""
    try:
        text, tokens, elapsed = send_probe(timeout_s=timeout_s)
        return ProbeResult(ok=True, text=text, tokens=tokens, elapsed_s=elapsed)
    except Exception as exc:
        return ProbeResult(ok=False, error=str(exc) or exc.__class__.__name__)


def _render_human_output(report: DoctorReport) -> None:
    console.print(f"\n{__logo__} Raven Doctor\n")

    paths = report.paths
    assert paths is not None  # _gather_static_checks always populates this
    console.print("[bold]Paths[/bold]")
    if paths.config_exists:
        console.print(f"  Config:    {paths.config_path}  [green]✓[/green]")
    else:
        console.print(f"  Config:    {paths.config_path}  [red]✗  (not found)[/red]")
    if paths.config_exists:
        mark = "[green]✓[/green]" if paths.workspace_exists else "[red]✗[/red]"
        console.print(f"  Workspace: {paths.workspace_path}  {mark}")

    if not paths.config_exists:
        console.print("\n[yellow]⚠ Raven is not configured.[/yellow] Run [cyan]raven onboard[/cyan] to set it up.")
        return

    if not report.config_loaded:
        console.print("\n[red]✗ Config schema invalid.[/red] Run [cyan]raven onboard --reset[/cyan] to recreate it.")
        return

    routing = report.routing
    if routing is not None:
        console.print("\n[bold]Routing[/bold]")
        console.print(f"  Model:        {routing.model}")
        if routing.provider:
            console.print(f"  Routes to:    {routing.provider}")
        else:
            console.print("  Routes to:    [red]<unresolved>[/red]")
        console.print(f"  Max tokens:   {routing.max_tokens}")
        console.print(f"  Context win:  {routing.context_window_tokens}")

    features = report.features
    if features is not None:
        console.print("\n[bold]Features[/bold]")
        count = len(features.channels_enabled)
        if count:
            console.print(f"  Channels:    {count} enabled  ({', '.join(features.channels_enabled)})")
        else:
            console.print("  Channels:    [dim]none enabled[/dim]")
        sf_label = "enabled" if features.skill_forge_enabled else "[dim]disabled[/dim]"
        console.print(f"  Skill forge: {sf_label}")

    gateway = report.gateway
    if gateway is not None:
        console.print("\n[bold]Gateway[/bold]")
        if gateway.running:
            since = (
                datetime.fromtimestamp(gateway.started_at).strftime("%Y-%m-%d %H:%M:%S") if gateway.started_at else "?"
            )
            console.print(f"  [green]✓ running[/green] (pid {gateway.pid}, since {since})")
        else:
            console.print("  [dim]not running[/dim]")

    if report.probe is not None:
        console.print("\n[bold]LLM Probe[/bold]")
        if routing:
            console.print(f"  → {routing.model}")
        if report.probe.ok:
            console.print(f'  [green]✓ Response:[/green] "{report.probe.text}"')
            extras: list[str] = []
            if report.probe.tokens:
                extras.append(f"{report.probe.tokens} tokens")
            if report.probe.elapsed_s is not None:
                extras.append(f"{report.probe.elapsed_s:.1f}s")
            if extras:
                console.print(f"  [green]✓ {', '.join(extras)}[/green]")
        else:
            console.print(f"  [red]✗ Failed:[/red] {report.probe.error}")
            print_probe_troubleshooting(routing.provider if routing else None)

    console.print()
    code = report.exit_code()
    if code == 0:
        if report.probe is None:
            console.print("[green]✓ Configuration looks healthy.[/green]")
            console.print("Run [cyan]doctor --probe[/cyan] to send a test message and verify the LLM responds.")
        else:
            console.print("[green]✓ All checks passed.[/green]")
    elif routing and routing.provider is None:
        console.print(
            f"[red]✗ Model [bold]{routing.model}[/bold] could not be routed to any configured provider.[/red]"
        )
        console.print("Run [cyan]raven provider list[/cyan] / [cyan]raven provider set[/cyan] to fix routing.")


def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        probe: bool = typer.Option(False, "--probe", help="Send a test message to verify the LLM responds."),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON (CI-friendly)."),
        timeout: int = typer.Option(
            15,
            "--timeout",
            help="LLM probe timeout in seconds.",
            min=1,
        ),
    ) -> None:
        """Health-check Raven config, routing, and (optionally) the LLM."""
        report = _gather_static_checks()

        if probe and report.routing is not None and report.routing.provider is not None:
            report.probe = _run_llm_probe(timeout_s=timeout)

        if json_output:
            console.print_json(json.dumps(asdict(report)))
        else:
            _render_human_output(report)

        raise typer.Exit(report.exit_code())


__all__ = ["register"]
