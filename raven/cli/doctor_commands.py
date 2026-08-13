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
from typing import TYPE_CHECKING, Optional

import typer
from rich.console import Console

from raven import __logo__
from raven.cli._helpers import print_probe_troubleshooting, send_probe

if TYPE_CHECKING:
    from raven.config.raven import RavenConfig

console = Console()


@dataclass
class PathsInfo:
    config_path: str
    config_exists: bool
    config_valid: bool = False
    config_invalid_reason: str = ""
    workspace_path: str = ""
    workspace_exists: bool = False


@dataclass
class RoutingInfo:
    model: str
    provider: Optional[str]
    max_tokens: int
    context_window_tokens: Optional[int]


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
class MemoryInfo:
    """What the memory backend is, and what it can actually do.

    ``configured`` comes from the config files; ``capabilities`` from a running
    server's ``/health``. Keeping both is the point: from everos 1.2.1 a server
    whose embedding provider failed to build still answers 200 and degrades to
    keyword-only search, so the two can disagree, and that disagreement is the
    fault worth reporting.
    """

    backend: Optional[str] = None
    server_running: bool = False
    reports_capabilities: bool = False
    configured: list[str] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    retrieval: Optional[str] = None

    @property
    def unbuilt(self) -> list[str]:
        """Roles the user configured that the server could not build."""
        from raven.plugin.memory.everos._health import capability_available

        return [s for s in self.configured if capability_available(self.capabilities, s) is False]

    @property
    def broken(self) -> list[str]:
        """Unbuilt roles that memory cannot work without at all.

        Separate from :attr:`unbuilt` because the others cost quality, not
        function: without embedding the adapter searches lexically instead of
        semantically, and that is a worse memory rather than no memory. Only this
        list decides the exit code.
        """
        from raven.plugin.memory.everos._health import REQUIRED_SECTIONS

        return [s for s in self.unbuilt if s in REQUIRED_SECTIONS]


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
    memory: Optional[MemoryInfo] = None
    probe: Optional[ProbeResult] = None

    def exit_code(self) -> int:
        if self.paths is None or not self.paths.config_exists:
            return 1
        if not self.paths.config_valid:
            return 1
        if not self.config_loaded:
            return 1
        if self.routing is None or self.routing.provider is None:
            return 1
        if self.probe is not None and not self.probe.ok:
            return 2
        # A role the user configured that the server could not build is a real
        # fault, not a warning: recall silently returns nothing.
        if self.memory is not None and self.memory.broken:
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

    # Classify config validity with load_config's eyes: a syntax error, an
    # empty file, and a non-object top level all mean no settings were read.
    # Inspect the file directly -- load_config swallows syntax errors into
    # defaults, and read_raw_or_raise folds the last two cases into {} for
    # its read-modify-write callers, so neither can classify all three.
    try:
        text = config_path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else None
    except (OSError, UnicodeDecodeError, ValueError):
        paths.config_invalid_reason = "invalid JSON"
    else:
        if not text.strip():
            paths.config_invalid_reason = "empty"
        elif not isinstance(data, dict):
            paths.config_invalid_reason = "not a JSON object"
    paths.config_valid = not paths.config_invalid_reason

    try:
        config = load_config()
    except Exception:
        return report
    report.config_loaded = True

    workspace = config.workspace_path
    paths.workspace_path = str(workspace)
    paths.workspace_exists = workspace.exists()

    from raven.providers.rates import resolve_max_output_tokens

    defaults = config.agents.defaults
    report.routing = RoutingInfo(
        model=defaults.model,
        provider=config.get_provider_name(),
        # What a request will actually carry, resolved the same way the
        # provider resolves it -- doctor reporting a configured number that
        # no longer exists would be reporting a setting, not the behaviour.
        max_tokens=resolve_max_output_tokens(defaults.model),
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


def _probe_memory(config: "RavenConfig") -> MemoryInfo:
    """Ask the memory server what it can do. Local HTTP only, never raises.

    Deliberately not part of ``_gather_static_checks``: that stays zero-network.
    This one talks to localhost, which is cheap enough to run unconditionally --
    unlike ``--probe``, it spends no tokens and reaches no third party.
    """
    backend = config.memory.backend
    info = MemoryInfo(backend=backend)
    if backend != "everos":
        return info
    from raven.config.update_everos import everos_role_configured
    from raven.plugin.memory.everos._health import (
        DEGRADING_SECTIONS,
        REQUIRED_SECTIONS,
        configured_base_url,
        probe_capabilities,
    )

    info.configured = [s for s in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS) if everos_role_configured(s)]
    # Recall quality is decided by the embedding role in the user-level
    # everos.toml: with it recall matches meaning, without it only keywords.
    info.retrieval = "semantic" if "embedding" in info.configured else "keyword-only"
    report = probe_capabilities(configured_base_url(config))
    info.server_running = report.reachable
    info.reports_capabilities = report.reports_capabilities
    info.capabilities = dict(report.capabilities)
    return info


def _run_llm_probe(timeout_s: int) -> ProbeResult:
    """Wrap :func:`send_probe` so failures become a structured ProbeResult."""
    try:
        text, tokens, elapsed = send_probe(timeout_s=timeout_s)
        return ProbeResult(ok=True, text=text, tokens=tokens, elapsed_s=elapsed)
    except Exception as exc:
        return ProbeResult(ok=False, error=str(exc) or exc.__class__.__name__)


