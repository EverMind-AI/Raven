"""Assemble generated changes through Raven's native configuration and plugin sockets."""

import json
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from inspect import Parameter, iscoroutinefunction, signature
from pathlib import Path, PurePosixPath
from typing import Any

from raven.agent.hook.participant import ParticipantHook
from raven.agent.loop.bundles import HostWiring, TurnPolicy
from raven.agent.loop.recovery import limits_from_defaults
from raven.agent.tools.registry import admit_tool
from raven.agent.workdir import WorkdirPolicy, WorkdirResolver
from raven.contracts.loop_hooks import AgentHook
from raven.contracts.services import PluginService
from raven.contracts.session_events import SessionObserver
from raven.contracts.tool_gate import ToolGate
from raven.core.runtime import RavenRuntime, build_runtime
from raven.home import set_config_path
from raven.plugins.context import PluginContext, ServiceLocator
from raven.providers.factory import make_lazy_provider
from raven.providers.pool import ProviderPool
from raven.session.manager import SessionManager

from ..harness import Artifact, Declaration
from ..harness.artifact import relative_path
from .action.runtime import BoundAction
from .capability.runtime import BoundCapability
from .inference import Inference, supplied
from .inspection import Baseline, runtime_sources
from .inspection.runtime import playbook_library
from .materialize import (
    PLUGIN_ID,
    install_content,
    load_factory,
    native_settings,
    write_package,
)
from .memory.context import MemoryContext
from .memory.runtime import BoundMemory
from .observe import LoopObserver, ObservedPool, ObservedProvider, Recorder, build_participant, participant_factory
from .planning.contracts import TARGET, TOOL_NAME, PlanningBinding
from .planning.runtime import BoundPlanning
from .prompts import bind_prompts

_ASSEMBLY: ContextVar[Any] = ContextVar("curator_assembly", default=None)


def _no_plan():
    return None


_COMPONENT_PROTOCOLS = {
    "services": PluginService,
    "session_observers": SessionObserver,
    "tool_gates": ToolGate,
}


def conform(value, protocol):
    """Check each protocol method the way the host calls it: async where the host awaits, and the same arguments."""
    for method, contract in vars(protocol).items():
        if method.startswith("_") or not callable(contract):
            continue
        implementation = getattr(value, method, None)
        if not callable(implementation):
            raise TypeError(f"{protocol.__name__} lacks {method}")
        if iscoroutinefunction(contract) and not iscoroutinefunction(implementation):
            raise TypeError(f"{protocol.__name__}.{method} must be async; the host awaits it")
        parameters = list(signature(contract).parameters.values())[1:]
        positional = [
            object() for p in parameters if p.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]
        keywords = {p.name: None for p in parameters if p.kind is Parameter.KEYWORD_ONLY}
        try:
            signature(implementation).bind(*positional, **keywords)
        except TypeError as exc:
            raise TypeError(
                f"{protocol.__name__}.{method} does not accept the host's call {method}{signature(contract)}: {exc}"
            ) from exc


def construct_component(kind, name, reference, context):
    """Called by native plugin factories, retaining failures the native host may silence."""
    package, objects, recorder, dependencies = _ASSEMBLY.get()
    recorder.add("component.call", kind_name=kind, name=name, factory=reference)
    try:
        factory = load_factory(reference, package)
        value = factory(context, **supplied(factory, dependencies))
        if value is None:
            raise TypeError("the requested factory declined construction")
        if kind == "tools":
            admit_tool(value)
        elif kind == "hooks":
            for method, contract in vars(AgentHook).items():
                if iscoroutinefunction(contract) and not callable(getattr(value, method, None)):
                    raise TypeError(f"hook lacks {method}")
            if not isinstance(getattr(value, "name", None), str):
                raise TypeError("hook lacks a native name")
        elif kind == "memory_backends":
            # recall_session, delete and health are optional on Raven's memory path.
            for method in ("recall", "store", "feedback", "start", "stop"):
                if not callable(getattr(value, method, None)):
                    raise TypeError(f"memory backend lacks {method}")
        else:
            conform(value, _COMPONENT_PROTOCOLS[kind])
        objects[kind, name] = value
        recorder.add(
            "component.constructed",
            kind_name=kind,
            name=name,
            factory=reference,
            implementation=f"{type(value).__module__}:{type(value).__qualname__}",
            runtime_name=getattr(value, "name", None),
        )
        return value
    except Exception as exc:
        recorder.add("component.error", kind_name=kind, name=name, error=f"{type(exc).__name__}: {exc}")
        raise


