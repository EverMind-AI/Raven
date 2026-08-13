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
from typing import TYPE_CHECKING, Any, Optional

import typer
from rich.console import Console

from raven import __logo__
from raven.cli._helpers import print_probe_troubleshooting, send_probe

if TYPE_CHECKING:
    from pathlib import Path

    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

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
    channels_missing_deps: list[str] = field(default_factory=list)
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
    root: Optional[str] = None
    owned: bool = True
    address: Optional[str] = None
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
class ConfigHealth:
    """What the config says that the migrations deliberately did not change.

    The migrations run at load, so by the time this command looks there is
    nothing pending -- they already happened, silently, one launch ago. What is
    left for a person to ask about is the set of things Raven will not decide
    on their behalf: a window they pinned that is smaller than the model can
    hold, and a provider nothing could resolve. Both are legitimate
    configurations and both are common mistakes, which is exactly why they need
    somewhere to be asked rather than a rule that guesses.
    """

    findings: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)


@dataclass
class ToolCapabilityInfo:
    """One credential-bearing tool, as the deployer needs to see it.

    Reported whether or not it is configured, which is the point: an
    unconfigured tool is not registered, so nothing else in the running system
    mentions that the capability exists at all.
    """

    tool: str
    summary: str
    #: ``nothing`` / ``own_credential`` / ``new_account`` -- how much the
    #: deployer has to do, which is a different question from configured-ness.
    need: str
    configured: bool
    #: Where a configured one got its credential; empty when unconfigured or
    #: when none was needed.
    source: str = ""
    config_path: str = ""
    env_var: str = ""
    obtain_from: str = ""
    cost_note: str = ""


@dataclass
class ToolsInfo:
    capabilities: list[ToolCapabilityInfo] = field(default_factory=list)

    @property
    def unconfigured(self) -> list[ToolCapabilityInfo]:
        return [c for c in self.capabilities if not c.configured]


@dataclass
class DoctorReport:
    version: int = 1
    config_loaded: bool = False
    paths: Optional[PathsInfo] = None
    routing: Optional[RoutingInfo] = None
    features: Optional[FeaturesInfo] = None
    gateway: Optional[GatewayInfo] = None
    memory: Optional[MemoryInfo] = None
    tools: Optional[ToolsInfo] = None
    probe: Optional[ProbeResult] = None
    config_health: Optional[ConfigHealth] = None

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


def _routes_anywhere(provider: str) -> bool:
    """Is this a provider name anything can route to?

    Deliberately generous: it accepts a vendor Raven carries no spec for as long
    as LiteLLM knows the name -- mistral and xai are supported exactly that way,
    and reporting them as broken would be worse than saying nothing. What it
    catches is a name nothing has ever heard of, which is a typo.
    """
    from raven.config.update_providers import ensure_routable_provider

    try:
        ensure_routable_provider(provider)
    except KeyError:
        # Only the answer this asks for. A broader catch would file a genuine
        # fault in the lookup as an ordinary "unroutable" finding, which reads
        # as the user's problem instead of ours -- and `provider use` catches
        # exactly this one for the same reason.
        return False
    return True


def _inspect_config_health(config: Any, *, fix: bool) -> ConfigHealth:
    """Ask the two questions the migrations refuse to answer for the user.

    ``fix`` is the consent: without it this only reports, because the finding is
    a value someone may have meant -- a window pinned below the model is a real
    configuration for a self-hosted endpoint served smaller than the catalogue
    thinks.

    The provider checks are reported and never fixed, and that is not an
    omission. Only the user knows which vendor they meant to pay: the load-time
    migration already tried the derivation and wrote down its answer wherever it
    had one, so a provider still blank here is one the derivation could not
    resolve. Guessing again in a command called ``--fix`` would be the same
    guess under a more confident name.
    """
    from raven.config.loader import get_config_path, read_raw_or_raise
    from raven.providers.rates import resolve_context_window

    health = ConfigHealth()
    defaults = config.agents.defaults
    pinned = defaults.context_window_tokens
    model = defaults.model

    if pinned and model:
        real = resolve_context_window(model)
        if real and pinned < real:
            health.findings.append(
                f"contextWindowTokens is pinned to {pinned:,}, but {model} holds {real:,}. "
                f"Everything is sized against the pin: the history budget, when the Curator "
                f"starts paying for a slow path, and when memory consolidation archives."
            )
            health.fixes.append("remove agents.defaults.contextWindowTokens so the window follows each model")

    provider = (getattr(defaults, "provider", "") or "").strip()
    # ``auto`` counts as unset, the way ``Config._match_provider`` counts it. The
    # migration leaves the literal in place when it cannot resolve a vendor, so
    # this is a reachable state and precisely the legacy case this check is for.
    # Read as a name instead, it is unroutable, and the user is told to fix a
    # typo they did not make while the real advice goes unsaid.
    if not provider or provider == "auto":
        health.findings.append(
            "agents.defaults.provider is not set, so the vendor serving "
            f"{model or 'your model'} is derived from its id. A model id does not name whose "
            "credential answers for it, and the derivation walks a list -- with two vendors "
            "configured, which one pays can come down to their order in it."
        )
        if provider == "auto":
            health.findings.append(
                "  (your config says `auto`, which is the retired spelling of unset -- it never detected anything)"
            )
        health.findings.append(f"  raven provider use {model or '<model>'} --provider <name>")
    elif not _routes_anywhere(provider):
        health.findings.append(
            f"agents.defaults.provider is {provider!r}, which nothing routes to. "
            "Every call resolves against a vendor that does not exist, so the credential "
            "it would use is never found."
        )
        health.findings.append("  raven provider list  # the names this accepts")

    if fix and health.fixes:
        path = get_config_path()
        try:
            raw = read_raw_or_raise(path)
            raw.get("agents", {}).get("defaults", {}).pop("contextWindowTokens", None)
            raw.get("agents", {}).get("defaults", {}).pop("context_window_tokens", None)
            _write_config_preserving_mode(path, raw)
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal
            health.findings.append(f"could not write the fix: {exc}")
        else:
            health.applied = list(health.fixes)
            health.fixes = []

    return health


