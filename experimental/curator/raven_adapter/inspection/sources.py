"""Versioned source registration for runtime components and their supporting materials."""

from hashlib import sha256
from inspect import getsourcefile, getsourcelines
from pathlib import Path

from raven.core.runtime import RavenRuntime

from ..targets import catalogue


def source_entry(symbol) -> dict | None:
    try:
        path = Path(getsourcefile(symbol)).resolve()
        lines, start = getsourcelines(symbol)
    except (TypeError, OSError):
        return None
    start = max(1, start)
    package = path.parent
    while (package.parent / "__init__.py").is_file():
        package = package.parent
    if (package.parent / "raven-plugin.toml").is_file():
        package = package.parent
    return {
        "path": str(path),
        "start": start,
        "root": str(package if (path.parent / "__init__.py").is_file() else path),
        "end": start + len(lines) - 1,
        "digest": sha256(path.read_bytes()).hexdigest(),
    }


def native_sources() -> dict[str, dict]:
    from raven.agent.harness import default_harness_modules, participants
    from raven.agent.hook.participant import ParticipantHook
    from raven.agent.loop.main import AgentLoop
    from raven.agent.loop.turn_path import TurnPathMixin
    from raven.agent.tools.registry import ToolRegistry
    from raven.config.mode_catalogue import build_mode_catalogue
    from raven.context_engine.factory import build_context_engine
    from raven.contracts.context import ContextEngine
    from raven.contracts.loop_hooks import AgentHook
    from raven.contracts.memory import MemoryBackend
    from raven.contracts.participant import AgentParticipant, StepView
    from raven.contracts.plugin_surface import ServiceLocator
    from raven.contracts.services import PluginService
    from raven.contracts.session_events import SessionObserver
    from raven.contracts.tool import Tool
    from raven.contracts.tool_gate import ToolGate
    from raven.core.runtime import build_runtime
    from raven.plugins.context import PluginContext
    from raven.plugins.registry import PluginRegistry

    from ..targets import action, capability, memory, planning
    from .mechanisms import STRATEGIES

    symbols = {
        **{f"strategy.{name}": contract for name, contract in STRATEGIES.items()},
        "runtime.assembly": build_runtime,
        "host.mode_catalogue": build_mode_catalogue,
        "host.session_policy": AgentLoop.set_session_policy,
        "loop.structure": AgentLoop,
        "loop.execution": TurnPathMixin,
        "loop.default_strategies": default_harness_modules,
        "loop.participant_timing": ParticipantHook,
        "loop.participant_composition": participants,
        "loop.participant_contract": AgentParticipant,
        "loop.step_view": StepView,
        "loop.tool_execution": ToolRegistry,
        "loop.context_assembly": build_context_engine,
        "host.plugin_registry": PluginRegistry,
        "host.plugin_context": PluginContext,
        "host.service_grants": ServiceLocator,
        "host.context_contract": ContextEngine,
        "host.tool_contract": Tool,
        "host.gate_contract": ToolGate,
        "host.service_contract": PluginService,
        "host.session_observer_contract": SessionObserver,
        "host.backend_contract": MemoryBackend,
        "loop.hook_contract": AgentHook,
        **{
            f"native.strategy.{name}": module.CONTRACT
            for name, module in (
                ("memory", memory),
                ("planning", planning),
                ("capability", capability),
                ("action", action),
            )
        },
        **{target.name: target.contract for target in catalogue()},
    }
    sources = {name: entry for name, symbol in symbols.items() if (entry := source_entry(symbol)) is not None}
    for target in catalogue():
        for index, item in enumerate(target.knowledge):
            entry = file_source(item) if isinstance(item, Path) else source_entry(item)
            if entry:
                sources[f"{target.name}.knowledge.{index}"] = entry
    for path in (Path(__file__).parents[1] / "reference").glob("*.md"):
        sources[f"reference.{path.stem}"] = file_source(path)
    return sources


def file_source(path: Path) -> dict:
    content = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "start": 1,
        "end": max(1, len(content.decode().splitlines())),
        "digest": sha256(content).hexdigest(),
    }


def runtime_sources(runtime: RavenRuntime, package: Path, config=None) -> dict[str, dict]:
    sources = native_sources()
    loop = runtime.loop
    symbols = {"instance.context_engine": type(loop.context_engine)}
    symbols.update(
        {
            f"instance.{name}": type(getattr(loop.harness, name))
            for name in ("memory", "planning", "capability", "action")
        }
    )
    for name in loop.tools.tool_names:
        symbols[f"tool.{name}"] = type(loop.tools.get(name))
    if runtime.backend is not None:
        symbols["instance.memory_backend"] = type(runtime.backend)
    for index, hook in enumerate(loop.hooks):
        symbols[f"hook.{index}"] = type(hook)
        factory = getattr(hook, "factory", None)
        if factory is not None:
            symbols[f"hook.{index}.factory"] = factory
    for name, symbol in symbols.items():
        entry = source_entry(symbol)
        if entry:
            sources[name] = entry
    from raven.core.plugin_stack import discover_plugins

    registry = runtime.plugin_registry
    for plugin in discover_plugins(config):
        active = registry.manifest_for(plugin.manifest.id)
        if active != plugin.manifest or plugin.location is None:
            continue
        sources[f"plugin.{plugin.manifest.id}.manifest"] = {
            **file_source(plugin.location),
            "root": str(plugin.location.parent.resolve()),
        }
    for kind in ("tool", "hook", "tool_gate", "session_observer", "memory_backend", "onboard"):
        names = getattr(registry, f"{kind}_names")()
        for name in names:
            factory = getattr(registry, f"get_{kind}_factory")(name)
            entry = source_entry(factory)
            if entry:
                for identifier in registry.activated_ids():
                    manifest = registry.manifest_for(identifier)
                    field = f"{kind}s" if kind != "onboard" else "onboard"
                    contributions = getattr(manifest.contributes, field, ())
                    if any(item.name == name for item in contributions):
                        sources[f"plugin.{identifier}.{kind}.{name}"] = entry
    for path in package.rglob("*.py"):
        if "__pycache__" not in path.parts:
            sources[f"generated.{path.relative_to(package).as_posix()}"] = {**file_source(path), "root": str(package)}
    return sources
