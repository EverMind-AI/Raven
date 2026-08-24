"""CLI assembly helper for the plugin / memory-backend stack.

Two functions that bridge the gap between RavenConfig (user-facing
settings under ``plugins`` / ``memory``) and the runtime objects
AgentLoop expects (a ready-to-use :class:`MemoryBackend` instance):

- :func:`build_plugin_registry` — discover all installed plugins
  (bundled + user-level + project-level + pip entry points), filter
  by ``config.plugins.disabled``, return an activated registry.
- :func:`maybe_build_memory_backend` — resolve ``config.memory.backend``
  to a concrete :class:`MemoryBackend` instance via the registry, or
  return ``None`` when no backend is selected / the requested
  contribution isn't available.

Both functions are intentionally lenient: a missing
plugin / activation error logs a warning and falls through to ``None``
rather than crashing the host. The legacy ``self.memory`` pipeline in
AgentLoop is unaffected — it always works regardless of whether a
plugin backend is wired.

Lifecycle (``backend.start()`` / ``backend.stop()``) is the **caller's**
responsibility. These helpers only construct; CLI bootstrap code does
the await around them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from raven.plugin import (
    PluginConflictError,
    PluginFactoryImportError,
    PluginNotFoundError,
    PluginRegistry,
    ServiceLocator,
    assemble_plugin_registry,
)

if TYPE_CHECKING:
    from raven.config.raven import RavenConfig
    from raven.memory_engine import MemoryBackend

logger = logging.getLogger(__name__)


def plugin_discovery_sources() -> dict:
    """Resolve the four discovery-source locations the host scans.

    Shared by :func:`build_plugin_registry` (live boot) and the
    ``raven plugins`` CLI command so both see the same set:

    - bundled — ``raven/plugin/memory/`` inside the package.
    - user    — ``~/.raven/plugin/``.
    - project — ``./.raven/plugin/``.
    - entry_points — the ``raven.plugins`` group.
    """
    import raven

    return {
        "bundled_dir": Path(raven.__path__[0]) / "plugin" / "memory",
        "user_dir": Path.home() / ".raven" / "plugins",
        "project_dir": Path.cwd() / ".raven" / "plugins",
        "entry_points_group": "raven.plugins",
    }


def build_plugin_registry(
    config: "RavenConfig",
) -> PluginRegistry:
    """Discover + activate every installed plugin admitted by ``config``.

    Reads ``config.plugins.disabled`` and forwards it to
    :func:`assemble_plugin_registry`. Activation errors
    (:class:`PluginConflictError`, :class:`PluginFactoryImportError`) are
    caught and logged — the caller receives an **empty** registry so
    AgentLoop can still boot and fall back to the legacy path.

    Discovery spans four sources (priority bundled > user > project >
    entry_points):

    - **bundled** — ``raven/plugin/memory/<id>/`` shipped inside the
      raven package (the EverOS backend lives here).
    - **user** — ``~/.raven/plugin/<id>/`` drop-in directories.
    - **project** — ``./.raven/plugin/<id>/`` drop-in directories.
    - **entry_points** — the ``raven.plugins`` group, where
      third-party pip-installed plugins register their factories.
    """
    disabled = frozenset(config.plugins.disabled)
    try:
        return assemble_plugin_registry(
            **plugin_discovery_sources(),
            disabled=disabled,
        )
    except (PluginConflictError, PluginFactoryImportError) as e:
        logger.warning(
            "plugin activation failed (%s); continuing without plugins. AgentLoop will use its legacy memory path.",
            e,
        )
        return PluginRegistry()


def maybe_build_memory_backend(
    workspace: Path,
    config: "RavenConfig",
    *,
    registry: PluginRegistry | None = None,
) -> "MemoryBackend | None":
    """Construct the configured memory backend, if any.

    Resolution order:

    1. If ``config.memory.backend`` is ``None``, return ``None``
       immediately — user explicitly disabled the plugin path.
    2. Look up the backend factory in the (possibly host-supplied)
       :class:`PluginRegistry`. If absent (e.g. the everos substrate
       wasn't installed), log a warning and return ``None``.
    3. Resolve the per-plugin config slice from
       ``config.plugins.config`` — first by plugin id (the canonical
       key, e.g. ``"everos-memory"``), then by backend contribution
       name (the friendlier key, e.g. ``"everos"``) as a fallback.

    The returned backend has **not** been ``await``-started — the
    caller (CLI bootstrap) is responsible for the
    ``await backend.start()`` / ``await backend.stop()`` lifecycle so
    those awaits sit in the right async context.
    """
    name = config.memory.backend
    if name is None:
        return None
    if registry is None:
        registry = build_plugin_registry(config)
    plugin_slice = _resolve_plugin_config_slice(registry, config, name)
    # Both checks are diagnostics only: they run before construction and never
    # change what the factory receives. A backend whose config is wrong still
    # builds, because refusing to boot over a config warning is a worse
    # failure than the one being reported.
    plugin_id = _plugin_id_for_backend(registry, name)
    manifest = registry.manifest_for(plugin_id) if plugin_id is not None else None
    if manifest is not None:
        for problem in validate_plugin_config_slice(
            plugin_slice,
            manifest.config_schema,
            label=f"plugins.config.{plugin_id}",
        ):
            logger.warning(problem)
    for problem in warn_on_memory_identity_mismatch(config, plugin_slice):
        logger.warning(problem)
    services = ServiceLocator(workspace=workspace)
    try:
        backend = registry.build_memory_backend(
            name,
            config=plugin_slice,
            services=services,
        )
    except PluginNotFoundError:
        logger.warning(
            "memory.backend=%r requested but no plugin contributes it. "
            "The everos backend ships bundled with raven — run `uv sync` "
            "to install its substrate. Continuing without a plugin backend.",
            name,
        )
        return None
    except Exception as e:
        # Factory raised during construction — log + degrade rather
        # than fail the host boot. CLEANUP will tighten this once the
        # plugin path is the canonical one and a failure is fatal.
        logger.warning(
            "memory backend %r factory raised at construction (%s); continuing without backend.",
            name,
            e,
        )
        return None
    return backend


def build_plugin_tools(
    workspace: Path,
    config: "RavenConfig",
    *,
    registry: PluginRegistry | None = None,
) -> list:
    """Construct every plugin-contributed tool admitted by ``config``.

    Mirrors :func:`maybe_build_memory_backend` but for the ``tools``
    contribution point: walks the activated registry's tool names,
    resolves each owning plugin's config slice, and builds the tool via
    :meth:`PluginRegistry.build_tool`. Lenient by design — a single
    tool's construction failure is logged and skipped so one bad plugin
    can't keep the agent from booting. A factory may also return ``None``
    to deliberately decline contribution (e.g. an optional dependency is
    absent); that's skipped quietly, not treated as a failure. The host
    registers the returned tools into the agent's :class:`ToolRegistry`.

    Returns an empty list when no plugin contributes a tool.
    """
    if registry is None:
        registry = build_plugin_registry(config)
    names = registry.tool_names()
    if not names:
        return []
    services = ServiceLocator(workspace=workspace)
    slices = config.plugins.config
    tools = []
    for name in names:
        plugin_id = registry.tool_plugin_id(name)
        plugin_slice = (plugin_id and slices.get(plugin_id)) or slices.get(name) or {}
        try:
            tool = registry.build_tool(
                name,
                config=plugin_slice,
                services=services,
            )
        except Exception as e:
            logger.warning(
                "plugin tool %r factory raised at construction (%s); skipping it.",
                name,
                e,
            )
            continue
        # A factory may return None to decline contribution at runtime
        # (e.g. an optional dependency isn't installed). That's a clean
        # opt-out, not a failure — skip it without the warning.
        if tool is None:
            logger.debug(
                "plugin tool %r factory opted out (returned None); skipping it.",
                name,
            )
            continue
        tools.append(tool)
    return tools


def _resolve_plugin_config_slice(
    registry: PluginRegistry,
    config: "RavenConfig",
    backend_name: str,
) -> dict:
    """Pick the right ``config.plugins.config[...]`` entry for a backend.

    Tries two keys, in order:

    1. The **plugin id** that contributes ``backend_name`` (canonical,
       e.g. ``"everos-memory"`` — comes from the manifest's
       ``[plugin] id`` field).
    2. The **backend contribution name** itself
       (e.g. ``"everos"`` — friendlier for handwritten config files).

    Returns an empty dict when neither key is present, so the plugin
    factory receives a deterministic shape and applies its own
    defaults.
    """
    slices = config.plugins.config
    plugin_id = _plugin_id_for_backend(registry, backend_name)
    if plugin_id is not None and plugin_id in slices:
        return slices[plugin_id]
    if backend_name in slices:
        return slices[backend_name]
    return {}


_SCHEMA_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "boolean": (bool,),
    # bool is an int subclass, so an accidental ``true`` would satisfy a
    # numeric declaration; it is excluded at the check instead of here.
    "integer": (int,),
    "number": (int, float),
    "array": (list,),
    "object": (dict,),
}


def validate_plugin_config_slice(
    slice_: dict,
    schema: dict,
    *,
    label: str,
) -> list[str]:
    """Check a plugin's config slice against its manifest ``config_schema``.

    Returns human-readable problems; an empty list means nothing to say. The
    caller logs them. Nothing raises: a wrong key is worth a warning at boot,
    not a dead host, and a batch must not die mid-run over a stray field.

    This exists because the slice is passed to the factory verbatim. Every key
    a plugin does not read is silently inert, so a typo (``deffer_extraction``)
    reads as "the setting had no effect" and the config still looks right. An
    empty ``schema`` declares nothing and therefore validates nothing, which is
    how a plugin opts out.
    """
    if not schema:
        return []
    problems: list[str] = []
    for key, value in sorted(slice_.items()):
        declared = schema.get(key)
        if declared is None:
            near = _closest_key(key, schema)
            hint = f"; did you mean {near!r}?" if near else ""
            problems.append(
                f"{label}: {key!r} is not declared in the plugin's "
                f"config_schema and is ignored{hint}",
            )
            continue
        if not isinstance(declared, dict):
            continue
        expected = declared.get("type")
        allowed = _SCHEMA_TYPES.get(expected) if isinstance(expected, str) else None
        if allowed is None or value is None:
            continue
        if isinstance(value, bool) and expected in ("integer", "number"):
            problems.append(
                f"{label}: {key!r} is declared {expected} but got a boolean",
            )
            continue
        if not isinstance(value, allowed):
            problems.append(
                f"{label}: {key!r} is declared {expected} but got "
                f"{type(value).__name__}",
            )
    return problems


def _closest_key(key: str, schema: dict) -> str | None:
    """Nearest declared key, for the "did you mean" hint. ``None`` when nothing
    is close enough to be worth guessing at."""
    import difflib

    matches = difflib.get_close_matches(key, list(schema), n=1, cutoff=0.8)
    return matches[0] if matches else None


def warn_on_memory_identity_mismatch(config: "RavenConfig", slice_: dict) -> list[str]:
    """Compare the host's recall identities against the plugin's write ones.

    ``memory.userId`` / ``memory.agentId`` are what the host passes to
    ``backend.recall``; the plugin slice's ``user_id`` / ``agent_id`` are
    stamped as the sender on everything it writes. EverOS routes by sender, so
    a mismatch writes under one owner and reads under another: recall comes
    back permanently empty with no error on either side. Both sides default to
    ``"default"``, which is why setting neither works and setting one does not.

    Only reported when recall is actually on. In the store-only profile
    (``recall_enabled: false``) nothing reads, so a difference costs nothing
    today -- warning there would fire on every measurement arm, which is how a
    real warning gets tuned out.
    """
    if not slice_.get("recall_enabled", True):
        return []
    problems: list[str] = []
    for host_value, plugin_key, host_key, lane in (
        (config.memory.user_id, "user_id", "memory.userId", "recall_memory_enabled"),
        (config.memory.agent_id, "agent_id", "memory.agentId", "recall_skills_enabled"),
    ):
        if not slice_.get(lane, True):
            continue
        plugin_value = slice_.get(plugin_key)
        if plugin_value is not None and plugin_value != host_value:
            problems.append(
                f"{host_key}={host_value!r} does not match the plugin's "
                f"{plugin_key}={plugin_value!r}: writes are stamped with the "
                f"plugin id and recall asks for the host one, so this lane "
                f"returns nothing. Set both to the same value.",
            )
    return problems


def _plugin_id_for_backend(
    registry: PluginRegistry,
    backend_name: str,
) -> str | None:
    """Reverse-lookup the plugin id that contributes ``backend_name``.

    Returns ``None`` when no activated plugin contributes the named
    backend — the caller (config resolver) treats that as "fall
    through to the contribution-name key".
    """
    for plugin_id in registry.activated_ids():
        mf = registry.manifest_for(plugin_id)
        if mf is None:
            continue
        for contribution in mf.contributes.memory_backends:
            if contribution.name == backend_name:
                return plugin_id
    return None


async def shutdown_memory_backend(
    agent_loop,
    backend,
    *,
    promote: bool,
    log: logging.Logger | None = None,
    surface: str = "cli",
) -> None:
    """Drain, optionally promote, then stop — the teardown ordering contract.

    Three steps that must happen in this order, and each of which must not be
    able to skip the next:

    1. **drain** — the after-turn store is detached under a turn budget, so
       exiting on top of it loses exactly the turns that were slowest to index.
    2. **promote** — only when ``memory.flush_on_task_end`` is on. After the
       drain, because a deferred-capture backend learns each session's
       backend-side id inside ``store``.
    3. **stop** — releases the HTTP pool / embedded index lock. It must run
       even if 1 or 2 raised, or the next process cannot start.

    Hence one ``try`` per step rather than one around all three. Shared by the
    surfaces whose only task boundary is exit (TUI, gateway); ``raven agent``
    keeps its own path because it promotes the single session it wrote rather
    than every session the process captured.
    """
    if backend is None:
        return
    log = log or logger
    try:
        await agent_loop.drain_backend_stores()
    except Exception:
        log.exception("%s: backend store drain failed; continuing shutdown", surface)
    if promote:
        try:
            await agent_loop.promote_all_backend_sessions()
        except Exception:
            log.exception("%s: backend promotion failed; continuing shutdown", surface)
    try:
        await backend.stop()
    except Exception:
        log.exception("%s: memory backend stop failed; continuing shutdown", surface)


__all__ = [
    "build_plugin_registry",
    "build_plugin_tools",
    "maybe_build_memory_backend",
    "shutdown_memory_backend",
    "validate_plugin_config_slice",
    "warn_on_memory_identity_mismatch",
]