def _write_config_preserving_mode(path: "Path", raw: dict) -> None:
    """Atomic replace that carries the original's mode across.

    ``os.replace`` swaps the inode, so the mode of what lands is the temp
    file's: a config the user tightened to owner-only (it holds
    ``providers.*.apiKey``) would come back world-readable. Same rule the
    loader's migration writer follows.
    """
    import json as _json
    import os as _os

    tmp = path.with_name(f"{path.name}.doctorfix.{_os.getpid()}")
    tmp.write_text(_json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        _os.chmod(tmp, path.stat().st_mode & 0o7777)
    except OSError:
        pass
    _os.replace(tmp, path)


def _gather_tools(config: "Config") -> ToolsInfo:
    """Every credential-bearing tool and whether this install can use it.

    Reads the capability table rather than re-deriving the rules: three
    families decide registration three different ways, and a fourth opinion
    here is how the answers drift apart. See
    ``raven/agent/tools/capabilities.py``.
    """
    from raven.agent.tools.capabilities import CAPABILITIES, configured_from, is_configured

    return ToolsInfo(
        capabilities=[
            ToolCapabilityInfo(
                tool=cap.tool,
                summary=cap.summary,
                need=cap.need.value,
                configured=is_configured(cap, config),
                source=configured_from(cap, config),
                config_path=cap.config_path,
                env_var=cap.env_var,
                obtain_from=cap.obtain_from,
                cost_note=cap.cost_note,
            )
            for cap in CAPABILITIES
        ]
    )


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

    from raven.channels.manager import missing_dependency_channels

    report.features = FeaturesInfo(
        channels_enabled=enabled,
        channels_missing_deps=missing_dependency_channels(config),
        skill_forge_enabled=skill_forge_on,
    )

    report.tools = _gather_tools(config)

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
    from raven.config.update_everos import everos_owned, everos_role_configured, everos_root
    from raven.plugin.memory.everos._health import (
        DEGRADING_SECTIONS,
        REQUIRED_SECTIONS,
        configured_base_url,
        probe_capabilities,
    )

    # Which memories, and whose. Neither was reachable from any command before:
    # the wizard printed the path once while converging and nothing showed it
    # again, so "where are my memories" had no answer short of reading
    # config.json by hand. This is the place that question gets asked.
    info.owned = everos_owned()
    info.address = configured_base_url(config)
    report = probe_capabilities(configured_base_url(config))
    info.server_running = report.reachable
    info.reports_capabilities = report.reports_capabilities
    info.capabilities = dict(report.capabilities)

    if info.owned:
        info.root = str(everos_root())
        info.configured = [s for s in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS) if everos_role_configured(s)]
        # Recall quality is decided by the embedding role in the user-level
        # everos.toml: with it recall matches meaning, without it only keywords.
        info.retrieval = "semantic" if "embedding" in info.configured else "keyword-only"
        return info

    # A root the user runs. Nothing here may come from the local filesystem:
    # no root is recorded for it, so ``everos_root()`` would answer with the
    # fallback -- a directory that is not theirs and holds none of their
    # memories -- and the roles read out of that directory's toml would
    # describe an install nobody is using. Reading their toml is not an option
    # either; not touching it is the promise. What the server says about itself
    # is the only honest source, and when it is down there is no source at all.
    info.root = None
    # Every section the server has an opinion about -- built or failed. Taking
    # only the built ones made ``unbuilt`` (the failed subset of this list)
    # structurally empty, so ``broken`` and the exit code could never fire and
    # a server that could not build its LLM reported healthy. Raven cannot read
    # their toml to learn what they configured, and does not need to: a section
    # the server reports as unavailable is one it tried to build and could not.
    info.configured = [s for s in (*REQUIRED_SECTIONS, *DEGRADING_SECTIONS) if report.available(s) is not None]
    info.retrieval = None
    if report.reports_capabilities:
        info.retrieval = "semantic" if report.available("embedding") is True else "keyword-only"
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
    if memory.root:
        console.print(f"  Memories:   {memory.root}")
    if not memory.owned:
        console.print(
            "  [dim]Managed by you -- Raven reads it at the address below and never writes,\n"
            "  starts or stops it, so it does not track where on disk it keeps them.[/dim]"
        )
    console.print(f"  Address:    {memory.address}")
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


