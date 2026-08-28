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
from raven.ppt.backends.script import ScriptBackend, asset_helpers, provision, run_script, with_template_helpers
from raven.ppt.contracts import Profile
from raven.ppt.profiles import registry
from raven.ppt.services.ingest import ingest_materials
from raven.ppt.services.template import bound
from raven.ppt.stages._measure import DeckMeasurer
from raven.ppt.stages._views import DeckViews
from raven.ppt.stages.build import BATCH_VIEWS, BuildStage
from raven.ppt.stages.prepare import PrepareStage
from raven.ppt.tools._composer import ProviderComposer
from raven.ppt.tools.brief import PptBriefTool
from raven.ppt.tools.build import PptBuildTool
from raven.ppt.tools.fetch import PptFetchTool
from raven.ppt.tools.generate_image import PptGenerateImageTool
from raven.ppt.tools.ingest import PptIngestTool
from raven.ppt.tools.inspect import PptFigureInspectTool
from raven.ppt.tools.outline import PptOutlineTool
from raven.ppt.tools.prepare import PptPrepareTool
from raven.ppt.tools.review import PptReviewTool
from raven.ppt.tools.template import PptTemplateTool

log = logging.getLogger(__name__)


def build_ppt_tools(
    workspace: Path,
    *,
    profile: str = registry.DEFAULT,
    provider: Any | None = None,
    composer_model: str | None = None,
    render_dpi: int = 144,
    render_concurrency: int = 2,
    views_per_call: int = BATCH_VIEWS,
    deck_name: str = "deck.pptx",
    web_proxy: str | None = None,
    image_config: Any | None = None,
    media_proxy: str | None = None,
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
    helpers = asset_helpers()

    def provision_script_workspace(project):
        template = bound(project)
        effective = with_template_helpers(helpers, template) if template else helpers
        return provision(project, effective)

    backend = _backend(helpers)
    stage = BuildStage(
        backend=backend,
        measure=measure,
        profile=chosen,
        destination=lambda project: project.exports_dir / deck_name,
        views_per_call=views_per_call,
    )
    # Built first: the build tool runs it on the first finished deck, and the model
    # can call it on its own besides. One instance, so both routes share the record
    # that says a deck has already had its first reading.
    review = PptReviewTool(
        workspace,
        views,
        composer=ProviderComposer(provider=provider, model=composer_model or None)
        if provider is not None
        else None,
    )
    tools: list[Tool] = [
        PptPrepareTool(workspace, _prepare(provider), provision=provision_script_workspace),
        PptBriefTool(workspace),
        PptFetchTool(workspace, proxy=web_proxy, ingest=ingest_materials),
        PptGenerateImageTool(workspace, image_config, proxy=media_proxy),
        PptIngestTool(workspace),
        PptFigureInspectTool(
            workspace,
            views,
            composer=ProviderComposer(provider=provider, model=composer_model or None)
            if provider is not None
            else None,
        ),
        PptOutlineTool(workspace),
        PptTemplateTool(workspace, views, provision=provision_script_workspace),
        PptBuildTool(workspace, stage, views, chosen, review=review),
        # Registered as well as wired into the build: the automatic reading happens
        # once, on the first finished deck, and after that the author asks for one.
        review,
    ]
    _warn_if_incomplete(chosen, tools)
    return tools


def _prepare(provider: Any | None) -> PrepareStage:
    """The intake stage, with the ingest it drives and a model to read the task.

    The composer is the main model rather than the second one: reading a task is
    the author's own kind of work, and the isolation that matters here is the empty
    context rather than a different set of weights. Without a provider the stage
    still ingests the conventional materials directory and still asks the three
    brief questions -- what is lost is the reading, not the preparation.
    """
    composer = ProviderComposer(provider=provider) if provider is not None else None
    return PrepareStage(composer=composer, ingest=ingest_materials)


def _backend(helpers):
    """The script backend as the callable the stage expects.

    A closure rather than the class, because the stage's contract is "give me a
    deck from this submission" and the backend's is "run a program" -- the helper
    installation belongs to neither and happens here, once, where the assets are
    known to be installed.
    """

    async def backend(project, script):
        return await run_script(project, script, helpers=helpers)

    backend.name = ScriptBackend.name
    return backend


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