def _plugin(root, artifact, declaration, package):
    rows = []
    for target_name, entries in artifact.values.items():
        binding = declaration.target(target_name).binding
        if binding.startswith("plugin.contributes."):
            rows.extend((binding.rsplit(".", 1)[-1], entry) for entry in entries)
    if not rows:
        return None
    wrapper_name = f"_bindings_{package.name}"
    wrapper = ["from experimental.curator.raven_adapter.bind import construct_component"]
    manifest = ["[plugin]", f"id = {json.dumps(PLUGIN_ID)}", 'version = "0.1.0"']
    for i, (kind, entry) in enumerate(rows):
        wrapper.extend(
            [
                f"def build_{i}(context):",
                f"    return construct_component({kind!r}, {entry['name']!r}, {entry['factory']!r}, context)",
            ]
        )
        manifest.extend(
            [
                f"[[plugin.contributes.{kind}]]",
                f"name = {json.dumps(entry['name'])}",
                f"factory = {json.dumps(f'{wrapper_name}:build_{i}')}",
            ]
        )
    (root / f"{wrapper_name}.py").write_text("\n".join(wrapper) + "\n")
    plugin = root / "plugins" / PLUGIN_ID
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "raven-plugin.toml").write_text("\n".join(manifest) + "\n")
    return plugin.parent


@dataclass
class Bound:
    runtime: RavenRuntime
    baseline: Baseline
    artifact: Artifact
    recorder: Recorder
    observer: LoopObserver
    components: dict
    sources: dict
    planning: BoundPlanning | None = None
    strategies: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)

    async def prepare(self):
        if self.planning:
            await self.planning.prepare()
        for strategy in self.strategies.values():
            await strategy.prepare()

    async def start(self):
        await self.runtime.loop._connect_mcp()
        manager = self.runtime.loop.mcp_manager_if_started
        states = manager.status() if manager is not None else []
        self.recorder.add("mcp.status", servers=states)
        requested = self.artifact.values.get("capability.mcp", {})
        for name in requested:
            if self.baseline.config.tools.mcp_servers[name].enabled:
                state = next((row for row in states if row["name"] == name), None)
                if state is None or not state["connected"]:
                    raise RuntimeError(f"MCP server did not connect: {name}: {state}")
        if self.runtime.backend is not None:
            await self.runtime.backend.start()
            self.recorder.add("backend.started", implementation=type(self.runtime.backend).__name__)
        if self.baseline.resident:
            await self.runtime.loop.start_plugin_services()
            started = self.runtime.loop._started_services
            failed = [
                service
                for service in self.runtime.loop.plugin_services
                if not any(service is active for active in started)
            ]
            for service in failed:
                try:
                    await service.stop()
                except Exception as exc:
                    self.recorder.add("service.error", error=f"failed-start cleanup: {exc}")
            for (kind, name), value in self.components.items():
                if kind == "services" and any(value is service for service in failed):
                    raise RuntimeError(f"service did not start: {name}")
            self.recorder.add(
                "services.started", count=len(started), observers=len(self.runtime.loop.session_observers)
            )

    async def close(self):
        await self.runtime.dispose()
        self.recorder.add("runtime.closed")


