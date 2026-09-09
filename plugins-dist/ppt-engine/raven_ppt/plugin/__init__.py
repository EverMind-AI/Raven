"""Factories the manifest names: eleven deck tools and the turn-frame hook.

The registry calls one factory per contribution, each with its own
``PluginContext``, so the tools and the hook cannot be handed to each other
directly. A per-workspace :class:`_Shared` bridges them (the research-flow
shape): whichever factory runs first builds it, and every side reads the same
config slice, the same prototypes and the same per-workdir engine cache.

Admission is the D6 shape the other product plugins use: an absent or false
``enabled`` in ``plugins.config["ppt-engine"]`` means no surface is cast at
all, and a build whose deck dependencies cannot import declines the tools the
way the fork's assembly returned ``[]`` (verdict feature 1's factory-decline
convention). The fork's own gate was ``tools.ppt.enabled``, read per assembly;
plugin admission runs at the same time in this host, so the flip is of key
path, not of moment (D4/D5). A slice that does not PARSE is neither on nor
off: the host's stack builder skips a raising factory quietly, so the parse
error is caught here, the tool factories decline, and ``make_hook`` casts the
fail-closed :class:`~raven_ppt.plugin.hook.MisconfiguredEngineHook` -- every
turn answers with the config fix named until someone repairs the slice (the
code-flow MisconfiguredGate doctrine, w101).

Ten of the eleven tools are the fork's, re-seated per turn by
:mod:`.session_tools`; the eleventh, ``ppt_image_search``, is the D2
adaptation of the image search the fork had grown inside the trunk built-in
``web_search``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from raven.contracts.tool import Tool
from raven_ppt.plugin.config import EngineConfig
from raven_ppt.plugin.hook import MisconfiguredEngineHook, PptEngineHook
from raven_ppt.plugin.image_search import PptImageSearchTool
from raven_ppt.plugin.session_tools import SessionTool

if TYPE_CHECKING:
    from raven.plugins.context import PluginContext

# Never a real deck's home: prototypes are built once so the registered tools
# can answer for name, description, schema and timeout before any turn binds a
# working directory. Construction touches no disk under the workspace.
_PROTOTYPE_WORKSPACE = Path("/nonexistent/ppt-engine-prototype")

logger = logging.getLogger(__name__)


class _Shared:
    """One activation's shared state: the slice, prototypes, engine cache."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self.slice_error: str | None = None
        try:
            self.cfg = EngineConfig.from_slice(dict(ctx.config or {}))
        except ValueError as exc:
            # Caught here rather than allowed to escape the factories: the
            # host logs-and-skips a raising factory, which would silently boot
            # this deck product with no deck face at all. The factories read
            # this field: tools decline, the hook seat casts the sentinel.
            self.slice_error = str(exc)
            self.cfg = EngineConfig()
        self._prototypes: dict[str, Tool] | None = None
        self._engines: dict[str, dict[str, Tool]] = {}
        self._hook: PptEngineHook | None = None
        self._image_search: PptImageSearchTool | None = None

    def _assemble(self, workspace: Path) -> dict[str, Tool]:
        from raven_ppt.tools.assembly import build_ppt_tools

        cfg = self.cfg
        return {
            tool.name: tool
            for tool in build_ppt_tools(
                workspace,
                profile=cfg.profile,
                provider=self._ctx.services.provider,
                composer_model=cfg.composer_model or None,
                render_dpi=cfg.render_dpi,
                render_concurrency=cfg.render_concurrency,
                views_per_call=cfg.views_per_call,
                deck_name=cfg.deck_name,
                web_proxy=cfg.web_proxy,
                image_config=cfg.image,
                reader_effort=cfg.reader_effort or None,
            )
        }

    def prototypes(self) -> dict[str, Tool]:
        if self._prototypes is None:
            self._prototypes = self._assemble(_PROTOTYPE_WORKSPACE)
        return self._prototypes

    def engine_for(self, root: Path) -> dict[str, Tool]:
        """The fork-shaped tool set fenced inside ``root``, built once per root.

        One assembly per working directory is the fork's one-engine-per-session
        shape; entries are never reclaimed, which is the fork's own custody
        precedent (job directories were never reclaimed either) -- the code-2
        session-lifecycle seam collects this when it lands.
        """
        key = str(Path(root).resolve())
        engine = self._engines.get(key)
        if engine is None:
            engine = self._assemble(Path(key))
            self._engines[key] = engine
        return engine

    def session_tool(self, name: str) -> SessionTool | None:
        prototype = self.prototypes().get(name)
        if prototype is None:
            return None
        return SessionTool(prototype, self.engine_for)

    def image_search(self) -> PptImageSearchTool | None:
        """The D2 tool, or ``None`` without a key to search with.

        The fork's loop registered no keyless web_search rather than advertise
        a tool whose every call is an error string; the same refusal, said as a
        factory decline. Asked of the built tool rather than the slice because
        the key resolves from either the slice or ``SERPER_API_KEY``.
        """
        if self._image_search is None:
            tool = PptImageSearchTool(api_key=self.cfg.image_search_api_key, proxy=self.cfg.web_proxy)
            if tool.api_key:
                self._image_search = tool
        return self._image_search

    def hook(self) -> PptEngineHook:
        if self._hook is None:
            # The locator's workspace is the agent home whose bootstrap seats
            # the host's context builder reads; it is where the first-touch
            # identity seeding lands (see hook.seed_identity).
            self._hook = PptEngineHook(
                home=Path(self._ctx.services.workspace), deck_per_session=self.cfg.deck_per_session
            )
        return self._hook

    def deck_capable(self) -> bool:
        """Whether this environment can make decks at all (verdict feature 1)."""
        try:
            import PIL  # noqa: F401
            import pptx  # noqa: F401
        except ImportError:
            return False
        return bool(self.prototypes())


