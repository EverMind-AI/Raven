"""Building the tool set a chosen route needs, and nothing else.

The predecessor's assembly function took eleven parameters and carried ten
branches on one boolean, two of which contradicted each other well enough that a
whole sub-route -- fifteen hundred lines of designer tooling -- could not be
registered under any configuration. Nobody noticed for months, because a test had
frozen the unreachability as expected behaviour.

So this reads the route from the registry rather than recomputing it from flags,
and then checks its own work: every tool the profile's stages name has to exist
when assembly finishes. A route that cannot run says so here, at startup, in one
line naming what is missing -- rather than at the point a model calls a tool that
was never registered.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.backends.script import ScriptBackend, asset_helpers, run_script
from raven.ppt.contracts import Profile
from raven.ppt.profiles import registry
from raven.ppt.services.ingest import ingest_materials
from raven.ppt.stages._measure import DeckMeasurer
from raven.ppt.stages._views import DeckViews
from raven.ppt.stages.build import BuildStage
from raven.ppt.stages.design_pass import DesignPass, TypeFloors
from raven.ppt.stages.prepare import PrepareStage
from raven.ppt.tools._composer import ProviderComposer
from raven.ppt.tools.brief import PptBriefTool
from raven.ppt.tools.build import PptBuildTool
from raven.ppt.tools.fetch import PptFetchTool
from raven.ppt.tools.ingest import PptIngestTool
from raven.ppt.tools.inspect import PptFigureInspectTool
from raven.ppt.tools.outline import PptOutlineTool
from raven.ppt.tools.prepare import PptPrepareTool
from raven.ppt.tools.template import PptTemplateTool

log = logging.getLogger(__name__)


def build_ppt_tools(
    workspace: Path,
    *,
    profile: str = registry.DEFAULT,
    provider: Any | None = None,
    designer_model: str | None = None,
    design_pass: bool = False,
    design_rounds: int = 2,
    render_dpi: int = 144,
    render_concurrency: int = 2,
    design_concurrency: int = 6,
    deck_name: str = "deck.pptx",
    web_proxy: str | None = None,
) -> list[Tool]:
    """The tools for one route, or [] when the ppt extra is not installed.

    The empty list is deliberate and load-bearing: the deck tools are an optional
    extra, and an agent without python-pptx should start without them rather than
    fail to start. Every caller treats [] as "this build cannot make decks".
    """
    try:
        import PIL  # noqa: F401
        import pptx  # noqa: F401
    except ImportError:
        log.info("ppt tools unavailable: install raven[ppt]")
        return []

    try:
        chosen = registry.get(profile)
    except ValueError as exc:
        log.warning("%s -- falling back to %s", exc, registry.DEFAULT)
        chosen = registry.get(registry.DEFAULT)

    if chosen.backend != "script":
        # The other two routes are declared and their stages are not written yet.
        # Saying so beats registering a route that would fail at the first call.
        log.warning("the %s route is declared but its backend is not implemented yet", chosen.name)
        return []

    views = DeckViews(dpi=render_dpi, concurrency=render_concurrency)
    measure = DeckMeasurer(views=views)
    backend = _backend()
    stage = BuildStage(
        backend=backend,
        measure=measure,
        profile=chosen,
        design_pass=(
            _design_pass(provider, designer_model, views, measure, backend, design_rounds, design_concurrency)
            if design_pass
            else None
        ),
        destination=lambda project: project.exports_dir / deck_name,
    )
    tools: list[Tool] = [
        PptPrepareTool(workspace, _prepare(provider)),
        PptBriefTool(workspace),
        PptFetchTool(workspace, proxy=web_proxy, ingest=ingest_materials),
        PptIngestTool(workspace),
        PptFigureInspectTool(workspace, views),
        PptOutlineTool(workspace),
        PptTemplateTool(workspace, views),
        PptBuildTool(workspace, stage, views, chosen),
    ]
    _warn_if_incomplete(chosen, tools)
    return tools


def _prepare(provider: Any | None) -> PrepareStage:
    """The intake stage, with the ingest it drives and a model to read the task.

    The composer is the main model rather than the designer's: reading a task is
    the author's own kind of work, and the isolation that matters here is the empty
    context rather than a different set of weights. Without a provider the stage
    still ingests the conventional materials directory and still asks the three
    brief questions -- what is lost is the reading, not the preparation.
    """
    composer = ProviderComposer(provider=provider) if provider is not None else None
    return PrepareStage(composer=composer, ingest=ingest_materials)


def _backend():
    """The script backend as the callable the stage expects.

    A closure rather than the class, because the stage's contract is "give me a
    deck from this submission" and the backend's is "run a program" -- the helper
    installation belongs to neither and happens here, once, where the assets are
    known to be installed.
    """
    helpers = asset_helpers()

    async def backend(project, script):
        return await run_script(project, script, helpers=helpers)

    backend.name = ScriptBackend.name
    return backend


def _design_pass(
    provider: Any | None,
    model: str | None,
    views: DeckViews,
    measure: DeckMeasurer,
    rebuild,
    rounds: int,
    concurrency: int,
) -> DesignPass | None:
    """The design pass, or None when there is no model to run it on.

    None is the default rather than a degraded state, and `tools.ppt.designer.
    enabled` is what turns it on -- see PptDesignerConfig for the measurements
    that made it opt-in. A build without it still measures the deck and reports
    everything, and the author can act on all of it. What is lost is the second
    pair of eyes, not the checks.

    It shares the stage's measurer and the stage's rebuild, so the numbers it is
    handed between rounds are the numbers the stage will report at the end. Giving
    it its own would let a page pass its own check and fail the one that matters.
    """
    if provider is None:
        return None
    return DesignPass(
        renderer=views,
        composer=ProviderComposer(provider=provider, model=model or None),
        build=lambda project: rebuild(project, None),
        measure=measure,
        floors=TypeFloors(),
        rounds=rounds,
        concurrency=concurrency,
    )


def _warn_if_incomplete(profile: Profile, tools: list[Tool]) -> None:
    ok, missing = registry.available(profile.name, {tool.name for tool in tools})
    if missing:
        log.warning(
            "the %s route names %s that %s not registered: %s.",
            profile.name,
            "tools" if len(missing) > 1 else "a tool",
            "were" if len(missing) > 1 else "is",
            ", ".join(missing),
        )
    if not ok:
        log.warning("the %s route cannot run: a required stage has no tool.", profile.name)
    _warn_if_skill_missing(profile)


def _warn_if_skill_missing(profile: Profile) -> None:
    """Whether the skill the route declares is actually on disk.

    The field had no reader at all, so all three routes named a skill that did not
    exist and nothing said so for the whole of the port. It is checked rather than
    consumed: the skill reaches the prompt through the catalogue's own `always`
    flag, which is upstream's mechanism and not this package's business -- but a
    route that names one it does not ship is a claim worth failing out loud.
    """
    if not profile.skill:
        return
    from raven.memory_engine.skill_local.registry import _DEFAULT_BUILTIN_SKILLS_DIR

    if not (Path(_DEFAULT_BUILTIN_SKILLS_DIR) / profile.skill / "SKILL.md").is_file():
        log.warning(
            "the %s route declares the skill %r and no such SKILL.md ships; the author will work without it.",
            profile.name,
            profile.skill,
        )