def assemble(
    baseline: Baseline,
    artifact: Artifact,
    declaration: Declaration,
    root: Path,
    recorder: Recorder,
    provider_factory=None,
    planning_state: Path | None = None,
) -> Bound:
    """Construct a generation; callers own process isolation and start/stop."""
    for name in artifact.values:
        binding = declaration.target(name).binding
        if not (
            binding.startswith(("config.", "raven_config.", "plugin.contributes."))
            or binding
            in {
                "bootstrap_files",
                "skill_files",
                "playbook_files",
                "HostWiring.hooks",
                "build_runtime.context_engine",
                TARGET,
                "memory.strategy",
                "capability.strategy",
                "action.strategy",
                "prompt.resources",
            }
        ):
            raise ValueError(f"native binding is not implemented: {binding}")
    root.mkdir(parents=True, exist_ok=True)
    package = write_package(root, artifact)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    prompts = bind_prompts(artifact, package)
    effective = native_settings(baseline, artifact, declaration)
    if effective.config.workspace_path != baseline.config.workspace_path:
        raise ValueError("the agent home is a host resource; a Harness change must preserve current task storage")
    for server in effective.config.tools.mcp_servers.values():
        if server.command in artifact.files:
            server.command = str(package / server.command)
        server.args = [str(package / argument) if argument in artifact.files else argument for argument in server.args]
    plugin_root = _plugin(root, artifact, declaration, package)
    if plugin_root is not None:
        effective.extensions.plugins.dirs = [*effective.extensions.plugins.dirs, str(plugin_root)]
    # Native live readers and construction consume the same two configuration trees.
    effective.extensions.base = effective.config

    provider = ObservedProvider((provider_factory or make_lazy_provider)(effective.config), recorder)
    router = None
    if baseline.hosting == "acp":
        from raven.core.provider_stack import build_model_routing

        router, provider = build_model_routing(effective.config, provider)
    infer = Inference(provider, recorder)
    groups = {}
    for name, reference in artifact.values.items():
        target = declaration.target(name)
        if target.binding == "HostWiring.hooks":
            groups.setdefault(reference, []).append(target)
    planning = None
    if TARGET in artifact.values:
        if baseline.task is None:
            raise ValueError("a planning strategy requires a host task binding")
        planning = BoundPlanning(
            PlanningBinding.model_validate(artifact.values[TARGET]),
            baseline.task,
            planning_state or root / "planning.json",
            package,
            recorder,
            infer=infer,
        )
    dependencies = {"plan": planning.read if planning else _no_plan}
    observer = LoopObserver(recorder)
    hooks = [observer]
    for index, (reference, targets) in enumerate(groups.items()):
        loaded = load_factory(reference, package)
        # Construct once here so a wrong entry point fails the check instead of leaving a seat absent at turn time.
        build_participant(loaded, targets, dependencies)
        factory = participant_factory(loaded, targets, recorder, dependencies)
        hooks.append(
            ParticipantHook(
                f"curator-participant-{index}",
                factory,
                rolls_back=any(target.contract.__name__ == "review" for target in targets),
            )
        )

    if planning and (planning.render or planning.observe):
        hooks.append(planning.hook())

    strategies = {}
    for name, implementation in (("memory", BoundMemory), ("capability", BoundCapability), ("action", BoundAction)):
        target_name = f"{name}.strategy"
        if target_name not in artifact.values:
            continue
        if baseline.task is None:
            raise ValueError(f"a {name} strategy requires a host task binding")
        strategy = implementation(
            declaration.target(target_name).parse(artifact.values[target_name]),
            baseline.task,
            (planning_state.parent if planning_state else root) / f"{name}.json",
            package,
            recorder,
            infer=infer,
            plan=dependencies["plan"],
        )
        strategies[name] = strategy
        hooks.append(strategy.hook())
    capability = strategies.get("capability")
    if capability:
        capability.stage_skills(root, effective.extensions)

    raw = {
        **effective.config.model_dump(mode="json", by_alias=True),
        **effective.extensions.model_dump(mode="json", by_alias=True, exclude={"base"}),
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(raw, ensure_ascii=False))
    config_path.chmod(0o600)
    set_config_path(config_path)

    context_engine = None
    context_target = next(
        (
            target
            for target in declaration.targets
            if target.binding == "build_runtime.context_engine" and target.name in artifact.values
        ),
        None,
    )
    if context_target:
        context = PluginContext(
            config=effective.extensions.context.model_dump(),
            services=ServiceLocator(
                workspace=effective.config.workspace_path,
                user_id=effective.extensions.memory.user_id,
                agent_id=effective.extensions.memory.agent_id,
                provider=provider,
            ),
        )
        context_engine = load_factory(artifact.values[context_target.name], package)(context)
        for method in ("assemble", "after_turn", "set_provider"):
            if not callable(getattr(context_engine, method, None)):
                raise TypeError(f"context engine lacks {method}")
        if not hasattr(context_engine, "name") or not hasattr(context_engine, "owns_compaction"):
            raise TypeError("context engine lacks its native identity or compaction declaration")

    memory = strategies.get("memory")
    memory_context = MemoryContext(memory, context_engine) if memory and memory.config.composition else None
    if memory_context is not None:
        context_engine = memory_context

    if baseline.hosting == "acp":
        from raven.core.engine_stack import build_local_sessions

        from .hosting.lifecycle import local_host

        sessions, workdir = build_local_sessions(effective.config, workspace=None)
        host = local_host(effective.config)
        host.hooks = [*(host.hooks or ()), *hooks]
    else:
        sessions = SessionManager(effective.config.workspace_path)
        workdir = WorkdirResolver(
            WorkdirPolicy.LAUNCH_DIR,
            agent_home=effective.config.workspace_path,
            launch_dir=effective.workdir,
            sessions=sessions,
        )
        host = HostWiring(hooks=hooks)
    objects = {}
    token = _ASSEMBLY.set((package, objects, recorder, dependencies))
    runtime = None
    try:
        with install_content(effective.config.workspace_path, artifact, declaration):
            runtime = build_runtime(
                effective.config,
                effective.extensions,
                provider=provider,
                router=router,
                session_manager=sessions,
                provider_pool=ObservedPool(ProviderPool(effective.config), recorder),
                workdir_resolver=workdir,
                context_engine=context_engine,
                policy=TurnPolicy(
                    max_iterations=effective.config.agents.defaults.max_tool_iterations,
                    empty_recovery=limits_from_defaults(effective.config.agents.defaults),
                    interactive=baseline.hosting == "acp",
                ),
                host=host,
            )
            if baseline.hosting == "acp":
                from raven.proactive_engine.schedulers.cron.tool import CronTool

                cron_tool = runtime.loop.tools.get("cron")
                if isinstance(cron_tool, CronTool):
                    cron_tool.set_context("acp", "default")
            if memory_context is not None:
                memory_context.bind(runtime, effective.extensions)
            if planning and planning.to_change:
                if runtime.loop.tools.get(TOOL_NAME) is not None:
                    raise ValueError(f"planning tool would replace an existing tool: {TOOL_NAME}")
                runtime.loop.tools.register(planning.tool())
            if capability:
                capability.install(runtime)
            if not baseline.allow_delegation:
                from .capability.leaf import bind_leaf

                bind_leaf(runtime)
            _verify_bindings(runtime, artifact, declaration, objects, effective.extensions.memory.backend)
            _verify_skills(runtime, artifact, effective.config.workspace_path)
            _verify_playbooks(runtime, artifact)
        recorder.add("runtime.bound", package=str(package), targets=list(artifact.values))
        return Bound(
            runtime,
            effective,
            artifact,
            recorder,
            observer,
            objects,
            runtime_sources(runtime, package, effective.extensions),
            planning,
            strategies,
            prompts,
        )
    except BaseException:
        if runtime is not None:
            runtime.loop.context.skills.stop_file_watcher()
        raise
    finally:
        _ASSEMBLY.reset(token)