# Keyed by workspace rather than by ServiceLocator identity: the host builds a
# fresh locator per contribution kind, so identity would split the sides.
_SHARED: dict[str, _Shared] = {}


def _shared_for(ctx: "PluginContext") -> _Shared:
    key = str(ctx.services.workspace)
    shared = _SHARED.get(key)
    if shared is None:
        shared = _Shared(ctx)
        _SHARED[key] = shared
    return shared


def _contributing(ctx: "PluginContext") -> _Shared | None:
    shared = _shared_for(ctx)
    if shared.slice_error is not None or not shared.cfg.enabled:
        return None
    return shared


def _make_session_tool(ctx: "PluginContext", name: str) -> SessionTool | None:
    shared = _contributing(ctx)
    if shared is None or not shared.deck_capable():
        return None
    return shared.session_tool(name)


def make_ppt_prepare(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_prepare")


def make_ppt_brief(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_brief")


def make_ppt_fetch(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_fetch")


def make_ppt_generate_image(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_generate_image")


def make_ppt_ingest(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_ingest")


def make_ppt_figure_inspect(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_figure_inspect")


def make_ppt_outline(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_outline")


def make_ppt_template(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_template")


def make_ppt_build(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_build")


def make_ppt_review(ctx: "PluginContext") -> Tool | None:
    return _make_session_tool(ctx, "ppt_review")


def make_ppt_image_search(ctx: "PluginContext") -> Tool | None:
    shared = _contributing(ctx)
    return shared.image_search() if shared else None


def make_hook(ctx: "PluginContext") -> PptEngineHook | MisconfiguredEngineHook | None:
    """The turn-frame hook: staging in, verification out.

    Gated on the slice alone, not on the deck imports: the fork staged material
    before its engine assembled, and a staging that works while the tools
    decline names the broken install in its own reply instead of dropping the
    user's documents without a word. A slice that does not parse casts the
    fail-closed sentinel instead -- deliberately even when ``enabled`` was
    meant to be false, because an unparseable slice proves nothing about
    intent, and closed-and-loud beats open-and-quiet.
    """
    shared = _shared_for(ctx)
    if shared.slice_error is not None:
        logger.warning(
            "ppt-engine: config slice is malformed; casting a fail-closed hook: %s",
            shared.slice_error,
        )
        return MisconfiguredEngineHook(shared.slice_error)
    return shared.hook() if shared.cfg.enabled else None


__all__ = [
    "make_hook",
    "make_ppt_brief",
    "make_ppt_build",
    "make_ppt_fetch",
    "make_ppt_figure_inspect",
    "make_ppt_generate_image",
    "make_ppt_image_search",
    "make_ppt_ingest",
    "make_ppt_outline",
    "make_ppt_prepare",
    "make_ppt_review",
    "make_ppt_template",
]