def _render_memory_capabilities(memory: MemoryInfo) -> None:
    """Report the running server's capabilities, or say why they are unknown.

    "Server running" and "server can recall" stopped being the same statement in
    everos 1.2.1, so they are printed as separate lines rather than one tick.
    """
    from raven.plugin.memory.everos._health import capability_available

    if memory.backend != "everos":
        return
    if not memory.server_running:
        console.print("  Server:     [dim]not running  (starts on demand)[/dim]")
        if memory.configured:
            console.print(f"  Configured: {', '.join(memory.configured)}")
        return
    console.print("  Server:     [green]running[/green]")
    if not memory.reports_capabilities:
        console.print("  [dim]This server does not report capabilities (everos < 1.2.1).[/dim]")
        if memory.configured:
            console.print(f"  Configured: {', '.join(memory.configured)}")
        return
    from raven.plugin.memory.everos._health import DEGRADING_SECTIONS, REQUIRED_SECTIONS

    for section in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS):
        label = f"  {section + ':':<12}"
        if section not in memory.configured:
            console.print(f"{label}[dim]not configured{_degradation_note(section)}[/dim]")
            continue
        state = capability_available(memory.capabilities, section)
        if state is True:
            console.print(f"{label}[green]✓[/green]")
        elif state is False:
            console.print(f"{label}[red]✗ configured, but the server could not build it[/red]")
        else:
            console.print(f"{label}[dim]not reported[/dim]")
    if memory.unbuilt:
        console.print()
        if memory.broken:
            console.print(
                f"  [yellow]⚠ Memory needs {' and '.join(memory.broken)} and cannot work until this is fixed.[/yellow]"
            )
        else:
            # "memory", not "recall": an unbuilt multimodal llm costs ingest of
            # images / PDFs / audio, which recall never sees either way.
            console.print(
                f"  [yellow]⚠ {' and '.join(memory.unbuilt)} is configured but unavailable, so memory "
                "runs degraded.[/yellow]"
            )
        console.print(f"  [dim]Check the server log: {_server_log_hint()}[/dim]")


def _degradation_note(section: str) -> str:
    """What is lost by leaving an optional role unconfigured.

    Stated per role rather than as one blanket "optional": they degrade
    differently, and a user deciding whether to configure embedding needs to know
    it costs semantic recall specifically.
    """
    return {
        "embedding": "  (recall matches keywords, not meaning)",
        "rerank": "  (agent-track recall uses the LLM lane instead of a cross-encoder)",
        "multimodal": "  (images, PDFs and audio stay out of memory)",
    }.get(section, "")


def _server_log_hint() -> str:
    from raven.plugin.memory.everos._server import server_log_path

    return str(server_log_path())


def _render_human_output(report: DoctorReport) -> None:
    console.print(f"\n{__logo__} Raven Doctor\n")

    paths = report.paths
    assert paths is not None  # _gather_static_checks always populates this
    console.print("[bold]Paths[/bold]")
    if not paths.config_exists:
        console.print(f"  Config:    {paths.config_path}  [red]✗  (not found)[/red]")
    elif not paths.config_valid:
        reason = paths.config_invalid_reason or "invalid JSON"
        console.print(f"  Config:    {paths.config_path}  [yellow]⚠  {reason} (running on defaults)[/yellow]")
    else:
        console.print(f"  Config:    {paths.config_path}  [green]✓[/green]")
    if paths.config_exists:
        mark = "[green]✓[/green]" if paths.workspace_exists else "[red]✗[/red]"
        console.print(f"  Workspace: {paths.workspace_path}  {mark}")

    if not paths.config_exists:
        console.print("\n[yellow]⚠ Raven is not configured.[/yellow] Run [cyan]raven onboard[/cyan] to set it up.")
        return

    if not report.config_loaded:
        if paths.config_valid:
            console.print(
                "\n[red]✗ Config schema invalid.[/red] Run [cyan]raven onboard --reset[/cyan] to recreate it."
            )
        else:
            reason = paths.config_invalid_reason or "invalid JSON"
            console.print(f"\n[yellow]⚠ Config file is {reason}; the checks above ran on built-in defaults.[/yellow]")
            console.print(f"Fix [cyan]{paths.config_path}[/cyan] or run [cyan]raven onboard --reset[/cyan].")
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
        console.print(f"  Context win:  {routing.context_window_tokens if routing.context_window_tokens else 'auto'}")

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

    memory = report.memory
    if memory is not None and memory.backend:
        console.print("\n[bold]Memory[/bold]")
        console.print(f"  Backend:    {memory.backend}")
        if memory.retrieval == "semantic":
            console.print("  Retrieval:  semantic")
        elif memory.retrieval:
            console.print("  Retrieval:  [dim]keyword-only  (no embedding key)[/dim]")
        _render_memory_capabilities(memory)

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
    elif not paths.config_valid:
        reason = paths.config_invalid_reason or "invalid JSON"
        console.print(f"[yellow]⚠ Config file is {reason}; the checks above ran on built-in defaults.[/yellow]")
        console.print(f"Fix [cyan]{paths.config_path}[/cyan] (JSON allows no comments or trailing commas).")
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

        if report.config_loaded:
            from raven.config.raven import load_raven_config

            report.memory = _probe_memory(load_raven_config())

        if probe and report.routing is not None and report.routing.provider is not None:
            report.probe = _run_llm_probe(timeout_s=timeout)

        if json_output:
            console.print_json(json.dumps(asdict(report)))
        else:
            _render_human_output(report)

        raise typer.Exit(report.exit_code())


__all__ = ["register"]