def _render_tool_capabilities(tools: ToolsInfo) -> None:
    """List every credential-bearing tool, configured or not.

    An unconfigured tool is not registered, so the agent never offers it and no
    other surface says it exists -- this is the only place a deployer can learn
    the capability is available at all. Ordered by how much they would have to
    do, so what is one edit away reads before what needs an account.

    Not a fault: an install with no image generation is a choice, so nothing
    here moves the exit code.
    """
    for cap in tools.capabilities:
        label = f"  {cap.tool + ':':<17}"
        if cap.configured:
            where = f"  [dim]({cap.source})[/dim]" if cap.source else ""
            console.print(f"{label}[green]✓[/green] {cap.summary}{where}")
            continue
        console.print(f"{label}[dim]-  {cap.summary}[/dim]")
        # One fact per line rather than one sentence: the terminal wraps a long
        # line mid-path, and a config key broken across two rows cannot be
        # copied, which is the only thing this row is for.
        indent = f"{'':<19}"
        if cap.need == "own_credential":
            console.print(f"{indent}[dim]switch on:[/dim] {cap.config_path}")
            console.print(f"{indent}[dim]key: borrowed from providers.openrouter[/dim]")
        elif cap.need == "new_account":
            console.print(f"{indent}[dim]set:[/dim] {cap.config_path}")
            if cap.env_var:
                console.print(f"{indent}[dim]or env:[/dim] {cap.env_var}")
            console.print(f"{indent}[dim]key from:[/dim] {cap.obtain_from}")
        if cap.cost_note:
            console.print(f"{indent}[dim]{cap.cost_note}[/dim]")

    if not tools.unconfigured:
        return
    console.print(
        f"  [dim]{len(tools.unconfigured)} capability(s) available but not set up; the agent is not offered them.[/dim]"
    )


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
        if features.channels_missing_deps:
            from raven.channels.manager import _missing_dep_hint

            names = ", ".join(features.channels_missing_deps)
            console.print(f"               [yellow]⚠ SDK missing: {names}[/yellow]  [dim]{_missing_dep_hint()}[/dim]")
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
        elif not memory.owned:
            console.print("  Retrieval:  [dim]unknown  (the server you run is not answering)[/dim]")
        _render_memory_capabilities(memory)

    if report.tools is not None:
        console.print("\n[bold]Tool capabilities[/bold]")
        _render_tool_capabilities(report.tools)

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

    health = report.config_health
    if health and (health.findings or health.applied):
        console.print("\n[bold]Config[/bold]")
        for line in health.findings:
            console.print(
                f"  [yellow]![/yellow] {line}" if not line.startswith("  ") else f"  [dim]{line.strip()}[/dim]"
            )
        for line in health.applied:
            console.print(f"  [green]fixed[/green] {line}")
        if health.fixes:
            console.print("  [dim]Run [cyan]raven doctor --fix[/cyan] to apply:[/dim]")
            for line in health.fixes:
                console.print(f"    [dim]- {line}[/dim]")


def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(
        probe: bool = typer.Option(False, "--probe", help="Send a test message to verify the LLM responds."),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON (CI-friendly)."),
        fix: bool = typer.Option(False, "--fix", help="Apply the config fixes this reports, where one exists."),
        timeout: int = typer.Option(
            15,
            "--timeout",
            help="LLM probe timeout in seconds.",
            min=1,
        ),
    ) -> None:
        """Health-check Raven config, routing, and (optionally) the LLM.

        ``--fix`` is consent, not a mode: without it the config findings are
        reported and nothing is written, because each one is a value somebody
        may have meant.
        """
        report = _gather_static_checks()

        if report.config_loaded:
            from raven.config.loader import load_config
            from raven.config.raven import load_raven_config

            report.memory = _probe_memory(load_raven_config())
            report.config_health = _inspect_config_health(load_config(), fix=fix)

        if probe and report.routing is not None and report.routing.provider is not None:
            report.probe = _run_llm_probe(timeout_s=timeout)

        if json_output:
            console.print_json(json.dumps(asdict(report)))
        else:
            _render_human_output(report)

        raise typer.Exit(report.exit_code())


__all__ = ["register"]