def _verify_bindings(runtime, artifact, declaration, objects, backend_name):
    registry, loop = runtime.plugin_registry, runtime.loop
    for target_name, entries in artifact.values.items():
        binding = declaration.target(target_name).binding
        if not binding.startswith("plugin.contributes."):
            continue
        kind = binding.rsplit(".", 1)[-1]
        registered = getattr(
            registry,
            {
                "tools": "tool_names",
                "hooks": "hook_names",
                "services": "service_names",
                "memory_backends": "memory_backend_names",
                "tool_gates": "tool_gate_names",
                "session_observers": "session_observer_names",
            }[kind],
        )()
        for entry in entries:
            name = entry["name"]
            if name not in registered:
                raise ValueError(f"{target_name}: contribution was not registered: {name}")
            if kind == "memory_backends":
                if name == backend_name and (
                    runtime.backend is None or objects.get((kind, name)) is not runtime.backend
                ):
                    raise ValueError(f"selected memory backend was not bound: {name}")
                continue
            if (kind, name) not in objects:
                raise ValueError(f"{target_name}: requested component was not constructed: {name}")
            value = objects[kind, name]
            actual = {
                "tools": lambda: loop.tools.get(value.name) is value,
                "hooks": lambda: any(value is item for item in loop.hooks),
                "services": lambda: any(value is item for item in loop.plugin_services),
                "tool_gates": lambda: any(value is item for item in loop.tools.tool_gates),
                "session_observers": lambda: any(value is item for item in loop.session_observers),
            }[kind]()
            if not actual:
                raise ValueError(f"{target_name}: component was not bound: {name}")
    if "memory.backend_config" in artifact.values and runtime.backend is None:
        if artifact.values["memory.backend_config"].get("backend") is not None:
            raise ValueError("selected memory backend was not constructed")


def _verify_playbooks(runtime, artifact):
    files = artifact.values.get("planning.playbooks", {})
    if not files:
        return
    library = playbook_library(runtime.loop)
    loaded = set(library.names()) if library is not None else set()
    for name in files:
        parts = PurePosixPath(relative_path(name)).parts
        from ..composition.requirements import read_requirements, requirement_node

        if requirement_node(name):
            read_requirements(files[name])
            if (
                library is None
                or parts[0] not in loaded
                or parts[2] not in {node.id for node in library.store.load(parts[0]).nodes}
            ):
                raise ValueError(f"requirements refer to a missing native playbook node: {name}")
        elif len(parts) != 2 or parts[1] != "playbook.md":
            raise ValueError(f"a playbook file must be <name>/playbook.md or a node requirements.json: {name}")
        if parts[0] not in loaded:
            raise ValueError(
                f"playbook was not loaded; check its frontmatter, its yaml playbook-spec block and the node agents: {name}"
            )


def _verify_skills(runtime, artifact, home):
    files = artifact.values.get("planning.skills", {})
    if not files:
        return
    roots = [
        Path(row["path"]).parent.resolve() for row in runtime.loop.context.skills.list_skills(filter_unavailable=False)
    ]
    for name in files:
        path = (home / "skills" / name).resolve()
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError(f"skill resource has no discoverable SKILL.md package: {name}")
