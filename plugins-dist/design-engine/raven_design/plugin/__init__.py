"""Factories the manifest names: the turn-frame hook and three design tools.

The registry calls one factory per contribution, each with its own
``PluginContext``, so the tools and the hook cannot be handed to each other
directly. A per-activation :class:`_Shared` bridges them (the sibling product
plugins' shape): whichever factory runs first parses the slice once, builds
the selector once (fail-closed over the packaged corpus), and caches one
:class:`~raven_design.rendering.service.RenderService` per working directory
-- the pooled loop serves many sessions, and a render service is bound to one
workspace by its path policy, so the seat is per-workdir (the ppt engine's
SessionTool insight).

Admission is the D6 shape: an absent or false ``enabled`` in
``plugins.config["design-engine"]`` means no surface is cast at all. The
render tools additionally decline when the ``render`` extra is not installed
(playwright / pymupdf / pypdfium2 -- the fork assembly's returns-[] contract,
said as plugin admission), and the task-state surfaces decline without a
``taskState.stateRoot`` (state written to a guessed path is state a reinstall
silently abandons). A slice that does not PARSE is neither on nor off: the
parse error is caught here, every tool factory declines, and ``make_hook``
casts the fail-closed :class:`~raven_design.plugin.hook.MisconfiguredEngineHook`
-- every turn answers with the config fix named until someone repairs the
slice (the code-flow MisconfiguredGate doctrine, w101).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.contracts.tool import Tool
from raven_design.plugin.config import EngineConfig
from raven_design.plugin.hook import DesignEngineHook, MisconfiguredEngineHook, build_selector

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext
    from raven_design.rendering.service import RenderService
    from raven_design.task_state.manager import TaskStateManager

logger = logging.getLogger(__name__)

#: The render extra's import faces; absent, the render tools decline.
_RENDER_EXTRA_MODULES = ("playwright", "fitz", "pypdfium2")


def _render_extra_missing() -> list[str]:
    return [name for name in _RENDER_EXTRA_MODULES if importlib.util.find_spec(name) is None]


class _Shared:
    """One activation's shared state: the slice, the selector, the caches.

    Keyed by the granted workspace (the ppt engine's shape) and holding the
    context it was built from: the registry hands every factory a fresh
    admitted copy of the slice, so an object-identity key would neither share
    (four factories, four parses, two competing render-service caches) nor
    stay safe (a freed dict's id can be reused by a later activation and
    serve it a stale generation). One activation per workspace shares one
    parse, one selector, one per-workdir service cache.
    """

    _instances: dict[str, "_Shared"] = {}

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self.slice_error: str | None = None
        self.cfg: EngineConfig | None = None
        self.selector = None
        self.manager: "TaskStateManager | None" = None
        self._services: dict[str, "RenderService"] = {}
        try:
            self.cfg = EngineConfig.from_slice(dict(ctx.config or {}))
        except ValueError as exc:
            self.slice_error = str(exc)
            logger.error("design-engine: %s", exc)
            return
        if not self.cfg.enabled:
            return
        if self.cfg.selector.enabled:
            try:
                self.selector = build_selector(self.cfg)
            except ValueError as exc:
                # A wheel whose packaged corpus cannot back the fixed catalog
                # is misconfigured freight, not a degraded turn: sentinel.
                self.slice_error = f"packaged domain-skill corpus is unusable: {exc}"
                logger.error("design-engine: %s", self.slice_error)
                return
        if self.cfg.task_state.enabled and self.cfg.task_state.state_root:
            from raven_design.task_state.manager import TaskStateManager

            self.manager = TaskStateManager(Path(self.cfg.task_state.state_root))

    @classmethod
    def for_context(cls, ctx: "PluginContext") -> "_Shared":
        key = str(getattr(getattr(ctx, "services", None), "workspace", None) or id(ctx))
        shared = cls._instances.get(key)
        if shared is None:
            shared = cls(ctx)
            cls._instances[key] = shared
        return shared

    def service_for(self, workspace: Path) -> "RenderService":
        key = str(workspace)
        service = self._services.get(key)
        if service is None:
            from raven.config.paths import get_media_dir, get_runtime_subdir
            from raven_design.rendering.service import RenderService

            service = RenderService.from_tool_config(
                self.cfg.render,
                workspace=workspace,
                media_root=get_media_dir(),
                runtime_root=get_runtime_subdir("render"),
                restrict_to_workspace=self.cfg.render.restrict_to_workspace,
            )
            self._services[key] = service
        return service


class _SeatedRenderTool(Tool):
    """A render tool re-seated per working directory.

    Name, description, schema and timeout come from a prototype built over the
    slice's settings alone -- construction touches no disk and probes no
    binary -- and every call resolves ``workdir.current()`` to the session's
    own directory, builds (or reuses) that directory's service, and delegates
    to a fork tool instance bound to it.
    """

    def __init__(self, shared: _Shared, tool_cls: type) -> None:
        from raven_design.rendering.models import RenderConfig

        self._shared = shared
        self._tool_cls = tool_cls
        render = shared.cfg.render
        capture = render.default_capture_duration_seconds
        # The advertised schema must say what the executing service will
        # enforce, so the slice's own preview and motion knobs ride into the
        # prototype config; the registration timeout mirrors the service's
        # own margin over the slice timeout (a boxlite worker adds a startup
        # budget on top -- the seated service's tool_timeout_seconds governs
        # the real call either way).
        prototype_service = SimpleNamespace(
            config=RenderConfig(
                chrome_path=None,
                libreoffice_path=None,
                default_preview_count=render.default_preview_count,
                max_preview_count=render.max_preview_count,
                max_motion_seconds=render.max_capture_duration_seconds,
                motion_fps=render.motion_fps,
                timeout_seconds=render.timeout_seconds,
            ),
            tool_timeout_seconds=float(render.timeout_seconds) + 30.0,
        )
        self._prototype = tool_cls(prototype_service, capture)
        self.timeout_seconds = self._prototype.timeout_seconds

    @property
    def name(self) -> str:
        return self._prototype.name

    @property
    def description(self) -> str:
        return self._prototype.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._prototype.parameters

    def display_call(self, args: dict[str, Any]) -> str | None:
        return self._prototype.display_call(args)

    async def execute(self, **params: Any) -> Any:
        bound = workdir.current()
        if bound is None:
            return "Error: rendering needs a bound working directory for this turn."
        capture = self._shared.cfg.render.default_capture_duration_seconds
        seated = self._tool_cls(self._shared.service_for(Path(bound)), capture)
        return await seated.execute(**params)


def _tools_admission(ctx: "PluginContext") -> _Shared | None:
    shared = _Shared.for_context(ctx)
    if shared.slice_error is not None or shared.cfg is None or not shared.cfg.enabled:
        return None
    return shared


def make_hook(ctx: "PluginContext"):
    shared = _Shared.for_context(ctx)
    if shared.slice_error is not None:
        return MisconfiguredEngineHook(shared.slice_error)
    if shared.cfg is None or not shared.cfg.enabled:
        return None
    if shared.selector is None and shared.manager is None:
        # Nothing this hook would do on any phase; declining keeps the chain
        # exactly as long as the configuration asked for.
        return None
    return DesignEngineHook(shared.cfg, shared.selector, shared.manager)


def _make_render_tool(ctx: "PluginContext", tool_cls_name: str):
    shared = _tools_admission(ctx)
    if shared is None or not shared.cfg.render.enabled:
        return None
    if missing := _render_extra_missing():
        logger.info(
            "design-engine: %s declines; the render extra is not installed (missing: %s)",
            tool_cls_name,
            ", ".join(missing),
        )
        return None
    from raven_design.tools.render import PreviewFileTool, RenderFileTool

    tool_cls = {"RenderFileTool": RenderFileTool, "PreviewFileTool": PreviewFileTool}[tool_cls_name]
    return _SeatedRenderTool(shared, tool_cls)


def make_render_file(ctx: "PluginContext"):
    return _make_render_tool(ctx, "RenderFileTool")


def make_preview_file(ctx: "PluginContext"):
    return _make_render_tool(ctx, "PreviewFileTool")


def make_update_task_state(ctx: "PluginContext"):
    shared = _tools_admission(ctx)
    if shared is None or shared.manager is None:
        return None
    from raven_design.task_state.tool import TaskStateTool

    return TaskStateTool(shared.manager)


__all__ = [
    "make_hook",
    "make_preview_file",
    "make_render_file",
    "make_update_task_state",
]
